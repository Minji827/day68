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
- event_group_id 기반 중복이벤트 묶기는 SQL엔진에 미반영(disclosures 적재 단계에
  연결 컬럼이 없음).
- D1/D2/D6A~D6D의 강도 밴드(구조화된 금액/자산 비율 필요)는 그 구조화 데이터가
  SQL엔진에 적재돼 있지 않아 바이너리(밴드 없음)로 유지 - 기존 엔진과 동일한 수준.
- F5의 강도 밴드는 TOTAL_ASSETS가 적재돼 있지 않아 b_val(차입금증가/총자산) 대신
  증가율(rate) 자체로 밴드를 매긴다(기존 SQL엔진의 F5 로직 그대로 유지).

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
D5_BASE = {"TYPO": 1, "MINOR_CHANGE": 3, "MAJOR_AMOUNT": 6, "CORE_FINANCIAL": 9}
D5_LABEL = {
    "TYPO": "오탈자 수준 정정", "MINOR_CHANGE": "일반 조건 변경 정정",
    "MAJOR_AMOUNT": "중요 금액 변경 정정", "CORE_FINANCIAL": "핵심 재무수치 정정",
}

# SQL이 공유하는 grade CASE 식 - score 컬럼 하나를 참조해 등급을 매긴다.
# src/rules/rule_engine_v40.py::_grade_from_score와 동일(8~10=높음,4~7=중간,1~3=낮음).
_GRADE_CASE_SQL = "CASE WHEN {score} >= 8 THEN '높음' WHEN {score} >= 4 THEN '중간' ELSE '낮음' END"


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
             direction, account_std_name, old_value, new_value, change_rate, description)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F1',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, b_val,
               format('영업이익 %%s (전년 동기 %%s원 -> 최신 %%s원, %%s년)',
                      CASE WHEN direction = 'negative' THEN '흑자->적자 전환' ELSE '적자->흑자 전환' END,
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'), bsns_year)
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
             direction, account_std_name, old_value, new_value, change_rate, description)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F2',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, b_val,
               format('영업활동현금흐름 %%s (전년 동기 %%s원 -> 최신 %%s원, %%s년)',
                      CASE WHEN direction = 'negative' THEN '양수->음수 전환' ELSE '음수->양수 전환' END,
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'), bsns_year)
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
             direction, account_std_name, old_value, new_value, change_rate, description)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F3',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, rate,
               format('매출액 전년 동기 대비 %%s%%%% %%s (%%s원 -> %%s원, %%s년)',
                      round(rate, 1), CASE WHEN rate > 0 THEN '증가' ELSE '감소' END,
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'), bsns_year)
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
             direction, account_std_name, old_value, new_value, change_rate, description)
        SELECT s.corp_code, co.corp_name, %(rd)s, 'financial', 'F4',
               {_GRADE_CASE_SQL.format(score='s.score')}, s.score, false, 'negative',
               '부채비율', s.ratio_prev, s.ratio_cur, s.delta,
               format('부채비율 %%s%%%% -> %%s%%%% (%%s년)', s.ratio_prev, s.ratio_cur, s.bsns_year)
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
             direction, account_std_name, old_value, new_value, change_rate, description)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F5',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, 'negative',
               account_std_name, frmtrm_amount, thstrm_amount, rate * 100,
               format('차입금 %%s원 -> %%s원 (%%s%%%% 증가, %%s년)',
                      to_char(frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(thstrm_amount, 'FM999,999,999,999,999,999'),
                      round(100 * rate, 1), bsns_year)
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
             direction, account_std_name, old_value, new_value, change_rate, description)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F6',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, rate,
               format('흑자 유지 중 영업이익 %%s%%%% %%s (%%s년)',
                      round(rate, 1), CASE WHEN rate > 0 THEN '증가' ELSE '감소' END, bsns_year)
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
             direction, account_std_name, old_value, new_value, change_rate, description)
        SELECT corp_code, corp_name, %(rd)s, 'financial', 'F7',
               {_GRADE_CASE_SQL.format(score='score')}, score, false, direction,
               account_std_name, frmtrm_amount, thstrm_amount, rate,
               format('적자 유지 중 영업손실 %%s (%%s%%%%, %%s년)',
                      CASE WHEN rate > 0 THEN '축소' ELSE '확대' END, round(abs(rate), 1), bsns_year)
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
             direction, account_std_name, old_value, new_value, description)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'financial', 'F8', '긴급확인', %(base)s, true,
               'negative', f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               format('자기자본 상태 전환(양수 -> 0 이하): 전년 동기 %%s원 -> 최신 %%s원 (%%s년)',
                      to_char(f.frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(f.thstrm_amount, 'FM999,999,999,999,999,999'), f.bsns_year)
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
             direction, account_std_name, old_value, new_value, description)
        SELECT f.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D1', %(grade)s, %(base)s, false,
               'negative', f.account_std_name, f.frmtrm_amount, f.thstrm_amount,
               format('단기차입금 %%s원 -> %%s원 (%%s년)',
                      to_char(f.frmtrm_amount, 'FM999,999,999,999,999,999'),
                      to_char(f.thstrm_amount, 'FM999,999,999,999,999,999'), f.bsns_year)
        FROM core.latest_financials f
        JOIN core.companies co USING (corp_code)
        WHERE f.account_std_code = 'ST_BORROWINGS'
          AND f.frmtrm_amount IS NOT NULL AND f.thstrm_amount > f.frmtrm_amount
        """,
        {"rd": review_date, "base": base_score, "grade": grade},
    )
    return cur.rowcount


def rule_d2(cur, review_date: str, base_score: int) -> int:
    """D2: 유상증자 결정 공시 (마지막 검토일 이후). 바이너리(밴드 없음)."""
    grade = _grade_from_score(base_score)
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D2', %(grade)s, %(base)s, false,
               'negative', d.rcept_no, format('유상증자 결정 공시: %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND d.report_nm_clean ILIKE '%%유상증자%%' AND d.report_nm_clean ILIKE '%%결정%%'
        """,
        {"rd": review_date, "base": base_score, "grade": grade},
    )
    return cur.rowcount


def rule_d3(cur, review_date: str, base_score: int) -> int:
    """D3: 감사의견 비적정 (best-effort 제목 키워드 매칭 — 공시원문 파싱 전까지는 놓칠
    수 있음, TODO). 긴급확인 7개 조건 중 하나 - 등장 자체로 항상 긴급확인."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D3', '긴급확인', %(base)s, true,
               'negative', d.rcept_no,
               format('감사의견 비적정 의심 공시(제목 매칭, 원문 확인 필요): %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND (d.report_nm_clean ILIKE '%%부적정%%' OR d.report_nm_clean ILIKE '%%의견거절%%'
               OR d.report_nm_clean ILIKE '%%한정의견%%')
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_d4(cur, review_date: str, base_score: int) -> int:
    """D4: 최대주주 변경. v4.0은 "가장 중요한 단일 사건" 방식이라 같은 기간 여러
    건이어도 더 이상 합산되지 않는다(v3.3과의 차이 - 공시 1건당 이벤트 1개는 동일)."""
    grade = _grade_from_score(base_score)
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D4', %(grade)s, %(base)s, false,
               'neutral', d.rcept_no, format('최대주주 변경 공시: %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND d.report_nm_clean ILIKE '%%최대주주%%변경%%'
        """,
        {"rd": review_date, "base": base_score, "grade": grade},
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
        """
        SELECT d.rcept_no, d.corp_code, co.corp_name, d.report_nm_clean, d.orig_rcept_no
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s AND d.is_correction
        """,
        {"rd": review_date},
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
        description = f"정정공시: {report_nm} [{D5_LABEL[ctype]}]{emergency_note}{orig_note}"
        cur.execute(
            """
            INSERT INTO mart.change_events
                (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
                 direction, rcept_no, description)
            VALUES (%s, %s, %s, 'disclosure', 'D5', %s, %s, %s, %s, %s, %s)
            """,
            (corp_code, corp_name, review_date, grade, score, is_emergency, direction, rcept_no, description),
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
                 direction, rcept_no, description)
            SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', %(rid)s, %(grade)s, %(base)s, false,
                   %(dir)s, d.rcept_no, format('{label} 공시: %%s', d.report_nm_clean)
            FROM core.disclosures d
            JOIN core.companies co USING (corp_code)
            WHERE d.rcept_dt > %(rd)s AND ({where_sql})
            """,
            {"rd": review_date, "base": base_score, "grade": grade, "rid": disc_type, "dir": direction},
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
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D7', '긴급확인', %(base)s, true,
               'negative', d.rcept_no,
               format('관리종목 지정·상장적격성 실질심사·상장폐지 관련 공시: %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND (d.report_nm_clean ILIKE '%%관리종목%%' OR d.report_nm_clean ILIKE '%%상장적격성%%'
               OR d.report_nm_clean ILIKE '%%상장폐지%%')
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_d8(cur, review_date: str, base_score: int) -> int:
    """D8(신설): 매매거래정지·회생절차개시신청·부도. 긴급확인 7개 조건 중 하나."""
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D8', '긴급확인', %(base)s, true,
               'negative', d.rcept_no,
               format('매매거래정지·회생절차개시신청·부도 관련 공시: %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND (d.report_nm_clean ILIKE '%%거래정지%%' OR d.report_nm_clean ILIKE '%%회생절차%%'
               OR d.report_nm_clean ILIKE '%%부도%%')
        """,
        {"rd": review_date, "base": base_score},
    )
    return cur.rowcount


def rule_d9(cur, review_date: str, base_score: int) -> int:
    """D9(신설): 중대한 영업정지·핵심사업중단. src/rules/rule_engine_v40.py의 실제
    구현과 동일하게 긴급확인 고정이 아니라 base_score=7(등급 "중간")로 둔다."""
    grade = _grade_from_score(base_score)
    cur.execute(
        """
        INSERT INTO mart.change_events
            (corp_code, corp_name, review_date, change_type, rule_id, grade, score, is_emergency,
             direction, rcept_no, description)
        SELECT d.corp_code, co.corp_name, %(rd)s, 'disclosure', 'D9', %(grade)s, %(base)s, false,
               'negative', d.rcept_no,
               format('중대한 영업정지·핵심사업중단 공시: %%s', d.report_nm_clean)
        FROM core.disclosures d
        JOIN core.companies co USING (corp_code)
        WHERE d.rcept_dt > %(rd)s
          AND (d.report_nm_clean ILIKE '%%영업정지%%' OR d.report_nm_clean ILIKE '%%영업중단%%'
               OR d.report_nm_clean ILIKE '%%핵심사업%%')
        """,
        {"rd": review_date, "base": base_score, "grade": grade},
    )
    return cur.rowcount


def context_rate_vs_borrowings(cur, review_date: str) -> None:
    """CTX1: 기준금리 상승 + 차입금 증가 -> 점수 반영 없이 mart.rate_context에 맥락만
    기록. v4.0 개편과 무관 - 그대로 유지."""
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
    """v4.0 핵심: SUM 대신 window function으로 "가장 중요한 단일 사건"을 선출한다
    (스펙 8번 순서 그대로 - ①is_emergency ②grade ③score ④raw_change(abs) ⑤rule_sort).
    additional_important_events는 대표사건을 뺀 나머지 중 grade가 '높음'/'중간'인
    이벤트 개수(등급 산정에는 안 씀 - 정보성, "점수를 더해서 등급을 안 만든다"는
    원칙 유지)."""
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
            WHERE ce.review_date = %(rd)s
        ),
        counts AS (
            SELECT corp_code, corp_name,
                   count(*) AS change_count,
                   count(*) FILTER (WHERE change_type = 'financial') AS financial_rule_count,
                   count(*) FILTER (WHERE change_type = 'disclosure') AS disclosure_rule_count
            FROM mart.change_events WHERE review_date = %(rd)s
            GROUP BY corp_code, corp_name
        ),
        additional AS (
            SELECT corp_code, count(*) AS n
            FROM scored WHERE rn > 1 AND grade IN ('높음', '중간')
            GROUP BY corp_code
        )
        INSERT INTO mart.company_priority
            (corp_code, corp_name, review_date, priority_level, priority_score, is_emergency,
             top_rule_id, top_event_id, additional_important_events, reason_text,
             change_count, financial_rule_count, disclosure_rule_count)
        SELECT
            c.corp_code, c.corp_name, %(rd)s,
            CASE WHEN s.is_emergency THEN 'emergency'
                 WHEN s.grade = '높음' THEN 'high' WHEN s.grade = '중간' THEN 'mid'
                 ELSE 'low' END,
            s.score, s.is_emergency, s.rule_id, s.event_id, COALESCE(a.n, 0), s.description,
            c.change_count, c.financial_rule_count, c.disclosure_rule_count
        FROM counts c
        JOIN scored s ON s.corp_code = c.corp_code AND s.rn = 1
        LEFT JOIN additional a ON a.corp_code = c.corp_code
        """,
        {"rd": review_date},
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
            (SELECT count(*) FROM mart.company_priority WHERE review_date = %(rd)s
                AND priority_level IN ('emergency', 'high')),
            (SELECT count(*) FROM core.disclosures WHERE rcept_dt > %(rd)s),
            (SELECT count(*) FROM core.disclosures WHERE rcept_dt > %(rd)s AND is_correction)
        )
        """,
        {"rd": review_date},
    )
    print("mart.kpi_daily: 1건 집계")


#  F8이 F4보다 먼저 와야 한다 - F4가 NOT EXISTS로 F8 발생 여부를 체크해 적용제외를
#  판단하므로, F8 이벤트가 먼저 적재돼 있어야 한다.
RULE_FNS = [
    ("F1", rule_f1), ("F2", rule_f2), ("F8", rule_f8), ("F3", rule_f3), ("F4", rule_f4),
    ("F5", rule_f5), ("F6", rule_f6), ("F7", rule_f7),
    ("D1", rule_d1), ("D2", rule_d2), ("D3", rule_d3), ("D4", rule_d4), ("D5", rule_d5),
    ("D6A", rule_d6a), ("D6B", rule_d6b), ("D6C", rule_d6c), ("D6D", rule_d6d),
    ("D7", rule_d7), ("D8", rule_d8), ("D9", rule_d9),
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
