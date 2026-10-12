"""mart/core 데이터를 대시보드가 쓰는 JSON 형태로 뽑는 공용 함수.

v4.7: 하나였던 export_all()을 둘로 나눴다.

예전엔 /api/dashboard가 DB를 통째로 덤프했다 - 전 기업의 공시 전건, 재무계정 전건,
분기값 전건, AI 해설 원문 발췌 전건, 기준금리 일별 관측치 전건을 한 번에 내려보내고
브라우저가 JS로 필터했다. 기업 2개일 때 이미 107KB였고 그중 화면이 판단에 쓰는 건
8KB(company_priority + change_events + kpi)뿐 - 나머지 93%가 낭비였다. 분석할 때마다,
기업을 지울 때마다 전부 다시 받았으니 기업 수에 정비례해 커진다.

- export_dashboard(): 전체 모니터링 화면이 쓰는 요약만. 기업 수에 비례하지만
  기업당 행 수가 작다(우선순위 1행 + 이벤트 ~10행). 공시는 전건 대신 집계로 대체.
- export_company():   기업 상세 화면이 쓰는 그 기업 데이터만. 화면에서 기업을
  고를 때 그때 받는다.

change_events는 대시보드에만 담는다 - 상세 화면도 쓰지만 같은 배열을 corp_code로
거르면 되고, 양쪽에 넣으면 같은 데이터를 두 번 받게 된다.

버려진 것:
- rate_history(기준금리 일별 643행, 27KB) - 화면에서 아무 데도 안 썼다.
- rate_context - 상세 화면은 company_priority.rate_exposure/rate_context_text를
  쓰고 있어서 역시 안 쓰인다.
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


def _one(cur, query, params=None):
    rows = _rows(cur, query, params)
    return rows[0] if rows else None


def resolve_review_date(cur, review_date: str | None) -> str | None:
    """review_date를 안 주면 mart에 있는 가장 최근 검토일을 쓴다
    (분석을 서로 다른 날짜로 여러 번 돌리면 결과가 섞여 보이는 걸 방지)."""
    if review_date is not None:
        return review_date
    cur.execute("SELECT max(review_date) FROM mart.company_priority")
    row = cur.fetchone()
    return row[0].isoformat() if row and row[0] else None


# 상세 화면 "정정 공시" 패널이 보여주는 최근 건수. 더 보려면 기업 상세로 들어간다.
RECENT_CORRECTION_LIMIT = 8


def export_dashboard(cur, review_date: str | None = None) -> dict:
    """전체 모니터링 화면용 요약."""
    review_date = resolve_review_date(cur, review_date)

    return {
        "review_date": review_date,
        # 검토일 선택 드롭다운용 - mart에 쌓인 과거 검토 회차를 고를 수 있게.
        "review_dates": [
            r["review_date"]
            for r in _rows(
                cur,
                "SELECT DISTINCT review_date FROM mart.company_priority ORDER BY review_date DESC",
            )
        ],
        "companies": _rows(
            cur, "SELECT corp_code, corp_name, stock_code FROM core.companies ORDER BY corp_name"
        ),
        "company_priority": _rows(
            cur,
            "SELECT * FROM mart.company_priority WHERE review_date = %s ORDER BY priority_score DESC",
            (review_date,),
        ),
        "change_events": _rows(
            cur,
            "SELECT * FROM mart.change_events WHERE review_date = %s ORDER BY corp_name, score DESC",
            (review_date,),
        ),
        "kpi_daily": _rows(cur, "SELECT * FROM mart.kpi_daily WHERE review_date = %s", (review_date,)),
        "rule_catalog": _rows(
            cur,
            """
            SELECT rule_id, rule_type, description, base_score, domain, is_emergency_rule
            FROM core.rule_catalog ORDER BY rule_id
            """,
        ),
        # 공시 전건(기업 2개에 200행/47KB) 대신 집계. 화면이 실제로 쓰던 건
        # "기업별 신규/정정 건수"와 "전체 합계" 둘뿐이었다.
        "disclosure_summary": _rows(
            cur,
            """
            SELECT corp_code,
                   count(*) FILTER (WHERE NOT is_correction) AS new_count,
                   count(*) FILTER (WHERE is_correction)     AS correction_count
            FROM core.disclosures GROUP BY corp_code
            """,
        ),
        "disclosure_totals": _one(
            cur,
            """
            SELECT count(*) FILTER (WHERE NOT is_correction) AS new_count,
                   count(*) FILTER (WHERE is_correction)     AS correction_count
            FROM core.disclosures
            """,
        ) or {"new_count": 0, "correction_count": 0},
        "recent_corrections": _rows(
            cur,
            """
            SELECT d.rcept_no, d.corp_code, co.corp_name, d.report_nm_clean,
                   d.rcept_dt, d.orig_rcept_no
            FROM core.disclosures d JOIN core.companies co USING (corp_code)
            WHERE d.is_correction
            ORDER BY d.rcept_dt DESC LIMIT %s
            """,
            (RECENT_CORRECTION_LIMIT,),
        ),
    }


# ---------------------------------------------------------------------------
# 분기별 지표 추이 - 차트와 똑같은 "빈칸 포함" 격자
# ---------------------------------------------------------------------------
# core.quarterly_financials에는 **값이 있는 분기만** 행이 있다. 그대로 CSV로 내보내면
# 미공시 분기가 통째로 빠져서 Power BI 축에 구멍이 생긴다(3개년 12분기를 그려야 하는데
# 9개만 나오는 식). 화면 차트는 JS가 격자를 채워서 "미공시"로 표시하고 있었는데, 그
# 로직이 브라우저에만 있어서 내보내기 쪽은 혜택을 못 봤다.
#
# 그래서 같은 규칙을 여기로 옮긴다 - 3개년 × 4분기 = 12행을 **항상** 만들고, 값이
# 없으면 빈칸 + is_disclosed=false로 둔다. 화면 차트와 CSV가 같은 함수를 쓰진 않지만
# (한쪽은 JS) 규칙은 1:1로 맞췄다: 연도 선정, prior_amount 폴백, 비율 계산 전부 동일.

QUARTERLY_ACCOUNTS = ("REVENUE", "OP_INCOME", "NET_INCOME")


def _margin(num, rev):
    """매출이 0/결측이면 비율이 무의미하므로 None - 0%로 두면 "실제로 0%"와 "계산
    불가"가 섞인다(차트의 marginOf와 동일)."""
    if num is None or rev is None or rev == 0:
        return None
    return float(num) / float(rev) * 100


def quarterly_grid(cur, corp_code: str | None = None, require_revenue: bool = False) -> list[dict]:
    """기업 × 연도 × 분기(1~4) 격자. 미공시 분기도 빈 값으로 한 행씩 채워서 돌려준다.

    require_revenue: 연도를 살릴 기준.
      False(기본, CSV 내보내기용) - 매출/영업이익/당기순이익 중 **아무거나** 있으면
        그 연도를 남긴다. 데이터를 버리지 않는 쪽.
      True(화면 차트와 동일) - 매출이 있는 연도만. 차트는 매출 막대와 "매출 대비"
        비율선만 그리므로 매출이 없는 연도는 4칸 전부 빈 기둥이 된다.

    두 기준이 실제로 갈리는 사례가 있다(실측: 블루산업개발 2026년 - 영업이익은
    공시됐는데 매출이 없어서 차트에서는 그 해가 통째로 빠진다). 내보내기는 그
    영업이익을 버릴 이유가 없어서 기본값을 False로 둔다 - 대신 revenue 칸이 비므로
    Power BI에서도 막대는 안 그려지고 값만 남는다.
    """
    params: tuple = ()
    where = ""
    if corp_code:
        where = "WHERE q.corp_code = %s"
        params = (corp_code,)

    rows = _rows(
        cur,
        f"""
        SELECT q.corp_code, co.corp_name, co.stock_code, q.bsns_year, q.quarter,
               q.account_std_code, q.curr_amount, q.prior_amount
        FROM core.quarterly_financials q
        JOIN core.companies co USING (corp_code)
        {where}
        """,
        params,
    )
    if not rows:
        return []

    # corp -> account -> year -> quarter -> row
    by_corp: dict[str, dict] = {}
    names: dict[str, tuple[str, str | None]] = {}
    for r in rows:
        names[r["corp_code"]] = (r["corp_name"], r["stock_code"])
        # bsns_year까지 키에 넣어야 한다 - 한 기업이 bsns_year 그룹을 2개 가질 수 있어서
        # (derive_quarters가 "그 해 + 전년"을 함께 적재) quarter 번호만으로 인덱싱하면
        # 두 그룹이 같은 키를 놓고 덮어쓴다.
        by_quarter = (by_corp.setdefault(r["corp_code"], {})
                             .setdefault(r["account_std_code"], {})
                             .setdefault(r["bsns_year"], {}))
        by_quarter[r["quarter"]] = r

    out: list[dict] = []
    for corp, by_account in sorted(by_corp.items()):
        corp_name, stock_code = names[corp]

        # 그 기업이 "자기 해"로 적재한 연도들 + 그보다 1년 이른 해(가장 이른 그룹의
        # prior_amount로만 존재). derive_quarters가 "그 해 + 전년"을 함께 적재해서
        # 2개 그룹이면 3개년이 나온다.
        self_years = sorted({y for acct in by_account.values() for y in acct})
        if not self_years:
            continue
        display_years = [str(int(self_years[0]) - 1)] + self_years

        def value_at(account: str, year: str, quarter: int):
            own = by_account.get(account, {}).get(year, {}).get(quarter)
            if own is not None:
                return own["curr_amount"]
            nxt = by_account.get(account, {}).get(str(int(year) + 1), {}).get(quarter)
            return nxt["prior_amount"] if nxt is not None else None

        grid = []
        for year in display_years:
            for quarter in (1, 2, 3, 4):
                vals = {a: value_at(a, year, quarter) for a in QUARTERLY_ACCOUNTS}
                grid.append((year, quarter, vals))

        # 네 분기 모두 비어 있는 연도는 "미공시 4칸"만 만드므로 제외.
        def _has_data(v) -> bool:
            if require_revenue:
                return v["REVENUE"] is not None
            return any(x is not None for x in v.values())

        years_with_data = {y for y, _q, v in grid if _has_data(v)}
        for year, quarter, v in grid:
            if year not in years_with_data:
                continue
            rev, op, net = v["REVENUE"], v["OP_INCOME"], v["NET_INCOME"]
            out.append({
                "corp_code": corp,
                "corp_name": corp_name,
                "stock_code": stock_code,
                "bsns_year": int(year),
                "quarter": quarter,
                # Power BI 축 정렬용 - 문자열 라벨만 있으면 "2025 1Q" < "2025 2Q"가
                # 사전순으로는 맞아도 연도가 섞이면 깨진다.
                "period_sort": int(year) * 10 + quarter,
                "period_label": f"{year} {quarter}Q",
                "quarter_label": f"{quarter}분기",     # 분기끼리 묶어 비교할 때(차트와 같은 축)
                "revenue": rev,
                "op_income": op,
                "net_income": net,
                "op_margin_pct": _margin(op, rev),
                "net_margin_pct": _margin(net, rev),
                # 값이 하나도 없으면 "아직 공시 안 된 분기". 행 자체는 남겨야 Power BI
                # 축에 구멍이 안 생긴다.
                "is_disclosed": any(x is not None for x in (rev, op, net)),
            })
    return out


QUARTERLY_CSV_COLUMNS = [
    "corp_code", "corp_name", "stock_code", "bsns_year", "quarter",
    "period_sort", "period_label", "quarter_label",
    "revenue", "op_income", "net_income",
    "op_margin_pct", "net_margin_pct", "is_disclosed",
]


# 상세 화면의 공시 표는 각각 최근 12건만 보여준다. 전건을 보내면 삼성전자 한 기업이
# 3,602건(683KB)이라 "대시보드가 전부 다 내려보낸다"는 문제를 상세 쪽으로 옮기는 꼴이
# 된다. 건수는 대시보드의 disclosure_summary(서버 집계)가 따로 들고 있다.
DETAIL_DISCLOSURE_LIMIT = 12


def export_company(cur, corp_code: str, review_date: str | None = None) -> dict:
    """기업 상세 화면용 - 그 기업 데이터만.

    change_events는 일부러 뺐다(대시보드 응답에 이미 있고 거기서 corp_code로 거른다).
    """
    review_date = resolve_review_date(cur, review_date)

    def recent_disclosures(is_correction: bool):
        return _rows(
            cur,
            """
            SELECT rcept_no, corp_code, report_nm_clean, rcept_dt,
                   is_correction, orig_rcept_no, match_method
            FROM core.disclosures
            WHERE corp_code = %s AND is_correction = %s
            ORDER BY rcept_dt DESC, rcept_no DESC
            LIMIT %s
            """,
            (corp_code, is_correction, DETAIL_DISCLOSURE_LIMIT),
        )

    return {
        "corp_code": corp_code,
        "review_date": review_date,
        "recent_disclosures": recent_disclosures(False),
        "recent_corrections": recent_disclosures(True),
        # reprt_code='11011'(사업보고서=연간)로 한정한다. 예전엔 전 보고서 유형을
        # 다 내려보내고 화면이 "최신 연도 + CFS 우선"으로만 골랐는데, 같은 연도에
        # 분기/반기 보고서 행이 같이 있으면(실측: 삼성전자 2026 REVENUE가 11012/
        # 11013 × CFS/OFS 4행) 어느 쪽이 뽑힐지가 순서에 달려 있었다. 그 결과 "최신
        # 사업연도" 칸에 분기 누적치가 들어가, 바로 옆 "적용 Rule"(core.latest_financials
        # = reprt_code 11011 기준)과 다른 숫자를 나란히 보여줄 수 있었다.
        "financial_accounts": _rows(
            cur,
            """
            SELECT corp_code, bsns_year, fs_div, account_std_code, account_std_name,
                   thstrm_amount, frmtrm_amount
            FROM core.financial_accounts
            WHERE corp_code = %s AND reprt_code = '11011'
            ORDER BY account_std_code, bsns_year
            """,
            (corp_code,),
        ),
        "quarterly_financials": _rows(
            cur,
            """
            SELECT corp_code, bsns_year, quarter, account_std_code, account_std_name,
                   curr_amount, prior_amount, fs_div
            FROM core.quarterly_financials WHERE corp_code = %s
            ORDER BY account_std_code, quarter
            """,
            (corp_code,),
        ),
        "explanation_sentences": _rows(
            cur,
            """
            SELECT es.rcept_no, es.sentence_no, es.sentence_type, es.sentence_text,
                   es.evidence_rcept_no, es.evidence_section, es.evidence_excerpt,
                   d.corp_code, d.report_nm_clean, d.rcept_dt, d.orig_rcept_no
            FROM mart.explanation_sentences es
            JOIN core.disclosures d ON d.rcept_no = es.rcept_no
            WHERE d.corp_code = %s
            ORDER BY es.rcept_no, es.sentence_no
            """,
            (corp_code,),
        ),
    }
