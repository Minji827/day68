"""mart/core 데이터를 대시보드가 쓰는 JSON 형태로 뽑는 공용 함수.
scripts/webapp.py의 API와 (과거) 수동 export 스크립트가 공유한다.
"""
from __future__ import annotations

import datetime
import decimal


def _default(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    raise TypeError(str(type(o)))


def _rows(cur, query, params=None):
    cur.execute(query, params or ())
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def export_all(cur, review_date: str | None = None) -> dict:
    """review_date를 안 주면 mart에 있는 가장 최근 검토일 기준으로 보여준다
    (분석을 서로 다른 날짜로 여러 번 돌리면 결과가 섞여 보이는 걸 방지)."""
    if review_date is None:
        cur.execute("SELECT max(review_date) FROM mart.company_priority")
        row = cur.fetchone()
        review_date = row[0].isoformat() if row and row[0] else None

    return {
        "review_date": review_date,
        "companies": _rows(cur, "SELECT corp_code, corp_name, stock_code FROM core.companies"),
        "company_priority": _rows(
            cur,
            "SELECT * FROM mart.company_priority WHERE review_date = %s ORDER BY priority_score DESC",
            (review_date,),
        ),
        "change_events": _rows(
            cur,
            "SELECT * FROM mart.change_events WHERE review_date = %s ORDER BY corp_name, weight DESC",
            (review_date,),
        ),
        "kpi_daily": _rows(cur, "SELECT * FROM mart.kpi_daily WHERE review_date = %s", (review_date,)),
        "rate_context": _rows(cur, "SELECT * FROM mart.rate_context WHERE review_date = %s", (review_date,)),
        "rate_history": _rows(
            cur,
            "SELECT obs_date, value FROM core.rate_observations WHERE stat_code='722Y001' ORDER BY obs_date",
        ),
        "rule_catalog": _rows(cur, "SELECT rule_id, rule_type, description, weight FROM core.rule_catalog ORDER BY rule_id"),
        "disclosures": _rows(
            cur,
            """
            SELECT d.rcept_no, d.corp_code, co.corp_name, co.stock_code, d.report_nm_clean,
                   d.rcept_dt, d.is_correction, d.orig_rcept_no, d.match_method
            FROM core.disclosures d JOIN core.companies co USING (corp_code)
            ORDER BY d.rcept_dt
            """,
        ),
        "financial_accounts": _rows(
            cur,
            """
            SELECT corp_code, bsns_year, fs_div, account_std_code, account_std_name,
                   thstrm_amount, frmtrm_amount
            FROM core.financial_accounts
            ORDER BY corp_code, account_std_code, bsns_year
            """,
        ),
        "explanation_sentences": _rows(
            cur,
            """
            SELECT es.rcept_no, es.sentence_no, es.sentence_text, es.evidence_rcept_no,
                   es.evidence_section, es.evidence_excerpt,
                   d.corp_code, d.report_nm_clean, d.rcept_dt, d.orig_rcept_no
            FROM mart.explanation_sentences es
            JOIN core.disclosures d ON d.rcept_no = es.rcept_no
            ORDER BY d.corp_code, es.rcept_no, es.sentence_no
            """,
        ),
    }
