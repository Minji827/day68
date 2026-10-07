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


def load_companies(cur) -> None:
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
        ON CONFLICT (corp_code) DO UPDATE SET
            corp_name = EXCLUDED.corp_name,
            stock_code = EXCLUDED.stock_code,
            updated_at = now()
        """
    )
    print(f"core.companies: {cur.rowcount}건 upsert")


def load_disclosures(cur) -> None:
    cur.execute("SELECT corp_code, response FROM raw.opendart_disclosure_calls")
    rows_to_insert = []
    for corp_code, response in cur.fetchall():
        for item in response.get("list", []):
            report_nm = item["report_nm"].strip()
            rows_to_insert.append(
                (
                    item["rcept_no"],
                    item["corp_code"],
                    report_nm,
                    clean_title(report_nm),
                    "정정" in report_nm,
                    item["rcept_dt"],
                    item.get("flr_nm"),
                    item.get("rm"),
                )
            )
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


def load_financial_accounts(cur) -> None:
    cur.execute("SELECT account_nm_raw, account_std_code, account_std_name, agg_method FROM core.account_mapping")
    mapping: dict[str, list[tuple[str, str, str]]] = {}
    for raw_nm, std_code, std_name, agg_method in cur.fetchall():
        mapping.setdefault(raw_nm, []).append((std_code, std_name, agg_method))

    cur.execute("SELECT corp_code, bsns_year, reprt_code, fs_div, response FROM raw.opendart_financial_calls")
    agg: dict[tuple, dict] = {}
    for corp_code, bsns_year, reprt_code, fs_div, response in cur.fetchall():
        for item in response.get("list", []):
            account_nm = item.get("account_nm", "").strip()
            if account_nm not in mapping:
                continue
            thstrm = to_decimal(item.get("thstrm_amount"))
            frmtrm = to_decimal(item.get("frmtrm_amount"))
            for std_code, std_name, agg_method in mapping[account_nm]:
                key = (corp_code, bsns_year, reprt_code, fs_div, std_code)
                if key not in agg:
                    agg[key] = {"std_name": std_name, "thstrm": thstrm or Decimal(0), "frmtrm": frmtrm or Decimal(0)}
                else:
                    agg[key]["thstrm"] += thstrm or Decimal(0)
                    agg[key]["frmtrm"] += frmtrm or Decimal(0)

    rows = [
        (corp_code, bsns_year, reprt_code, fs_div, std_code, v["std_name"], v["thstrm"], v["frmtrm"])
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
             thstrm_amount, frmtrm_amount)
        VALUES %s
        ON CONFLICT (corp_code, bsns_year, reprt_code, fs_div, account_std_code) DO UPDATE SET
            thstrm_amount = EXCLUDED.thstrm_amount,
            frmtrm_amount = EXCLUDED.frmtrm_amount
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
