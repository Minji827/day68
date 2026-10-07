"""변화 탐지 SQL 룰엔진: F1-F5(재무), D1-D5(공시 가산), CTX1(기준금리 맥락, 무점수).
core.* -> mart.change_events / mart.company_priority / mart.kpi_daily / mart.rate_context.

중요도는 AI가 아니라 SQL이 계산한다 (core.rule_catalog.weight 참고).
F3/F4/F5의 기준값(10%, 20%p, 20%)과 점수 구간(5점=높음/2점=중간)은 근거 문헌이
뒷받침하는 값이 아니라 팀 설정값 — core.rule_catalog.is_team_assumption 참고,
샘플 기업으로 돌려보고 조정할 것.

Usage:
    python scripts/run_rules.py --review-date 2026-08-31
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db

HIGH_THRESHOLD = 5
MID_THRESHOLD = 2


def get_weights(cur) -> dict[str, int]:
    cur.execute("SELECT rule_id, weight FROM core.rule_catalog")
    return dict(cur.fetchall())


def clear_review(cur, review_date: str) -> None:
    cur.execute("DELETE FROM mart.change_events WHERE review_date = %s", (review_date,))
    cur.execute("DELETE FROM mart.company_priority WHERE review_date = %s", (review_date,))
    cur.execute("DELETE FROM mart.kpi_daily WHERE review_date = %s", (review_date,))
    cur.execute("DELETE FROM mart.rate_context WHERE review_date = %s", (review_date,))


def rule_f1(cur, review_date: str, weight: int) -> int:
    """F1: 영업이익 직전 >0 → 최신 ≤0 (흑자→적자 전환). 높음."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight,
             account_std_name, old_value, new_value, description)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'financial', 'F1', %(w)s,
               f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               format('영업이익 %%s → %%s (흑자→적자 전환, %%s년)', f.frmtrm_amount, f.thstrm_amount, f.bsns_year)
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'OP_INCOME' AND f.frmtrm_amount > 0 AND f.thstrm_amount <= 0
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_f2(cur, review_date: str, weight: int) -> int:
    """F2: 영업활동현금흐름 직전 >0 → 최신 <0. 높음."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight,
             account_std_name, old_value, new_value, description)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'financial', 'F2', %(w)s,
               f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               format('영업활동현금흐름 %%s → %%s (%%s년)', f.frmtrm_amount, f.thstrm_amount, f.bsns_year)
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'OCF' AND f.frmtrm_amount > 0 AND f.thstrm_amount < 0
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_f3(cur, review_date: str, weight: int) -> int:
    """F3: 매출 YoY <= -10% (팀 설정값, 근거 없음)."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight,
             account_std_name, old_value, new_value, change_rate, description)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'financial', 'F3', %(w)s,
               f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount,
               format('매출액 YoY %%s%%%% (%%s → %%s, %%s년)',
                      round(100 * (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount, 1),
                      f.frmtrm_amount, f.thstrm_amount, f.bsns_year)
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'REVENUE' AND f.frmtrm_amount > 0
          AND (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount <= -0.10
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_f4(cur, review_date: str, weight: int) -> int:
    """F4: 부채비율(부채총계/자본총계) +20%p 이상 (기준값은 팀 설정)."""
    cur.execute(
        """
        WITH ratio AS (
            SELECT l.corp_code, l.bsns_year,
                   l.thstrm_amount AS liab_cur, l.frmtrm_amount AS liab_prev,
                   e.thstrm_amount AS eq_cur, e.frmtrm_amount AS eq_prev
            FROM core.latest_financials l
            JOIN core.latest_financials e
                ON e.corp_code = l.corp_code AND e.bsns_year = l.bsns_year
               AND e.fs_div = l.fs_div AND e.account_std_code = 'TOTAL_EQUITY'
            WHERE l.account_std_code = 'TOTAL_LIAB'
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight,
             account_std_name, old_value, new_value, change_rate, description)
        SELECT r.corp_code, co.corp_name, %(rd)s, 'financial', 'F4', %(w)s,
               '부채비율',
               round(100 * liab_prev / eq_prev, 1),
               round(100 * liab_cur / eq_cur, 1),
               round(100 * liab_cur / eq_cur, 1) - round(100 * liab_prev / eq_prev, 1),
               format('부채비율 %%s%% → %%s%% (%%s년)',
                      round(100 * liab_prev / eq_prev, 1), round(100 * liab_cur / eq_cur, 1), r.bsns_year)
        FROM ratio r
        JOIN core.companies co ON co.corp_code = r.corp_code
        WHERE eq_cur > 0 AND eq_prev > 0
          AND (100 * liab_cur / eq_cur) - (100 * liab_prev / eq_prev) >= 20
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_f5(cur, review_date: str, weight: int) -> int:
    """F5: 차입금(장단기 합산) +20% 이상 (기준값은 팀 설정)."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight,
             account_std_name, old_value, new_value, change_rate, description)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'financial', 'F5', %(w)s,
               f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount,
               format('차입금 %%s → %%s (%%s%%%% 증가, %%s년)',
                      f.frmtrm_amount, f.thstrm_amount,
                      round(100 * (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount, 1), f.bsns_year)
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'BORROWINGS' AND f.frmtrm_amount > 0
          AND (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount >= 0.20
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_d1(cur, review_date: str, weight: int) -> int:
    """D1: 단기차입금 직전 대비 증가."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight,
             account_std_name, old_value, new_value, description)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D1', %(w)s,
               f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               format('단기차입금 %%s → %%s (%%s년)', f.frmtrm_amount, f.thstrm_amount, f.bsns_year)
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'ST_BORROWINGS'
          AND f.frmtrm_amount IS NOT NULL AND f.thstrm_amount > f.frmtrm_amount
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_d2(cur, review_date: str, weight: int) -> int:
    """D2: 유상증자 결정 공시 (마지막 검토일 이후)."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D2', %(w)s, d.rcept_no,
               format('유상증자 결정 공시: %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND d.report_nm_clean ILIKE '%%유상증자%%' AND d.report_nm_clean ILIKE '%%결정%%'
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_d3(cur, review_date: str, weight: int) -> int:
    """D3: 감사의견 비적정 (best-effort 제목 키워드 매칭 — 공시원문 파싱 전까지는 놓칠 수 있음, TODO)."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D3', %(w)s, d.rcept_no,
               format('감사의견 비적정 의심 공시(제목 매칭, 원문 확인 필요): %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND (d.report_nm_clean ILIKE '%%부적정%%' OR d.report_nm_clean ILIKE '%%의견거절%%'
               OR d.report_nm_clean ILIKE '%%한정의견%%')
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_d4(cur, review_date: str, weight: int) -> int:
    """D4: 최대주주 변경. 공시 1건당 +1 적재 -> 같은 기간 2건 이상이면 합산 점수가 자연히 누적(강화)."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D4', %(w)s, d.rcept_no,
               format('최대주주 변경 공시: %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND d.report_nm_clean ILIKE '%%최대주주%%변경%%'
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def rule_d5(cur, review_date: str, weight: int) -> int:
    """D5: 정정공시 (마지막 검토일 이후)."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, weight, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D5', %(w)s, d.rcept_no,
               format('정정공시: %%s%%s', d.report_nm_clean,
                      CASE WHEN d.orig_rcept_no IS NOT NULL THEN format(' (원공시 %%s)', d.orig_rcept_no)
                           ELSE ' (정정 전 공시 없음)' END)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s AND d.is_correction
        """,
        {"rd": review_date, "w": weight},
    )
    return cur.rowcount


def context_rate_vs_borrowings(cur, review_date: str) -> None:
    """CTX1: 기준금리 상승 + 차입금 증가 -> 점수 반영 없이 mart.rate_context에 맥락만 기록."""
    cur.execute(
        """
        SELECT obs_date, value FROM core.rate_observations
        WHERE stat_code = '722Y001' AND item_code = '0101000' AND obs_date <= %s
        ORDER BY obs_date DESC LIMIT 1
        """,
        (review_date,),
    )
    latest = cur.fetchone()
    if not latest:
        print("CTX1: 기준금리 관측치 없음 - skip")
        return
    latest_date, latest_rate = latest

    cur.execute(
        """
        SELECT obs_date, value FROM core.rate_observations
        WHERE stat_code = '722Y001' AND item_code = '0101000' AND obs_date < %s
        ORDER BY obs_date DESC LIMIT 1
        """,
        (latest_date,),
    )
    prior = cur.fetchone()
    prior_rate = prior[1] if prior else None
    direction = "flat"
    if prior_rate is not None:
        if latest_rate > prior_rate:
            direction = "up"
        elif latest_rate < prior_rate:
            direction = "down"

    cur.execute(
        """
        INSERT INTO mart.rate_context (review_date, base_rate, prior_base_rate, rate_direction)
        VALUES (%s, %s, %s, %s)
        """,
        (review_date, latest_rate, prior_rate, direction),
    )
    print(f"CTX1: 기준금리 {prior_rate} -> {latest_rate} ({direction})")


def aggregate_company_priority(cur, review_date: str) -> None:
    cur.execute(
        """
        INSERT INTO mart.company_priority
            (corp_code, corp_name, review_date, priority_level, priority_score,
             change_count, financial_rule_count, disclosure_rule_count)
        SELECT
            corp_code, corp_name, %(rd)s,
            CASE WHEN sum(weight) >= %(high)s THEN 'high'
                 WHEN sum(weight) >= %(mid)s THEN 'mid'
                 ELSE 'low' END,
            sum(weight),
            count(*),
            count(*) FILTER (WHERE change_type = 'financial'),
            count(*) FILTER (WHERE change_type = 'disclosure')
        FROM mart.change_events
        WHERE review_date = %(rd)s
        GROUP BY corp_code, corp_name
        """,
        {"rd": review_date, "high": HIGH_THRESHOLD, "mid": MID_THRESHOLD},
    )
    print(f"mart.company_priority: {cur.rowcount}개 기업 집계")


def aggregate_kpi(cur, review_date: str) -> None:
    cur.execute(
        """
        INSERT INTO mart.kpi_daily
            (review_date, companies_changed, companies_high_priority, new_disclosures, corrections)
        VALUES (
            %(rd)s,
            (SELECT count(*) FROM mart.company_priority WHERE review_date = %(rd)s),
            (SELECT count(*) FROM mart.company_priority WHERE review_date = %(rd)s AND priority_level = 'high'),
            (SELECT count(*) FROM core.disclosures WHERE rcept_dt > %(rd)s),
            (SELECT count(*) FROM core.disclosures WHERE rcept_dt > %(rd)s AND is_correction)
        )
        """,
        {"rd": review_date},
    )
    print("mart.kpi_daily: 1건 집계")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-date", required=True, help="YYYY-MM-DD, '마지막 검토일'")
    args = parser.parse_args()

    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                weights = get_weights(cur)
                clear_review(cur, args.review_date)

                counts = {
                    "F1": rule_f1(cur, args.review_date, weights["F1"]),
                    "F2": rule_f2(cur, args.review_date, weights["F2"]),
                    "F3": rule_f3(cur, args.review_date, weights["F3"]),
                    "F4": rule_f4(cur, args.review_date, weights["F4"]),
                    "F5": rule_f5(cur, args.review_date, weights["F5"]),
                    "D1": rule_d1(cur, args.review_date, weights["D1"]),
                    "D2": rule_d2(cur, args.review_date, weights["D2"]),
                    "D3": rule_d3(cur, args.review_date, weights["D3"]),
                    "D4": rule_d4(cur, args.review_date, weights["D4"]),
                    "D5": rule_d5(cur, args.review_date, weights["D5"]),
                }
                for rule_id, n in counts.items():
                    print(f"{rule_id}: {n}건")

                context_rate_vs_borrowings(cur, args.review_date)
                aggregate_company_priority(cur, args.review_date)
                aggregate_kpi(cur, args.review_date)
        print("OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
