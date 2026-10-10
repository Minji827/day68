"""변화 탐지 SQL 룰엔진 v4.0: F1-F8(재무), D1-D9(공시, D6A~D6D 포함), CTX1(기준금리
맥락, 무점수/무등급). core.* -> mart.change_events / mart.company_priority /
mart.kpi_daily / mart.rate_context.

중요도는 AI가 아니라 SQL/Python이 계산한다 (core.rule_catalog.base_score 참고).

v4.0 핵심 원칙(팀 스펙) - "점수를 합산해서 등급을 매기지 않는다. 가장 중요한 단일
사건으로 기업 등급을 정한다." 이 SQL 엔진은 src/rules/rule_engine_v40.py(순수 함수
버전, 이미 구현·검증됨)와 같은 로직을 미러링한다 - 룰별 base_score/밴드 숫자는
rule_engine_v40.py의 THRESHOLDS와 그대로 맞췄다(두 엔진 간 숫자 불일치 방지).

이전 버전(risk_score/positive_score 합산, COMBO_* 가산점, 15점 cap, forced_high)과의
차이:
- 이벤트마다 grade/score(1~10)/is_emergency/direction을 직접 갖고, 집계는 "회사별로
  가장 중요한 이벤트 하나를 window function으로 선출"하는 방식으로 바뀌었다
  (aggregate_company_priority). SUM은 어디에도 없다.
- F1/F2/F3가 양방향(기존엔 악화 방향 위주)으로 바뀌고, F6/F7은 "흑자/적자 유지 중
  증감"으로 재정의(기존 F6=적자→흑자 전환은 새 F1에 흡수, 기존 F7=YoY+50%는 폐기).
  F8(자기자본 양수→0이하, 신설)이 히트하면 F4는 적용제외(해당 리뷰에서 F4 이벤트
  자체를 안 만듦).
- D5(정정공시)는 1~7 단일 스케일이 아니라 TYPO/MINOR_CHANGE/MAJOR_AMOUNT/
  CORE_FINANCIAL 4단계 유형 분류(classify_d5)로 바뀌었다. D6은 D6A~D6D 4개 rule_id로
  분할(D6D=계약해지, 신설). D7(관리종목등)/D8(거래정지등)/D9(영업정지등) 신설.
- F1+F2 동시발생은 COMBO_* 가산점이 아니라 두 이벤트의 is_emergency를 직접
  true로 세팅하는 promote_f1_f2_emergency로 처리.

단순화 지점(src/rules와 다른 부분, README_rule_engine.md에도 명시):
- "판정불가"(NOT_EVALUATED)/"적용제외"(EXCLUDED) 상태값 테이블은 SQL엔진에 안 만든다
  (F8 발생 시 F4는 그냥 이벤트를 안 만드는 것으로 암묵 처리).
- event_group_id 기반 중복이벤트 묶기는 인프라(컬럼+집계 로직)는 있지만(v4.1 2단계),
  D5만 자기 rcept_no를 그룹으로 쓰고 다른 룰과 안 겹쳐서 실질적으로는 거의 항상
  그룹이 1개짜리라 동작이 안 보인다 - 재무제표가 어느 공시(rcept_no)에서 나왔는지
  연결하는 컬럼이 DB에 없어 F-rule과 D5를 그룹으로 못 묶기 때문(팀과 합의된 단순화).
- D1/D2/D6A~D6D의 강도 밴드(구조화된 금액/자산 비율 필요)는 그 구조화 데이터가
  SQL엔진에 적재돼 있지 않아 바이너리(밴드 없음)로 유지 - 기존 엔진과 동일한 수준.
- F5의 강도 밴드는 TOTAL_ASSETS가 적재돼 있지 않아 b_val(차입금증가/총자산) 대신
  증가율(rate) 자체로 밴드를 매긴다(기존 SQL엔진의 F5 로직 그대로 유지).

v4.1 2단계(compare_basis 분리): 모든 이벤트에 compare_basis('YOY'/'CORRECTION'/
'NEW_EVENT')와 사람이 읽는 basis_label을 달았다. F1~F8·D1(재무 데이터 기반)은 YOY,
D5는 CORRECTION, 나머지 D-rule은 NEW_EVENT. MACRO는 아직 아무 룰도 안 씀(3단계
ECOS 작업에서 CTX1이 쓸 예정).

v4.3-1(버그 수정): "NEW_EVENT"(D2~D10, D5의 정정) 판정이 원래 `공시일 > review_date`
(이번 실행 자체의 날짜, 보통 오늘)로 비교했는데, 이러면 "오늘보다 미래인 공시"는
있을 수 없어 오늘 날짜로 실행하는 한 구조적으로 항상 0건이었다(실측: 이스트에이드의
관리종목지정우려·거래정지 공시가 전부 과거 날짜라 D7/D8이 못 잡음 - 키워드는 맞게
매칭되는데 날짜 비교가 막음). sql/007_views.sql의 core.last_review_before(corp_code,
review_date) 함수(기업별 "이번 review_date보다 이전"인 가장 최근 mart.company_priority
검토일)와 비교하도록 바꿨다. 직전 검토 이력이 없는 기업(처음 추가)은
FIRST_REVIEW_LOOKBACK_DAYS로 폴백.

Usage:
    python scripts/run_rules.py --review-date 2026-08-31
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from sections import ensure_sections

D5_HIGH_KEYWORDS = ("부적정", "의견거절", "한정의견", "자본잠식", "상장폐지", "횡령", "배임")
D5_LOW_KEYWORDS = ("단순정정", "단순", "오기재", "오타", "경미한")
D5_CORE_REPORT_KEYWORDS = ("사업보고서", "반기보고서", "분기보고서")

# src/rules/rule_engine_v40.py의 THRESHOLDS["D5_*"]["BASE"]와 동일.
# v4.1: MAJOR_AMOUNT 6->7 상향(핵심 보고서 정정은 "중간" 상위로, 팀 리뷰).
D5_BASE = {"TYPO": 1, "MINOR_CHANGE": 3, "MAJOR_AMOUNT": 7, "CORE_FINANCIAL": 9}
D5_LABEL = {
    "TYPO": "오탈자 수준 정정", "MINOR_CHANGE": "일반 조건 변경 정정",
    "MAJOR_AMOUNT": "중요 금액 변경 정정", "CORE_FINANCIAL": "핵심 재무수치 정정",
}

# SQL이 공유하는 grade CASE 식 - score 컬럼 하나를 참조해 등급을 매긴다.
# src/rules/rule_engine_v40.py::_grade_from_score와 동일(8~10=높음,4~7=중간,1~3=낮음).
_GRADE_CASE_SQL = "CASE WHEN {score} >= 8 THEN '높음' WHEN {score} >= 4 THEN '중간' ELSE '낮음' END"

# v4.3-1: 기업을 처음 분석해서 core.last_review_before()가 NULL(직전 검토 이력 없음)을
# 돌려줄 때 "신규 공시"로 간주할 룩백 윈도우(팀 결정: 분기 보고 주기와 맞춰 90일).
FIRST_REVIEW_LOOKBACK_DAYS = 90

# NEW_EVENT 계열 D-rule이 공통으로 쓰는 "신규 공시" 판정 - 기업별 직전 검토일(없으면
# review_date - 룩백일) 이후에 올라온 공시만 신규로 본다. sql/007_views.sql의
# core.last_review_before(corp_code, review_date)는 "이번 review_date보다 이전" 검토일만
# 돌려주므로(과거 테스트 날짜를 비순차적으로 재실행해도 안전) - FROM절에 core.disclosures
# d가 있어야 하고, 쿼리 파라미터에 rd/lookback을 같이 넘겨야 한다.
_NEW_EVENT_WHERE_SQL = (
    "d.rcept_dt > COALESCE(core.last_review_before(d.corp_code, %(rd)s::date), "
    "%(rd)s::date - make_interval(days => %(lookback)s))"
)


def _grade_from_score(score: int) -> str:
    if score >= 8:
        return "높음"
    if score >= 4:
        return "중간"
    return "낮음"


def get_base_scores(cur) -> dict[str, int]:
    cur.execute("SELECT rule_id, base_score FROM core.rule_catalog")
    return dict(cur.fetchall())


def clear_review(cur, review_date: str) -> None:
    cur.execute("DELETE FROM mart.change_events WHERE review_date = %s", (review_date,))
    cur.execute("DELETE FROM mart.company_priority WHERE review_date = %s", (review_date,))
    cur.execute("DELETE FROM mart.kpi_daily WHERE review_date = %s", (review_date,))
    cur.execute("DELETE FROM mart.rate_context WHERE review_date = %s", (review_date,))


# ---------------------------------------------------------------------------
# F 규칙 (재무) - F1/F2/F3는 양방향, F6/F7 신설, F8 신설
# ---------------------------------------------------------------------------

def rule_f1(cur, review_date: str, base_score: int) -> int:
    """F1: 영업이익 부호 전환(흑자<->적자, 양방향). 매출 대비 전환강도 밴드(5/10/20%)."""
    cur.execute(
        f"""
        WITH rev AS (
            SELECT corp_code, bsns_year, fs_div, thstrm_amount AS revenue
            FROM core.latest_financials WHERE account_std_code = 'REVENUE'
        ),
        chg AS (
            SELECT f.corp_code, co.corp_name, f.account_std_name, f.frmtrm_amount, f.thstrm_amount, f.bsns_year,
                   CASE WHEN r.revenue IS NULL OR r.revenue = 0 THEN NULL
                        ELSE abs(f.thstrm_amount - f.frmtrm_amount) / r.revenue * 100 END AS b_val,
                   CASE WHEN f.frmtrm_amount > 0 THEN 'negative' ELSE 'positive' END AS direction
            FROM core.latest_financials f
            JOIN core.companies co USING (corp_code)
            LEFT JOIN rev r ON r.corp_code = f.corp_code AND r.bsns_year = f.bsns_year AND r.fs_div = f.fs_div
            WHERE f.account_std_code = 'OP_INCOME'
              AND ((f.frmtrm_amount > 0 AND f.thstrm_amount < 0) OR (f.frmtrm_amount < 0 AND f.thstrm_amount > 0))
        ),
        scored AS (
            SELECT *, LEAST(10, GREATEST(1, %(base)s +
                CASE WHEN b_val IS NULL THEN 0 WHEN b_val >= 20 THEN 3 WHEN b_val >= 10 THEN 2
                     WHEN b_val >= 5 THEN 1 ELSE 0 END)) AS score
            FROM chg
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, change_rate, description,
             compare_basis, basis_label)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F1',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, b_val,
               format('[전년 동기] 영업이익 %%s (전년 동기 %%s원 -> 최신 %%s원, %%s년)',
                      CASE WHEN direction = 'negative' THEN '흑자->적자 전환' ELSE '적자->흑자 전환' END,
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'), bsns_year),
               'YOY', '전년 동기 대비'
        FROM scored
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_f2(cur, review_date: str, base_score: int) -> int:
    """F2: 영업활동현금흐름 부호 전환(양방향). F1과 동일한 전환강도 밴드."""
    cur.execute(
        f"""
        WITH rev AS (
            SELECT corp_code, bsns_year, fs_div, thstrm_amount AS revenue
            FROM core.latest_financials WHERE account_std_code = 'REVENUE'
        ),
        chg AS (
            SELECT f.corp_code, co.corp_name, f.account_std_name, f.frmtrm_amount, f.thstrm_amount, f.bsns_year,
                   CASE WHEN r.revenue IS NULL OR r.revenue = 0 THEN NULL
                        ELSE abs(f.thstrm_amount - f.frmtrm_amount) / r.revenue * 100 END AS b_val,
                   CASE WHEN f.frmtrm_amount > 0 THEN 'negative' ELSE 'positive' END AS direction
            FROM core.latest_financials f
            JOIN core.companies co USING (corp_code)
            LEFT JOIN rev r ON r.corp_code = f.corp_code AND r.bsns_year = f.bsns_year AND r.fs_div = f.fs_div
            WHERE f.account_std_code = 'OCF'
              AND ((f.frmtrm_amount > 0 AND f.thstrm_amount < 0) OR (f.frmtrm_amount < 0 AND f.thstrm_amount > 0))
        ),
        scored AS (
            SELECT *, LEAST(10, GREATEST(1, %(base)s +
                CASE WHEN b_val IS NULL THEN 0 WHEN b_val >= 20 THEN 3 WHEN b_val >= 10 THEN 2
                     WHEN b_val >= 5 THEN 1 ELSE 0 END)) AS score
            FROM chg
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, change_rate, description,
             compare_basis, basis_label)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F2',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, b_val,
               format('[전년 동기] 영업활동현금흐름 %%s (전년 동기 %%s원 -> 최신 %%s원, %%s년)',
                      CASE WHEN direction = 'negative' THEN '양수->음수 전환' ELSE '음수->양수 전환' END,
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'), bsns_year),
               'YOY', '전년 동기 대비'
        FROM scored
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def promote_f1_f2_emergency(cur, review_date: str) -> int:
    """긴급확인 조건(스펙 7-7): F1+F2가 같은 기업·같은 리뷰에서 함께 뜨면 둘 다
    긴급확인으로 승격한다. 기존 COMBO_F1_F2 가산점 로직을 대체(가산이 아니라 승격)."""
    cur.execute(
        """
        UPDATE mart.change_events SET grade = '긴급확인', is_emergency = true
        WHERE review_date = %(rd)s AND rule_id IN ('F1', 'F2')
          AND corp_code IN (
              SELECT a.corp_code FROM mart.change_events a
              JOIN mart.change_events b ON a.corp_code = b.corp_code AND a.review_date = b.review_date
              WHERE a.review_date = %(rd)s AND a.rule_id = 'F1' AND b.rule_id = 'F2'
          )
        """,
        {"rd": review_date},
    )
    return cur.rowcount


def rule_f3(cur, review_date: str, base_score: int) -> int:
    """F3: 매출 YoY ±10% 이상(양방향). 밴드 20/30/50%."""
    cur.execute(
        f"""
        WITH chg AS (
            SELECT f.corp_code, co.corp_name, f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
                   f.bsns_year, (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount * 100 AS rate
            FROM core.latest_financials f
            JOIN core.companies co USING (corp_code)
            WHERE f.account_std_code = 'REVENUE' AND f.frmtrm_amount > 0
        ),
        scored AS (
            SELECT *, LEAST(10, GREATEST(1, %(base)s +
                CASE WHEN abs(rate) >= 50 THEN 3 WHEN abs(rate) >= 30 THEN 2
                     WHEN abs(rate) >= 20 THEN 1 ELSE 0 END)) AS score,
                   CASE WHEN rate > 0 THEN 'positive' ELSE 'negative' END AS direction
            FROM chg WHERE abs(rate) >= 10
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, change_rate, description,
             compare_basis, basis_label)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F3',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, rate,
               format('[전년 동기] 매출액 전년 동기 대비 %%s%%%% %%s (%%s원 -> %%s원, %%s년)',
                      round(rate, 1), CASE WHEN rate > 0 THEN '증가' ELSE '감소' END,
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'), bsns_year),
               'YOY', '전년 동기 대비'
        FROM scored
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_f4(cur, review_date: str, base_score: int) -> int:
    """F4: 부채비율 +20%p 이상. F8(자기자본 전환)이 같은 기업에서 뜨면 적용제외
    (자기자본이 음수가 되면 비율 비교 자체가 부적절 - 스펙 9번 표)."""
    cur.execute(
        f"""
        WITH ratio AS (
            SELECT l.corp_code, l.bsns_year,
                   l.thstrm_amount AS liab_cur, l.frmtrm_amount AS liab_prev,
                   e.thstrm_amount AS eq_cur, e.frmtrm_amount AS eq_prev
            FROM core.latest_financials l
            JOIN core.latest_financials e
                ON e.corp_code = l.corp_code AND e.bsns_year = l.bsns_year
               AND e.fs_div = l.fs_div AND e.account_std_code = 'TOTAL_EQUITY'
            WHERE l.account_std_code = 'TOTAL_LIAB'
        ),
        chg AS (
            SELECT corp_code, bsns_year,
                   round(100 * liab_prev / eq_prev, 1) AS ratio_prev,
                   round(100 * liab_cur / eq_cur, 1) AS ratio_cur
            FROM ratio WHERE eq_cur > 0 AND eq_prev > 0
        ),
        scored AS (
            SELECT *, (ratio_cur - ratio_prev) AS delta,
                   LEAST(10, GREATEST(1, %(base)s +
                       CASE WHEN (ratio_cur - ratio_prev) >= 80 THEN 3
                            WHEN (ratio_cur - ratio_prev) >= 40 THEN 2
                            WHEN (ratio_cur - ratio_prev) >= 20 THEN 1 ELSE 0 END)) AS score
            FROM chg WHERE (ratio_cur - ratio_prev) >= 20
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, change_rate, description,
             compare_basis, basis_label)
        SELECT s.corp_code, co.corp_name, %(rd)s, 'financial', 'F4',
               {_GRADE_CASE_SQL.format(score='s.score')}, s.score, false, 'negative',
               '부채비율', s.ratio_prev, s.ratio_cur, s.delta,
               format('[전년 동기] 부채비율 %%s%%%% -> %%s%%%% (%%s년)', s.ratio_prev, s.ratio_cur, s.bsns_year),
               'YOY', '전년 동기 대비'
        FROM scored s
        JOIN core.companies co ON co.corp_code = s.corp_code
        WHERE NOT EXISTS (
            SELECT 1 FROM mart.change_events f8
            WHERE f8.review_date = %(rd)s AND f8.corp_code = s.corp_code AND f8.rule_id = 'F8'
        )
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_f5(cur, review_date: str, base_score: int) -> int:
    """F5: 총차입금 +20% 이상. TOTAL_ASSETS 미적재라 증가율(rate) 자체로 밴드(기존
    SQL엔진 로직 유지 - src/rules의 b_val=증가금액/총자산 밴드와는 다른 단순화)."""
    cur.execute(
        f"""
        WITH chg AS (
            SELECT f.corp_code, co.corp_name, f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
                   f.bsns_year, (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount AS rate
            FROM core.latest_financials f
            JOIN core.companies co USING (corp_code)
            WHERE f.account_std_code = 'BORROWINGS' AND f.frmtrm_amount > 0
        ),
        scored AS (
            SELECT *, LEAST(10, GREATEST(1, %(base)s +
                CASE WHEN rate >= 1.00 THEN 2 WHEN rate >= 0.50 THEN 1 ELSE 0 END)) AS score
            FROM chg WHERE rate >= 0.20
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, change_rate, description,
             compare_basis, basis_label)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F5',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, 'negative',
               account_std_name, frmtrm_amount, thstrm_amount, rate * 100,
               format('[전년 동기] 차입금 %%s원 -> %%s원 (%%s%%%% 증가, %%s년)',
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'),
                      round(100 * rate, 1), bsns_year),
               'YOY', '전년 동기 대비'
        FROM scored
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_f6(cur, review_date: str, base_score: int) -> int:
    """F6(신설): 흑자 유지 중(양수->양수) 영업이익 ±30% 이상 증감."""
    cur.execute(
        f"""
        WITH chg AS (
            SELECT f.corp_code, co.corp_name, f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
                   f.bsns_year, (f.thstrm_amount - f.frmtrm_amount) / f.frmtrm_amount * 100 AS rate
            FROM core.latest_financials f
            JOIN core.companies co USING (corp_code)
            WHERE f.account_std_code = 'OP_INCOME' AND f.frmtrm_amount > 0 AND f.thstrm_amount > 0
        ),
        scored AS (
            SELECT *, LEAST(10, GREATEST(1, %(base)s +
                CASE WHEN abs(rate) >= 80 THEN 3 WHEN abs(rate) >= 50 THEN 2
                     WHEN abs(rate) >= 30 THEN 1 ELSE 0 END)) AS score,
                   CASE WHEN rate > 0 THEN 'positive' ELSE 'negative' END AS direction
            FROM chg WHERE abs(rate) >= 30
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, change_rate, description,
             compare_basis, basis_label)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F6',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, rate,
               format('[전년 동기] 흑자 유지 중 영업이익 %%s%%%% %%s (%%s년)',
                      round(rate, 1), CASE WHEN rate > 0 THEN '증가' ELSE '감소' END, bsns_year),
               'YOY', '전년 동기 대비'
        FROM scored
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_f7(cur, review_date: str, base_score: int) -> int:
    """F7(신설): 적자 유지 중(음수->음수) 영업손실 ±30% 이상 확대/축소."""
    cur.execute(
        f"""
        WITH chg AS (
            SELECT f.corp_code, co.corp_name, f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
                   f.bsns_year, (f.thstrm_amount - f.frmtrm_amount) / abs(f.frmtrm_amount) * 100 AS rate
            FROM core.latest_financials f
            JOIN core.companies co USING (corp_code)
            WHERE f.account_std_code = 'OP_INCOME' AND f.frmtrm_amount < 0 AND f.thstrm_amount < 0
        ),
        scored AS (
            SELECT *, LEAST(10, GREATEST(1, %(base)s +
                CASE WHEN abs(rate) >= 80 THEN 3 WHEN abs(rate) >= 50 THEN 2
                     WHEN abs(rate) >= 30 THEN 1 ELSE 0 END)) AS score,
                   CASE WHEN rate > 0 THEN 'positive' ELSE 'negative' END AS direction
            FROM chg WHERE abs(rate) >= 30
        )
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, change_rate, description,
             compare_basis, basis_label)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F7',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, rate,
               format('[전년 동기] 적자 유지 중 영업손실 %%s (%%s%%%%, %%s년)',
                      CASE WHEN rate > 0 THEN '축소' ELSE '확대' END, round(abs(rate), 1), bsns_year),
               'YOY', '전년 동기 대비'
        FROM scored
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_f8(cur, review_date: str, base_score: int) -> int:
    """F8(신설): 자기자본 상태 전환(양수 -> 0 이하). 긴급확인 7개 조건 중 하나 -
    등장 자체로 항상 긴급확인(score=10 고정, 밴드 없음)."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, description,
             compare_basis, basis_label)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'financial', 'F8', '긴급확인', %(base)s, true,
               'negative', f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               format('[전년 동기] 자기자본 상태 전환(양수 -> 0 이하): 전년 동기 %%s원 -> 최신 %%s원 (%%s년)',
                      to_char(f.frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(f.thstrm_amount, 'FM999,999,999,999,999,999'), f.bsns_year),
               'YOY', '전년 동기 대비'
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'TOTAL_EQUITY' AND f.frmtrm_amount > 0 AND f.thstrm_amount <= 0
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# D 규칙 (공시) - D5는 4단계 재분류, D6은 D6A~D6D로 분할, D7/D8/D9 신설
# ---------------------------------------------------------------------------

def rule_d1(cur, review_date: str, base_score: int) -> int:
    """D1: 단기차입금 직전 대비 증가. 구조화된 금액/총자산 비율 데이터가 없어
    바이너리(밴드 없음) - 기존 엔진과 동일 수준."""
    grade = _grade_from_score(base_score)
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, account_std_name, old_value, new_value, description,
             compare_basis, basis_label)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D1', %(grade)s, %(base)s, false,
               'negative', f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               format('[전년 동기] 단기차입금 %%s원 -> %%s원 (%%s년)',
                      to_char(f.frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(f.thstrm_amount, 'FM999,999,999,999,999,999'), f.bsns_year),
               'YOY', '전년 동기 대비'
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'ST_BORROWINGS'
          AND f.frmtrm_amount IS NOT NULL AND f.thstrm_amount > f.frmtrm_amount
        """,
        {"rd": review_date, "base": base_score, "grade": grade},
    )
    return cur.rowcount


def rule_d2(cur, review_date: str, base_score: int) -> int:
    """D2: 자금조달 결정 공시 (마지막 검토일 이후). 바이너리(밴드 없음).
    v4.1: 유상증자뿐 아니라 전환사채·신주인수권부사채 발행결정도 같은 자금조달 압박
    신호로 보고 조건 확대(팀 리뷰)."""
    grade = _grade_from_score(base_score)
    cur.execute(
        f"""
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description, compare_basis, basis_label)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D2', %(grade)s, %(base)s, false,
               'negative', d.rcept_no, format('[신규 공시] 자금조달 결정 공시: %%s', d.report_nm_clean),
               'NEW_EVENT', '마지막 검토 이후 신규'
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL}
          AND (d.report_nm_clean ILIKE '%%유상증자%%' OR d.report_nm_clean ILIKE '%%전환사채%%'
               OR d.report_nm_clean ILIKE '%%신주인수권부사채%%')
          AND d.report_nm_clean ILIKE '%%결정%%'
        """,
        {"rd": review_date, "base": base_score, "grade": grade, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    return cur.rowcount


def rule_d3(cur, review_date: str, base_score: int) -> int:
    """D3: 감사의견 비적정 (best-effort 제목 키워드 매칭 — 공시원문 파싱 전까지는 놓칠
    수 있음, TODO). 긴급확인 7개 조건 중 하나 - 등장 자체로 항상 긴급확인."""
    cur.execute(
        f"""
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description, compare_basis, basis_label)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D3', '긴급확인', %(base)s, true,
               'negative', d.rcept_no,
               format('[신규 공시] 감사의견 비적정 의심 공시(제목 매칭, 원문 확인 필요): %%s', d.report_nm_clean),
               'NEW_EVENT', '마지막 검토 이후 신규'
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL}
          AND (d.report_nm_clean ILIKE '%%부적정%%' OR d.report_nm_clean ILIKE '%%의견거절%%'
               OR d.report_nm_clean ILIKE '%%한정의견%%')
        """,
        {"rd": review_date, "base": base_score, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    return cur.rowcount


def rule_d4(cur, review_date: str, base_score: int) -> int:
    """D4: 최대주주 변경. v4.0은 "가장 중요한 단일 사건" 방식이라 같은 기간 여러
    건이어도 더 이상 합산되지 않는다(v3.3과의 차이 - 공시 1건당 이벤트 1개는 동일)."""
    grade = _grade_from_score(base_score)
    cur.execute(
        f"""
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description, compare_basis, basis_label)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D4', %(grade)s, %(base)s, false,
               'neutral', d.rcept_no, format('[신규 공시] 최대주주 변경 공시: %%s', d.report_nm_clean),
               'NEW_EVENT', '마지막 검토 이후 신규'
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL}
          AND d.report_nm_clean ILIKE '%%최대주주%%변경%%'
        """,
        {"rd": review_date, "base": base_score, "grade": grade, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    return cur.rowcount


def classify_d5(cur, rcept_no: str, report_nm: str | None) -> tuple[str, int, bool]:
    """D5 정정공시를 4단계로 분류한다(src/rules/rule_engine_v40.py와 동일 체계):
    TYPO/MINOR_CHANGE/MAJOR_AMOUNT/CORE_FINANCIAL. "손익/자기자본 상태가 실제로
    달라졌는지"를 구조화 데이터로 판별할 입력이 SQL엔진엔 없어서, HIGH_KEYWORDS +
    CORE_REPORT_KEYWORDS 동시매칭을 근사치로 써서 CORE_FINANCIAL의 긴급확인 여부를
    판정한다(팀 설정값, src/rules의 structured.changes_pl_or_equity_state 대체).
    원문 섹션을 못 찾으면 가장 낮은 단계(TYPO)로 보수적으로 떨어뜨린다."""
    sections = ensure_sections(cur, rcept_no)
    is_core_report = any(k in (report_nm or "") for k in D5_CORE_REPORT_KEYWORDS)
    if not sections:
        return "TYPO", D5_BASE["TYPO"], False
    text = " ".join(s["section_text"] for s in sections)
    if any(k in text for k in D5_HIGH_KEYWORDS):
        return "CORE_FINANCIAL", D5_BASE["CORE_FINANCIAL"], is_core_report
    if any(k in text for k in D5_LOW_KEYWORDS):
        return "TYPO", D5_BASE["TYPO"], False
    if is_core_report:
        return "MAJOR_AMOUNT", D5_BASE["MAJOR_AMOUNT"], False
    return "MINOR_CHANGE", D5_BASE["MINOR_CHANGE"], False


def rule_d5(cur, review_date: str, _fallback_base_score: int) -> int:
    """D5: 정정공시. 4단계 유형 분류(classify_d5)로 base_score와 긴급확인 여부를
    결정한다(고정점수 아님 - v4.0 스펙)."""
    cur.execute(
        f"""
        SELECT d.rcept_no, d.corp_code, co.corp_name, d.report_nm_clean, d.orig_rcept_no
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL} AND d.is_correction
        """,
        {"rd": review_date, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    rows = cur.fetchall()
    n = 0
    for rcept_no, corp_code, corp_name, report_nm, orig_rcept_no in rows:
        ctype, base_score, is_emergency = classify_d5(cur, rcept_no, report_nm)
        score = base_score
        grade = "긴급확인" if is_emergency else _grade_from_score(score)
        direction = "neutral" if ctype in ("TYPO", "MINOR_CHANGE") else "negative"
        orig_note = f" (원공시 {orig_rcept_no})" if orig_rcept_no else " (정정 전 공시 없음)"
        emergency_note = " - 손익/자기자본 상태 변경 동반(근사치 판정)" if is_emergency else ""
        description = f"[정정] 정정공시: {report_nm} [{D5_LABEL[ctype]}]{emergency_note}{orig_note}"
        cur.execute(
            """
            INSERT INTO mart.change_events
                (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
                 direction, rcept_no, description, compare_basis, basis_label, event_group_id)
            VALUES (%s, %s, %s, 'disclosure', 'D5', %s, %s, %s, %s, %s, %s,
                    'CORRECTION', '정정 전 -> 정정 후', %s)
            """,
            (corp_code, corp_name, review_date, grade, score, is_emergency, direction, rcept_no, description, rcept_no),
        )
        n += 1
    return n


_D6_KEYWORD = {
    "D6A": ("공급계약·수주", "d.report_nm_clean ILIKE '%%수주%%' OR d.report_nm_clean ILIKE '%%공급계약%%'"),
    "D6B": ("시설투자", "d.report_nm_clean ILIKE '%%시설투자%%'"),
    "D6C": ("타법인 출자·인수", "d.report_nm_clean ILIKE '%%타법인%%출자%%'"),
    "D6D": ("계약 해지", "d.report_nm_clean ILIKE '%%계약해지%%' OR d.report_nm_clean ILIKE '%%해지%%'"),
}


def _rule_d6(disc_type: str, direction: str):
    label, where_sql = _D6_KEYWORD[disc_type]

    def _fn(cur, review_date: str, base_score: int) -> int:
        grade = _grade_from_score(base_score)
        cur.execute(
            f"""
            INSERT INTO mart.change_events
                (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
                 direction, rcept_no, description, compare_basis, basis_label)
            SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', %(rid)s, %(grade)s, %(base)s, false,
                   %(dir)s, d.rcept_no, format('[신규 공시] {label} 공시: %%s', d.report_nm_clean),
                   'NEW_EVENT', '마지막 검토 이후 신규'
            FROM core.disclosures d
            JOIN core.companies co USING (corp_code)
            WHERE {_NEW_EVENT_WHERE_SQL} AND ({where_sql})
            """,
            {"rd": review_date, "base": base_score, "grade": grade, "rid": disc_type, "dir": direction,
             "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
        )
        return cur.rowcount

    return _fn


rule_d6a = _rule_d6("D6A", "positive")
rule_d6b = _rule_d6("D6B", "positive")
rule_d6c = _rule_d6("D6C", "positive")
rule_d6d = _rule_d6("D6D", "negative")


def rule_d7(cur, review_date: str, base_score: int) -> int:
    """D7(신설): 관리종목 지정·상장적격성 실질심사·상장폐지. 긴급확인 7개 조건 중
    하나 - 등장 자체로 항상 긴급확인."""
    cur.execute(
        f"""
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description, compare_basis, basis_label)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D7', '긴급확인', %(base)s, true,
               'negative', d.rcept_no,
               format('[신규 공시] 관리종목 지정·상장적격성 실질심사·상장폐지 관련 공시: %%s', d.report_nm_clean),
               'NEW_EVENT', '마지막 검토 이후 신규'
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL}
          AND (d.report_nm_clean ILIKE '%%관리종목%%' OR d.report_nm_clean ILIKE '%%상장적격성%%'
               OR d.report_nm_clean ILIKE '%%상장폐지%%')
        """,
        {"rd": review_date, "base": base_score, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    return cur.rowcount


def rule_d8(cur, review_date: str, base_score: int) -> int:
    """D8(신설): 매매거래정지·회생절차개시신청·부도. 긴급확인 7개 조건 중 하나."""
    cur.execute(
        f"""
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description, compare_basis, basis_label)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D8', '긴급확인', %(base)s, true,
               'negative', d.rcept_no,
               format('[신규 공시] 매매거래정지·회생절차개시신청·부도 관련 공시: %%s', d.report_nm_clean),
               'NEW_EVENT', '마지막 검토 이후 신규'
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL}
          AND (d.report_nm_clean ILIKE '%%거래정지%%' OR d.report_nm_clean ILIKE '%%회생절차%%'
               OR d.report_nm_clean ILIKE '%%부도%%')
        """,
        {"rd": review_date, "base": base_score, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    return cur.rowcount


def rule_d9(cur, review_date: str, base_score: int) -> int:
    """D9(신설): 중대한 영업정지·핵심사업중단. src/rules/rule_engine_v40.py의 실제
    구현과 동일하게 긴급확인 고정이 아니라 base_score(v4.1: 8, 등급 "높음")로 둔다."""
    grade = _grade_from_score(base_score)
    cur.execute(
        f"""
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description, compare_basis, basis_label)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D9', %(grade)s, %(base)s, false,
               'negative', d.rcept_no,
               format('[신규 공시] 중대한 영업정지·핵심사업중단 공시: %%s', d.report_nm_clean),
               'NEW_EVENT', '마지막 검토 이후 신규'
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL}
          AND (d.report_nm_clean ILIKE '%%영업정지%%' OR d.report_nm_clean ILIKE '%%영업중단%%'
               OR d.report_nm_clean ILIKE '%%핵심사업%%')
        """,
        {"rd": review_date, "base": base_score, "grade": grade, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    return cur.rowcount


def rule_d10(cur, review_date: str, base_score: int) -> int:
    """D10(신설, v4.1): 채무보증·담보제공 결정 공시. D1/D4와 같은 밴드 없는 단일 점수
    패턴 - 구조화된 보증금액/자기자본 비율 데이터가 없어 바이너리로 둔다."""
    grade = _grade_from_score(base_score)
    cur.execute(
        f"""
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description, compare_basis, basis_label)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D10', %(grade)s, %(base)s, false,
               'negative', d.rcept_no,
               format('[신규 공시] 채무보증·담보제공 결정 공시: %%s', d.report_nm_clean),
               'NEW_EVENT', '마지막 검토 이후 신규'
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE {_NEW_EVENT_WHERE_SQL}
          AND (d.report_nm_clean ILIKE '%%채무보증%%' OR d.report_nm_clean ILIKE '%%담보제공%%')
        """,
        {"rd": review_date, "base": base_score, "grade": grade, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    return cur.rowcount


def context_rate_vs_borrowings(cur, review_date: str) -> None:
    """CTX1(v4.1 3단계 전면 개정): 기존엔 기준금리 숫자만 1행 보여주고 끝이었는데,
    "검토일 이후 금리가 바뀌었고, 이 기업이 차입 의존도가 높다"를 기업별로 직접
    연결한다 - "그래서 뭐?"가 없다는 팀 리뷰 반영. 점수·등급에는 여전히 반영 안 함
    (compare_basis='MACRO', aggregate_company_priority의 대표사건 선출에서 제외).

    rate_at_review(검토일 시점 기준금리) vs rate_now(적재된 최신 관측치) 비교 -> 같으면
    mart.rate_context에 변경없음 마커 1행만 쓰고 종료. 다르면 대상 기업 선정: 원래는
    BORROWINGS/TOTAL_ASSETS>=0.30 조건도 쓰기로 했으나 TOTAL_ASSETS가 SQL엔진에
    적재돼 있지 않아(알려진 제약, README 참고) 이 조건은 건너뛰고 같은 리뷰에 F5/D1/D10
    이벤트가 있는 기업만으로 대상을 선정한다."""
    cur.execute(
        """
        SELECT value FROM core.rate_observations
        WHERE stat_code = '722Y001' AND item_code = '0101000' AND obs_date <= %s
        ORDER BY obs_date DESC LIMIT 1
        """,
        (review_date,),
    )
    row = cur.fetchone()
    rate_at_review = row[0] if row else None

    cur.execute(
        """
        SELECT value FROM core.rate_observations
        WHERE stat_code = '722Y001' AND item_code = '0101000'
        ORDER BY obs_date DESC LIMIT 1
        """,
    )
    row = cur.fetchone()
    rate_now = row[0] if row else None

    if rate_at_review is None or rate_now is None:
        print("CTX1: 기준금리 관측치 없음 - skip")
        return

    if rate_at_review == rate_now:
        cur.execute(
            """
            INSERT INTO mart.rate_context
                (review_date, corp_code, rate_at_review, rate_now, rate_direction)
            VALUES (%s, '__ALL__', %s, %s, 'flat')
            """,
            (review_date, rate_at_review, rate_now),
        )
        print(f"CTX1: 기준금리 변경 없음 ({rate_now}%)")
        return

    direction = "up" if rate_now > rate_at_review else "down"
    ctx1_direction = "negative" if direction == "up" else "positive"

    print("CTX1: TOTAL_ASSETS 미적재 - 자산대비 차입비율 조건은 건너뛰고 F5/D1/D10 "
          "이벤트 기준으로만 대상 기업 선정")
    cur.execute(
        """
        SELECT DISTINCT corp_code, corp_name FROM mart.change_events
        WHERE review_date = %(rd)s AND rule_id IN ('F5', 'D1', 'D10')
        """,
        {"rd": review_date},
    )
    targets = cur.fetchall()

    for corp_code, corp_name in targets:
        cur.execute(
            """
            SELECT rule_id, change_rate FROM mart.change_events
            WHERE review_date = %(rd)s AND corp_code = %(cc)s AND rule_id IN ('F5', 'D1')
            """,
            {"rd": review_date, "cc": corp_code},
        )
        evs = cur.fetchall()
        f5_rate = next((rate for rid, rate in evs if rid == "F5" and rate is not None), None)
        has_d1 = any(rid == "D1" for rid, _ in evs)

        parts = [f"기준금리 {rate_at_review}% -> {rate_now}% ({'인상' if direction == 'up' else '인하'})"]
        if f5_rate is not None:
            parts.append(f"차입금 전년 동기 대비 +{f5_rate:.1f}%")
        if has_d1:
            parts.append("단기차입 증가 공시")
        exposure_reason = ", ".join(parts) + " -> 이자비용 변화 재확인"

        cur.execute(
            """
            INSERT INTO mart.rate_context
                (review_date, corp_code, rate_at_review, rate_now, rate_direction, exposure_reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (review_date, corp_code, rate_at_review, rate_now, direction, exposure_reason),
        )
        cur.execute(
            """
            INSERT INTO mart.change_events
                (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
                 direction, description, compare_basis, basis_label)
            VALUES (%s, %s, %s, 'context', 'CTX1', '참고', 0, false, %s, %s, 'MACRO',
                    '마지막 검토 이후 기준금리 변경')
            """,
            (corp_code, corp_name, review_date, ctx1_direction, f"[거시] {exposure_reason}"),
        )

    print(f"CTX1: 기준금리 {rate_at_review} -> {rate_now} ({direction}), 대상 기업 {len(targets)}개")


def aggregate_company_priority(cur, review_date: str) -> None:
    """v4.0 핵심: SUM 대신 window function으로 "가장 중요한 단일 사건"을 선출한다
    (스펙 8번 순서 그대로 - ①is_emergency ②grade ③score ④raw_change(abs) ⑤rule_sort).
    additional_important_events는 대표사건을 뺀 나머지 중 grade가 '높음'/'중간'인
    이벤트 개수(등급 산정에는 안 씀 - 정보성, "점수를 더해서 등급을 안 만든다"는
    원칙 유지).

    v4.1 2단계: 대표사건 선출 전에 event_group_id 그룹 내 최고점수만 남기고 나머지는
    is_grouped_duplicate=true로 표시(대표 후보에서 제외, change_count 등 집계에는 그대로
    포함 - "합산은 안 하지만 몇 건 있었는지"는 정보성이라 다름). 현재는 D5만 자기
    rcept_no를 event_group_id로 쓰고 다른 룰과 안 겹쳐서 실질적으로는 그룹이 거의 안
    생긴다(재무제표<->정정공시 연결이 DB에 없어서, README 참고) - 나중에 그 연결이
    생기면 바로 동작하게 인프라만 먼저 둔다.
    reason_text_2/*_event_count도 여기서 같이 계산한다."""
    cur.execute(
        """
        UPDATE mart.change_events ce
        SET is_grouped_duplicate = true
        WHERE ce.review_date = %(rd)s AND ce.event_group_id IS NOT NULL
          AND ce.score < (
              SELECT MAX(ce2.score) FROM mart.change_events ce2
              WHERE ce2.review_date = ce.review_date AND ce2.event_group_id = ce.event_group_id
          )
        """,
        {"rd": review_date},
    )
    cur.execute(
        """
        WITH scored AS (
            SELECT ce.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY ce.corp_code
                       ORDER BY ce.is_emergency DESC,
                                CASE ce.grade WHEN '높음' THEN 2 WHEN '중간' THEN 1 ELSE 0 END DESC,
                                ce.score DESC,
                                abs(ce.change_rate) DESC NULLS LAST,
                                CASE WHEN ce.rule_id LIKE 'F%%' THEN 0 ELSE 1 END, ce.rule_id ASC
                   ) AS rn
            FROM mart.change_events ce
            -- CTX1(change_type='context')은 점수·등급 미반영 참고용이라 대표사건 후보에서
            -- 제외(v4.1 3단계) - 안 그러면 다른 이벤트가 하나도 없는 회사에서 CTX1이
            -- 대표로 뽑혀 grade='참고'가 엉뚱하게 'low'로 매핑되는 문제가 생긴다.
            WHERE ce.review_date = %(rd)s AND NOT ce.is_grouped_duplicate AND ce.change_type <> 'context'
        ),
        counts AS (
            SELECT corp_code, corp_name,
                   count(*) AS change_count,
                   count(*) FILTER (WHERE change_type = 'financial') AS financial_rule_count,
                   count(*) FILTER (WHERE change_type = 'disclosure') AS disclosure_rule_count,
                   count(*) FILTER (WHERE compare_basis = 'YOY') AS yoy_event_count,
                   count(*) FILTER (WHERE compare_basis = 'CORRECTION') AS correction_event_count,
                   count(*) FILTER (WHERE compare_basis = 'NEW_EVENT') AS new_event_count
            FROM mart.change_events WHERE review_date = %(rd)s AND change_type <> 'context'
            GROUP BY corp_code, corp_name
        ),
        additional AS (
            SELECT corp_code, count(*) AS n
            FROM scored WHERE rn > 1 AND grade IN ('높음', '중간')
            GROUP BY corp_code
        ),
        second AS (
            SELECT corp_code, description FROM scored WHERE rn = 2
        ),
        rate AS (
            SELECT corp_code, exposure_reason FROM mart.rate_context
            WHERE review_date = %(rd)s AND corp_code <> '__ALL__'
        )
        INSERT INTO mart.company_priority
            (corp_code, corp_name, review_date, priority_level, priority_score, is_emergency,
             top_rule_id, top_event_id, additional_important_events, reason_text, reason_text_2,
             change_count, financial_rule_count, disclosure_rule_count,
             yoy_event_count, correction_event_count, new_event_count,
             rate_exposure, rate_context_text)
        SELECT
            c.corp_code, c.corp_name, %(rd)s,
            CASE WHEN s.is_emergency THEN 'emergency'
                 WHEN s.grade = '높음' THEN 'high' WHEN s.grade = '중간' THEN 'mid'
                 ELSE 'low' END,
            s.score, s.is_emergency, s.rule_id, s.event_id, COALESCE(a.n, 0), s.description, d2.description,
            c.change_count, c.financial_rule_count, c.disclosure_rule_count,
            c.yoy_event_count, c.correction_event_count, c.new_event_count,
            (r.corp_code IS NOT NULL), r.exposure_reason
        FROM counts c
        JOIN scored s ON s.corp_code = c.corp_code AND s.rn = 1
        LEFT JOIN additional a ON a.corp_code = c.corp_code
        LEFT JOIN second d2 ON d2.corp_code = c.corp_code
        LEFT JOIN rate r ON r.corp_code = c.corp_code
        """,
        {"rd": review_date},
    )
    print(f"mart.company_priority: {cur.rowcount}개 기업 집계")


def aggregate_kpi(cur, review_date: str) -> None:
    # v4.3-1: new_disclosures/corrections도 D-rule과 같은 "기업별 직전 검토일" 기준으로
    # 맞춘다 - 전역 review_date와만 비교하면 화면 KPI 숫자와 실제 rule 결과가 따로 논다.
    cur.execute(
        """
        INSERT INTO mart.kpi_daily
            (review_date, companies_changed, companies_high_priority, new_disclosures, corrections,
             rate_changed, rate_exposed_companies)
        VALUES (
            %(rd)s,
            (SELECT count(*) FROM mart.company_priority WHERE review_date = %(rd)s),
            (SELECT count(*) FROM mart.company_priority WHERE review_date = %(rd)s
                AND priority_level IN ('emergency', 'high')),
            (SELECT count(*) FROM core.disclosures d
                WHERE d.rcept_dt > COALESCE(core.last_review_before(d.corp_code, %(rd)s::date),
                                             %(rd)s::date - make_interval(days => %(lookback)s))),
            (SELECT count(*) FROM core.disclosures d
                WHERE d.rcept_dt > COALESCE(core.last_review_before(d.corp_code, %(rd)s::date),
                                             %(rd)s::date - make_interval(days => %(lookback)s))
                  AND d.is_correction),
            (SELECT EXISTS(SELECT 1 FROM mart.rate_context
                WHERE review_date = %(rd)s AND corp_code <> '__ALL__')),
            (SELECT count(*) FROM mart.rate_context WHERE review_date = %(rd)s AND corp_code <> '__ALL__')
        )
        """,
        {"rd": review_date, "lookback": FIRST_REVIEW_LOOKBACK_DAYS},
    )
    print("mart.kpi_daily: 1건 집계")


#  F8이 F4보다 먼저 와야 한다 - F4가 NOT EXISTS로 F8 발생 여부를 체크해 적용제외를
#  판단하므로, F8 이벤트가 먼저 적재돼 있어야 한다.
RULE_FNS = [
    ("F1", rule_f1), ("F2", rule_f2), ("F8", rule_f8), ("F3", rule_f3), ("F4", rule_f4),
    ("F5", rule_f5), ("F6", rule_f6), ("F7", rule_f7),
    ("D1", rule_d1), ("D2", rule_d2), ("D3", rule_d3), ("D4", rule_d4), ("D5", rule_d5),
    ("D6A", rule_d6a), ("D6B", rule_d6b), ("D6C", rule_d6c), ("D6D", rule_d6d),
    ("D7", rule_d7), ("D8", rule_d8), ("D9", rule_d9), ("D10", rule_d10),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-date", required=True, help="YYYY-MM-DD, '마지막 검토일'")
    args = parser.parse_args()

    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                base_scores = get_base_scores(cur)
                clear_review(cur, args.review_date)

                # F8(자기자본 전환)을 F4보다 먼저 실행해야 F4가 적용제외 여부를
                # NOT EXISTS로 체크할 수 있다.
                counts = {}
                for rule_id, fn in RULE_FNS:
                    counts[rule_id] = fn(cur, args.review_date, base_scores[rule_id])
                for rule_id, n in counts.items():
                    print(f"{rule_id}: {n}건")

                promoted = promote_f1_f2_emergency(cur, args.review_date)
                if promoted:
                    print(f"F1+F2 동시발생 긴급확인 승격: {promoted}건")

                context_rate_vs_borrowings(cur, args.review_date)
                aggregate_company_priority(cur, args.review_date)
                aggregate_kpi(cur, args.review_date)
        print("OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
