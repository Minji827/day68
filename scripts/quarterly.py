"""DART 분기보고서에서 순수 "그 분기만"의 값을 계산해 core.quarterly_financials에 저장한다.

핵심 함정 (OpenDART 공식 개발가이드 opendart.fss.or.kr 확인): 분기/반기 보고서의 (포괄)
손익계산서(IS) 항목은 thstrm_amount가 이미 "[3개월]"(그 분기만의 값)이고, 누적치는 별도
필드 thstrm_add_amount로 따로 온다 - thstrm_amount를 누적치로 오해해서 분기끼리 빼면
음수 매출 같은 틀린 값이 나온다(실제로 겪은 버그). 반면 현금흐름표(CF) 항목은 thstrm_add_
amount 필드 자체가 없다(DART가 안 줌) - 중간보고 현금흐름표는 통상 연초 누적으로 공시하는
회계 관행과 일치해서, CF는 thstrm_amount를 누적치로 보고 분기끼리 빼서 분리한다.

올해/작년을 각각 독립적으로 계산한다 (이전 버전의 함정: 올해 보고서의 frmtrm_q_amount로
작년 분기를 "같이" 얻으려 했는데, 그러면 올해 아직 안 올라온 분기만큼 작년도 같이
비어버린다 - 예를 들어 올해 3분기보고서가 11월에야 나오면 "작년 3분기"도 그때까지 안
뜨는 식. 작년은 이미 다 지난 해라 4개 보고서가 전부 존재하므로, 작년 몫도 작년 자체의
보고서 4종에서 따로 계산해야 "작년 거는 전부 뜨는" 게 맞다).

Usage:
    python scripts/quarterly.py --stock-code 452190 --bsns-year 2026
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db

QUARTER_REPRT_CODE = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}

# 손익계산서(IS) 항목: thstrm_amount가 이미 "그 분기 3개월"만의 값 (DART 공식 문서 확인).
# v4.2-1: NET_INCOME(당기순이익) 추가 - 분기 차트에 순이익률 보여주려고 필요(팀 리뷰).
# 손익계산서 항목이라 REVENUE/OP_INCOME과 동일하게 취급(별도 분기 분리 계산 불필요).
IS_ACCOUNTS = ("REVENUE", "OP_INCOME", "NET_INCOME")
# 현금흐름표(CF) 항목: thstrm_add_amount 필드가 없음 - 중간보고 현금흐름표의 통상적인
# 누적 공시 관행에 따라 thstrm_amount를 "연초 누적"으로 보고 분기끼리 빼서 분리한다
# (DART 공식 문서에 명시는 없음 - 최선 추정, 코드 설명 참고).
CF_ACCOUNTS = ("OCF",)
FLOW_ACCOUNTS = IS_ACCOUNTS + CF_ACCOUNTS


def _rows_by_reprt(cur, corp_code: str, bsns_year: str) -> dict[str, dict[str, dict]]:
    """reprt_code -> fs_div -> {account_std_code: {필드별 값}} (해당 연도 보고서만)."""
    cur.execute(
        """
        SELECT reprt_code, fs_div, account_std_code, account_std_name, thstrm_amount, thstrm_add_amount
        FROM core.financial_accounts
        WHERE corp_code = %s AND bsns_year = %s
          AND reprt_code = ANY(%s) AND account_std_code = ANY(%s)
        """,
        (corp_code, bsns_year, list(QUARTER_REPRT_CODE.values()), list(FLOW_ACCOUNTS)),
    )
    out: dict[str, dict[str, dict]] = {}
    for reprt_code, fs_div, std_code, std_name, thstrm, thstrm_add in cur.fetchall():
        out.setdefault(reprt_code, {}).setdefault(fs_div, {})[std_code] = {
            "thstrm": thstrm, "thstrm_add": thstrm_add, "std_name": std_name,
        }
    return out


def _pick_fs_div(by_reprt: dict[str, dict[str, dict]], account: str) -> str | None:
    """fs_div(연결/별도)는 하나로 통일해야 한다 - 중간에 섞이면 비교가 틀어진다. 4개
    보고서가 전부 있어야 하는 건 아니다(올해는 아직 3·4분기가 없는 게 정상) - 실제로
    존재하는 가장 이른 보고서 기준으로 CFS 우선, 없으면 OFS를 고른다."""
    for rc in QUARTER_REPRT_CODE.values():
        if rc not in by_reprt:
            continue
        for fs_div in ("CFS", "OFS"):
            if fs_div in by_reprt[rc] and account in by_reprt[rc][fs_div]:
                return fs_div
    return None


def _derive_is_account(by_reprt: dict, fs_div: str, account: str) -> dict[int, Decimal | None]:
    """손익계산서: thstrm_amount를 그대로 쓴다(이미 그 분기만의 값). 4분기만 사업보고서
    (연간 누적) - 9개월 누적(3분기보고서의 thstrm_add_amount)으로 뺄셈."""
    def cell_of(q: int) -> dict | None:
        return by_reprt.get(QUARTER_REPRT_CODE[q], {}).get(fs_div, {}).get(account)

    out: dict[int, Decimal | None] = {}
    for q in (1, 2, 3):
        cell = cell_of(q)
        out[q] = cell["thstrm"] if cell else None

    fy, nine_mo = cell_of(4), cell_of(3)
    if fy and nine_mo and fy["thstrm"] is not None and nine_mo["thstrm_add"] is not None:
        out[4] = fy["thstrm"] - nine_mo["thstrm_add"]
    else:
        out[4] = None
    return out


def _derive_cf_account(by_reprt: dict, fs_div: str, account: str) -> dict[int, Decimal | None]:
    """현금흐름표: thstrm_amount를 연초 누적으로 보고 분기끼리 뺀다."""
    def cell_of(q: int) -> dict | None:
        return by_reprt.get(QUARTER_REPRT_CODE[q], {}).get(fs_div, {}).get(account)

    cum: dict[int, Decimal | None] = {}
    for q in (1, 2, 3, 4):
        cell = cell_of(q)
        cum[q] = cell["thstrm"] if cell else None

    out: dict[int, Decimal | None] = {}
    prev_cum = Decimal(0)
    for q in (1, 2, 3, 4):
        if cum[q] is None or prev_cum is None:
            out[q] = None
            prev_cum = None
        else:
            out[q] = cum[q] - prev_cum
            prev_cum = cum[q]
    return out


def _derive_year(cur, corp_code: str, bsns_year: str) -> dict[str, tuple[dict[int, Decimal | None], str]]:
    """한 해(bsns_year)의 보고서만으로 그 해의 분기별 순수값을 계산한다. 반환:
    {account_std_code: ({quarter: amount}, std_name)}. 그 해 보고서가 아예 없으면 빈 dict."""
    by_reprt = _rows_by_reprt(cur, corp_code, bsns_year)
    result: dict[str, tuple[dict[int, Decimal | None], str]] = {}
    for account in FLOW_ACCOUNTS:
        fs_div = _pick_fs_div(by_reprt, account)
        if fs_div is None:
            continue
        std_name = next(
            by_reprt[rc][fs_div][account]["std_name"]
            for rc in QUARTER_REPRT_CODE.values()
            if rc in by_reprt and fs_div in by_reprt[rc] and account in by_reprt[rc][fs_div]
        )
        amounts = (
            _derive_is_account(by_reprt, fs_div, account)
            if account in IS_ACCOUNTS
            else _derive_cf_account(by_reprt, fs_div, account)
        )
        result[account] = (amounts, std_name)
    return result


def derive_quarters(cur, corp_code: str, bsns_year: str) -> int:
    """올해(bsns_year)와 작년(bsns_year-1)을 각각 그 해 자체의 보고서로 독립적으로
    계산해 upsert한다 - 올해 3·4분기가 아직 없어도 작년은 4분기 전부 뜬다. 특정 분기
    보고서가 없으면(예: 올해 3분기보고서는 11월에야 나옴) 있는 데까지만 계산하고
    나머지는 결측으로 둔다 - 추정하지 않는다."""
    prior_year = str(int(bsns_year) - 1)
    curr_by_account = _derive_year(cur, corp_code, bsns_year)
    prior_by_account = _derive_year(cur, corp_code, prior_year)

    rows = []
    for account in FLOW_ACCOUNTS:
        curr_amounts, curr_name = curr_by_account.get(account, ({}, None))
        prior_amounts, prior_name = prior_by_account.get(account, ({}, None))
        std_name = curr_name or prior_name
        if std_name is None:
            continue
        fs_div = "CFS"  # 저장용 메타 정보일 뿐 - 두 해 모두 독립적으로 CFS 우선 선택됨
        for q in (1, 2, 3, 4):
            c = curr_amounts.get(q)
            p = prior_amounts.get(q)
            if c is None and p is None:
                continue
            rows.append((corp_code, bsns_year, q, account, std_name, c, p, fs_div))

    if not rows:
        return 0

    import psycopg2.extras

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO core.quarterly_financials
            (corp_code, bsns_year, quarter, account_std_code, account_std_name, curr_amount, prior_amount, fs_div)
        VALUES %s
        ON CONFLICT (corp_code, bsns_year, quarter, account_std_code) DO UPDATE SET
            curr_amount = EXCLUDED.curr_amount, prior_amount = EXCLUDED.prior_amount, fs_div = EXCLUDED.fs_div
        """,
        rows,
    )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock-code")
    parser.add_argument("--corp-code")
    parser.add_argument("--bsns-year", required=True)
    args = parser.parse_args()

    conn = db.get_conn()
    try:
        with conn, conn.cursor() as cur:
            corp_code = args.corp_code
            if args.stock_code:
                cur.execute("SELECT corp_code FROM core.companies WHERE stock_code = %s", (args.stock_code,))
                row = cur.fetchone()
                if not row:
                    print(f"종목코드 '{args.stock_code}' 기업을 core.companies에서 찾을 수 없습니다.")
                    return
                corp_code = row[0]
            if not corp_code:
                print("--stock-code 또는 --corp-code 필요")
                return
            n = derive_quarters(cur, corp_code, args.bsns_year)
            print(f"core.quarterly_financials: {n}건 upsert")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
