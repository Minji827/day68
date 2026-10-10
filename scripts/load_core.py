"""Transform raw.* -> core.* : dedup, standardize, link corrections to originals.

PRD 9번 CORE 레이어. 핵심 로직:
- 정정공시 연결: "[기재정정]" 등 접두어를 뗀 제목으로, 같은 기업의 더 이른 공시를 매칭
  (OpenDART list.json에 원공시 접수번호 필드가 없음을 실측으로 확인했기 때문).
- 재무계정 표준화: core.account_mapping으로 raw 계정명 -> 표준 계정코드 변환,
  차입금처럼 쪼개진 계정은 합산.
"""
from __future__ import annotations

import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import psycopg2.extras

CORRECTION_PREFIX_RE = re.compile(r"^(\[[^\]]*\])+")


def clean_title(report_nm: str) -> str:
    return CORRECTION_PREFIX_RE.sub("", report_nm).strip()


def to_decimal(s) -> Decimal | None:
    if s in (None, "", "-"):
        return None
    try:
        return Decimal(str(s).replace(",", ""))
    except InvalidOperation:
        return None


def load_companies(cur, corp_code: str | None = None) -> None:
    cur.execute(
        """
        INSERT INTO core.companies (corp_code, corp_name, stock_code, updated_at)
        SELECT DISTINCT rc.corp_code, rc.corp_name, NULLIF(rc.stock_code, ''), now()
        FROM raw.opendart_corp_code rc
        WHERE rc.corp_code IN (
            SELECT corp_code FROM raw.opendart_disclosure_calls
            UNION
            SELECT corp_code FROM raw.opendart_financial_calls
        )
        AND (%(corp_code)s IS NULL OR rc.corp_code = %(corp_code)s)
        ON CONFLICT (corp_code) DO UPDATE SET
            corp_name = EXCLUDED.corp_name,
            stock_code = EXCLUDED.stock_code,
            updated_at = now()
        """,
        {"corp_code": corp_code},
    )
    print(f"core.companies: {cur.rowcount}건 upsert")


def load_disclosures(cur, corp_code: str | None = None) -> None:
    if corp_code:
        cur.execute("SELECT corp_code, response FROM raw.opendart_disclosure_calls WHERE corp_code = %s", (corp_code,))
    else:
        cur.execute("SELECT corp_code, response FROM raw.opendart_disclosure_calls")
    # rcept_no로 dedup — list.json 페이지네이션(collect.py) 중 새 공시가 끼어들면
    # 페이지 경계가 밀려서 같은 rcept_no가 두 페이지에 걸쳐 중복으로 올 수 있다.
    # execute_values의 ON CONFLICT DO UPDATE는 같은 커맨드 안에서 같은 행을 두 번
    # 건드리면 에러나므로, dict로 rcept_no당 하나만 남긴다(나중 값으로 덮어써도 내용은 동일).
    rows_by_rcept_no: dict[str, tuple] = {}
    for corp_code, response in cur.fetchall():
        for item in response.get("list", []):
            report_nm = item["report_nm"].strip()
            rows_by_rcept_no[item["rcept_no"]] = (
                item["rcept_no"],
                item["corp_code"],
                report_nm,
                clean_title(report_nm),
                "정정" in report_nm,
                item["rcept_dt"],
                item.get("flr_nm"),
                item.get("rm"),
            )
    rows_to_insert = list(rows_by_rcept_no.values())
    if not rows_to_insert:
        print("core.disclosures: 적재할 raw 데이터 없음")
        return

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO core.disclosures
            (rcept_no, corp_code, report_nm, report_nm_clean, is_correction, rcept_dt, flr_nm, rm)
        VALUES %s
        ON CONFLICT (rcept_no) DO UPDATE SET
            report_nm = EXCLUDED.report_nm,
            report_nm_clean = EXCLUDED.report_nm_clean,
            is_correction = EXCLUDED.is_correction,
            rcept_dt = EXCLUDED.rcept_dt,
            flr_nm = EXCLUDED.flr_nm,
            rm = EXCLUDED.rm
        """,
        rows_to_insert,
        template="(%s, %s, %s, %s, %s, to_date(%s, 'YYYYMMDD'), %s, %s)",
    )
    print(f"core.disclosures: {len(rows_to_insert)}건 upsert")

    # 재적재 시 이전 매칭 결과를 지우고 다시 계산 (매칭 로직이 바뀔 수 있으므로 멱등성 보장)
    cur.execute(
        "UPDATE core.disclosures SET orig_rcept_no = NULL, match_method = NULL WHERE is_correction"
    )

    # 정정공시 -> 원공시 연결.
    # 1단계: 같은 기업 + 같은 정제 제목 + 같은 제출인(flr_nm) 중 가장 가까운 과거 공시.
    #   ("임원ㆍ주요주주특정증권등소유상황보고서"처럼 매일 여러 임원이 각자 내는 공통
    #   양식은 제목만으로 매칭하면 다른 사람의 원공시와 잘못 연결됨 - 실측으로 확인된 버그.
    #   flr_nm까지 같아야 신뢰 가능한 매칭으로 본다.)
    cur.execute(
        """
        UPDATE core.disclosures d
        SET orig_rcept_no = m.orig_rcept_no,
            match_method = 'exact_title_filer'
        FROM (
            SELECT DISTINCT ON (c.rcept_no)
                c.rcept_no,
                o.rcept_no AS orig_rcept_no
            FROM core.disclosures c
            JOIN core.disclosures o
                ON o.corp_code = c.corp_code
               AND o.report_nm_clean = c.report_nm_clean
               AND o.flr_nm = c.flr_nm
               AND o.rcept_dt < c.rcept_dt
               AND o.rcept_no <> c.rcept_no
            WHERE c.is_correction
            ORDER BY c.rcept_no, o.rcept_dt DESC
        ) m
        WHERE d.rcept_no = m.rcept_no
        """
    )
    linked_filer = cur.rowcount

    # 2단계: flr_nm으로 못 찾았는데, 제목만으로도 과거 후보가 "정확히 1개"뿐이면 안전하게 매칭.
    # 후보가 2개 이상이면 추측하지 않고 넘어간다 (PRD: 추측하지 않고 누락으로 표시).
    cur.execute(
        """
        WITH candidates AS (
            SELECT c.rcept_no AS corr_rcept_no, o.rcept_no AS orig_rcept_no,
                   count(*) OVER (PARTITION BY c.rcept_no) AS cand_count,
                   row_number() OVER (PARTITION BY c.rcept_no ORDER BY o.rcept_dt DESC) AS rn
            FROM core.disclosures c
            JOIN core.disclosures o
                ON o.corp_code = c.corp_code
               AND o.report_nm_clean = c.report_nm_clean
               AND o.rcept_dt < c.rcept_dt
               AND o.rcept_no <> c.rcept_no
            WHERE c.is_correction AND c.orig_rcept_no IS NULL
        )
        UPDATE core.disclosures d
        SET orig_rcept_no = candidates.orig_rcept_no,
            match_method = 'exact_title_unique'
        FROM candidates
        WHERE d.rcept_no = candidates.corr_rcept_no
          AND candidates.rn = 1 AND candidates.cand_count = 1
        """
    )
    linked_unique = cur.rowcount

    cur.execute(
        """
        UPDATE core.disclosures
        SET match_method = 'unmatched'
        WHERE is_correction AND orig_rcept_no IS NULL AND match_method IS NULL
        """
    )
    unmatched = cur.rowcount
    print(
        f"정정공시 원공시 연결: {linked_filer}건 매칭(제출인 일치), "
        f"{linked_unique}건 매칭(제목 유일), {unmatched}건 미매칭(정정 전 공시 없음)"
    )


# "당기순이익"류 계정명이 IS 외 섹션(CIS/CF/SCE)에도 중복 등장하는 게 확인된 표준계정만
# sj_div='IS'로 한정한다 (REVENUE/OP_INCOME은 실측상 중복이 없어 불필요 - 범위를 넓히지 않음).
IS_ONLY_STD_CODES = {"NET_INCOME"}

# v4.3-2 실측 버그: REVENUE는 "매출액"뿐 아니라 "영업수익"(NAVER·이스트에이드·SK스퀘어
# 등 지주사/플랫폼 기업이 쓰는 동의어)도 매핑해야 하는데, SK스퀘어는 같은 보고서 안에
# 두 계정명을 "같은 값"으로 둘 다 공시한다(실측: 1,906,611,000,000원씩 동일). 기존
# 합산 로직을 그대로 쓰면 이런 기업은 매출이 2배로 뻥튀기된다(NET_INCOME 중복집계
# 버그와 같은 종류) - REVENUE는 합산 대상이 아니라 "동의어 중 하나만" 골라야 한다.
# 우선순위 목록: 먼저 오는 이름이 이미 값을 채웠으면 나중 동의어는 무시, 나중에 더
# 우선순위 높은 이름이 오면 덮어쓴다(순서 무관하게 항상 '매출액' 승리).
EXCLUSIVE_STD_CODE_PRIORITY = {"REVENUE": ("매출액", "영업수익")}


def load_financial_accounts(cur, corp_code: str | None = None) -> None:
    cur.execute("SELECT account_nm_raw, account_std_code, account_std_name, agg_method FROM core.account_mapping")
    mapping: dict[str, list[tuple[str, str, str]]] = {}
    for raw_nm, std_code, std_name, agg_method in cur.fetchall():
        mapping.setdefault(raw_nm, []).append((std_code, std_name, agg_method))

    # raw.opendart_financial_calls는 감사 추적용이라 같은 기업을 여러 번 분석하면 같은
    # (corp_code, bsns_year, reprt_code, fs_div)로 계속 새 행이 쌓인다(의도된 설계). 여기서
    # 그걸 전부 더하면 분석 횟수만큼 금액이 배로 불어난다 - DISTINCT ON으로 각 키의 가장
    # 최신 호출(requested_at 기준) 한 건만 골라서 집계한다.
    if corp_code:
        cur.execute(
            """
            SELECT DISTINCT ON (corp_code, bsns_year, reprt_code, fs_div)
                corp_code, bsns_year, reprt_code, fs_div, response
            FROM raw.opendart_financial_calls
            WHERE corp_code = %s
            ORDER BY corp_code, bsns_year, reprt_code, fs_div, requested_at DESC
            """,
            (corp_code,),
        )
    else:
        cur.execute(
            """
            SELECT DISTINCT ON (corp_code, bsns_year, reprt_code, fs_div)
                corp_code, bsns_year, reprt_code, fs_div, response
            FROM raw.opendart_financial_calls
            ORDER BY corp_code, bsns_year, reprt_code, fs_div, requested_at DESC
            """
        )
    agg: dict[tuple, dict] = {}
    for corp_code, bsns_year, reprt_code, fs_div, response in cur.fetchall():
        for item in response.get("list", []):
            account_nm = item.get("account_nm", "").strip()
            if account_nm not in mapping:
                continue
            # v4.2-1 실측 버그: "당기순이익"/"분기순이익" 계정명은 손익계산서(IS)뿐 아니라
            # 포괄손익계산서(CIS)·현금흐름표(CF)·자본변동표(SCE)에도 (DART 공시 관행상)
            # 동일 문자열로 중복 등장한다 - sj_div 구분 없이 합치면 실제 값의 몇 배로
            # 부풀려진다(삼성전자 2026 1Q 실측: 필터 전 283조 vs 손익계산서상 실제 47조).
            # 손익계산서(IS) 전용 표준계정은 그 섹션에서만 집계한다.
            sj_div = item.get("sj_div")
            thstrm = to_decimal(item.get("thstrm_amount"))
            frmtrm = to_decimal(item.get("frmtrm_amount"))
            # 분기/반기 보고서의 손익계산서 항목에서만 오는 필드 - 없으면 None 그대로 둔다
            # (분기별 계산에 쓰는 scripts/quarterly.py 참고. 연간 사업보고서나 재무상태표
            # 항목에는 애초에 안 오는 필드라 결측이 정상).
            thstrm_add = to_decimal(item.get("thstrm_add_amount"))
            frmtrm_q = to_decimal(item.get("frmtrm_q_amount"))
            frmtrm_add = to_decimal(item.get("frmtrm_add_amount"))
            for std_code, std_name, agg_method in mapping[account_nm]:
                if std_code in IS_ONLY_STD_CODES and sj_div != "IS":
                    continue
                key = (corp_code, bsns_year, reprt_code, fs_div, std_code)
                priority_list = EXCLUSIVE_STD_CODE_PRIORITY.get(std_code)
                if priority_list is not None:
                    this_priority = priority_list.index(account_nm) if account_nm in priority_list else len(priority_list)
                    existing = agg.get(key)
                    if existing is not None and this_priority >= existing["_src_priority"]:
                        continue  # 이미 더 우선순위 높은(또는 동률) 동의어가 채워져 있음 - 합치지 않고 버림
                    agg[key] = {
                        "std_name": std_name, "thstrm": thstrm or Decimal(0), "frmtrm": frmtrm or Decimal(0),
                        "thstrm_add": thstrm_add, "frmtrm_q": frmtrm_q, "frmtrm_add": frmtrm_add,
                        "_src_priority": this_priority,
                    }
                    continue
                if key not in agg:
                    agg[key] = {
                        "std_name": std_name, "thstrm": thstrm or Decimal(0), "frmtrm": frmtrm or Decimal(0),
                        "thstrm_add": thstrm_add, "frmtrm_q": frmtrm_q, "frmtrm_add": frmtrm_add,
                    }
                else:
                    agg[key]["thstrm"] += thstrm or Decimal(0)
                    agg[key]["frmtrm"] += frmtrm or Decimal(0)
                    # 합산 대상 세부계정이 여러 개면(예: 장단기 차입금 합산) 이 분기 전용
                    # 필드들도 같이 더한다 - None끼리는 None 유지, 하나라도 있으면 그만큼만 더함.
                    for f, v in (("thstrm_add", thstrm_add), ("frmtrm_q", frmtrm_q), ("frmtrm_add", frmtrm_add)):
                        if v is not None:
                            agg[key][f] = (agg[key][f] or Decimal(0)) + v

    rows = [
        (corp_code, bsns_year, reprt_code, fs_div, std_code, v["std_name"], v["thstrm"], v["frmtrm"],
         v["thstrm_add"], v["frmtrm_q"], v["frmtrm_add"])
        for (corp_code, bsns_year, reprt_code, fs_div, std_code), v in agg.items()
    ]
    if not rows:
        print("core.financial_accounts: 적재할 매핑 데이터 없음")
        return
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO core.financial_accounts
            (corp_code, bsns_year, reprt_code, fs_div, account_std_code, account_std_name,
             thstrm_amount, frmtrm_amount, thstrm_add_amount, frmtrm_q_amount, frmtrm_add_amount)
        VALUES %s
        ON CONFLICT (corp_code, bsns_year, reprt_code, fs_div, account_std_code) DO UPDATE SET
            thstrm_amount = EXCLUDED.thstrm_amount,
            frmtrm_amount = EXCLUDED.frmtrm_amount,
            thstrm_add_amount = EXCLUDED.thstrm_add_amount,
            frmtrm_q_amount = EXCLUDED.frmtrm_q_amount,
            frmtrm_add_amount = EXCLUDED.frmtrm_add_amount
        """,
        rows,
    )
    print(f"core.financial_accounts: {len(rows)}건 upsert")


def load_rate_observations(cur) -> None:
    cur.execute("SELECT stat_code, item_code, response FROM raw.ecos_rate_calls")
    rows = []
    for stat_code, item_code, response in cur.fetchall():
        for r in response:
            rows.append(
                (
                    r["STAT_CODE"],
                    r["ITEM_CODE1"],
                    r["TIME"],
                    to_decimal(r["DATA_VALUE"]),
                    r.get("UNIT_NAME"),
                )
            )
    if not rows:
        print("core.rate_observations: 적재할 raw 데이터 없음")
        return
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO core.rate_observations (stat_code, item_code, obs_date, value, unit)
        VALUES %s
        ON CONFLICT (stat_code, item_code, obs_date) DO UPDATE SET
            value = EXCLUDED.value, unit = EXCLUDED.unit
        """,
        rows,
        template="(%s, %s, to_date(%s, 'YYYYMMDD'), %s, %s)",
    )
    print(f"core.rate_observations: {len(rows)}건 upsert")


def main() -> None:
    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                load_companies(cur)
                load_disclosures(cur)
                load_financial_accounts(cur)
                load_rate_observations(cur)
        print("OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
