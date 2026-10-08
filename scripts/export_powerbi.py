"""deltawatch(core/mart) -> powerbi_db(dim_*/fact_*) 동기화.

실시간 스트리밍 복제가 아니라 "이벤트 트리거" 방식 — webapp.py의 /api/analyze가
끝날 때마다 sync_company()가 그 기업의 최신 상태를 powerbi_db에 통째로 다시 써넣는다
(delete-then-insert라 멱등 — 같은 리뷰를 두 번 돌려도 중복 안 쌓임). 기업 삭제는
delete_company() 하나로 끝남 — powerbi_db 쪽 FK가 전부 dim_company에 ON DELETE
CASCADE로 걸려 있어서 dim_company 행 하나만 지우면 연관된 fact_* 전부 같이 지워짐.

팀이 전달한 "같이 전달할 규칙"(코드 텍스트화/접수번호 중복제거/결측 공백/change_pct
vs change_label/Rule 점수만/날짜 포맷)은 Postgres 컬럼 타입(text/numeric/date)과
아래 classify_change()로 이미 반영돼 있다 — CSV로 내보낼 때 추가로 깨질 여지가 적다.

Usage (단독 실행 — 전체 기업 재동기화, 디버깅용):
    .venv/Scripts/python scripts/export_powerbi.py --review-date 2026-10-08
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2.extras

import db
from sections import ensure_sections

# dim_rule.rule_group — v4.0 전면 개정: 긴급확인 고정 룰(F8/D3/D7/D8)은 "즉시상향",
# D5(정정)는 그대로, D6A~D6D/D9는 "제도사건"에 합류, F-rule 전체는 "재무변동". 기존
# "조합가산"(COMBO_*)은 v4.0에 없는 개념이라 더 이상 안 씀 - CHECK 제약(sql/powerbi/
# 001_schema.sql)엔 남아있지만 이제 아무 rule_id도 이 값으로 떨어지지 않는다.
RULE_GROUP = {
    "D3": "즉시상향", "D7": "즉시상향", "D8": "즉시상향", "F8": "즉시상향",
    "D5": "정정",
    "D1": "제도사건", "D2": "제도사건", "D4": "제도사건",
    "D6A": "제도사건", "D6B": "제도사건", "D6C": "제도사건", "D6D": "제도사건", "D9": "제도사건",
    "F1": "재무변동", "F2": "재무변동", "F3": "재무변동", "F4": "재무변동",
    "F5": "재무변동", "F6": "재무변동", "F7": "재무변동",
}
RULE_NAME = {
    "F1": "영업이익 부호 전환", "F2": "영업현금흐름 부호 전환",
    "F3": "매출 증감", "F4": "부채비율 급증", "F5": "차입금 급증",
    "F6": "흑자 유지 중 영업이익 증감", "F7": "적자 유지 중 영업손실 증감",
    "F8": "자기자본 상태 전환",
    "D1": "단기차입금 증가", "D2": "유상증자 결정", "D3": "감사의견 비적정",
    "D4": "최대주주 변경", "D5": "정정공시",
    "D6A": "공급계약·수주", "D6B": "시설투자", "D6C": "타법인 출자·인수", "D6D": "계약 해지",
    "D7": "관리종목등", "D8": "거래정지등", "D9": "영업정지등",
}
FINANCIAL_RULES = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8")
EVENT_RULES = ("D1", "D2", "D4", "D6A", "D6B", "D6C", "D6D", "D7", "D8", "D9")

REPRT_CODE_KEYWORD = {
    "11011": "%사업보고서%",
    "11012": "%반기보고서%",
    "11013": "%1분기보고서%",
    "11014": "%3분기보고서%",
}


def classify_change(prev, curr) -> tuple[float | None, str | None]:
    """팀 규칙 4번: 부호가 걸치거나(흑자<->적자) 양쪽 다 0 이하면 change_pct는 비우고
    change_label(적자확대/적자축소/흑자전환/적자전환)만 채운다. 둘 다 양수일 때만
    평범한 증감률(change_pct)을 쓴다. 결측은 그대로 결측(둘 다 None)."""
    if prev is None or curr is None:
        return None, None
    if prev > 0 and curr > 0:
        return float(curr - prev) / float(prev), None
    if prev > 0 and curr <= 0:
        return None, "적자전환"
    if prev <= 0 and curr > 0:
        return None, "흑자전환"
    if curr < prev:
        return None, "적자확대"
    if curr > prev:
        return None, "적자축소"
    return None, None


def sync_dim_rule(src_cur, dst_cur) -> None:
    # dim_rule.score는 PowerBI 쪽 기존 컬럼명 유지 - v4.0의 base_score를 그대로 담는다
    # (더 이상 risk_score 합산용 weight가 아니라 1~10 "순서 정렬용 보조 척도").
    src_cur.execute("SELECT rule_id, description, base_score FROM core.rule_catalog WHERE rule_type != 'context'")
    rows = []
    for rule_id, description, base_score in src_cur.fetchall():
        group = RULE_GROUP.get(rule_id, "조합가산")
        name = RULE_NAME.get(rule_id, rule_id)
        rows.append((rule_id, group, name, description, base_score))
    psycopg2.extras.execute_values(
        dst_cur,
        """
        INSERT INTO dim_rule (rule_id, rule_group, rule_name, condition_text, score)
        VALUES %s
        ON CONFLICT (rule_id) DO UPDATE SET
            rule_group = EXCLUDED.rule_group, rule_name = EXCLUDED.rule_name,
            condition_text = EXCLUDED.condition_text, score = EXCLUDED.score
        """,
        rows,
    )
    # v4.0 개편으로 사라진 rule_id(COMBO_*, 구 D6)는 더 이상 core.rule_catalog에 없어서
    # 위 upsert가 안 건드린다 - fact_rule_hit에서 아직 참조 중인 게 아니면 지운다(FK라서
    # 참조 중인 건 그대로 둠 - 과거 review_date 재동기화 시 자연히 정리됨).
    current_ids = tuple(r[0] for r in rows)
    dst_cur.execute(
        """
        DELETE FROM dim_rule
        WHERE rule_id NOT IN %s
          AND NOT EXISTS (SELECT 1 FROM fact_rule_hit WHERE fact_rule_hit.rule_id = dim_rule.rule_id)
        """,
        (current_ids,),
    )


def sync_dim_account(src_cur, dst_cur) -> None:
    src_cur.execute(
        "SELECT DISTINCT account_std_code, account_std_name, unit FROM core.financial_accounts"
    )
    rows = src_cur.fetchall()
    if not rows:
        return
    psycopg2.extras.execute_values(
        dst_cur,
        """
        INSERT INTO dim_account (account_id, account_name, unit)
        VALUES %s
        ON CONFLICT (account_id) DO UPDATE SET
            account_name = EXCLUDED.account_name, unit = EXCLUDED.unit
        """,
        rows,
    )


def sync_base_rate(src_cur, dst_cur) -> None:
    src_cur.execute(
        """
        SELECT obs_date, value,
               lag(value) OVER (ORDER BY obs_date) AS prev_value
        FROM core.rate_observations
        WHERE stat_code = '722Y001' AND item_code = '0101000'
        ORDER BY obs_date
        """
    )
    rows = []
    for obs_date, value, prev_value in src_cur.fetchall():
        if prev_value is None:
            direction = None
        elif value > prev_value:
            direction = "인상"
        elif value < prev_value:
            direction = "인하"
        else:
            direction = "동결"
        rows.append((obs_date, value, prev_value, direction))
    if not rows:
        return
    psycopg2.extras.execute_values(
        dst_cur,
        """
        INSERT INTO fact_base_rate (date, rate, prev_rate, direction)
        VALUES %s
        ON CONFLICT (date) DO UPDATE SET
            rate = EXCLUDED.rate, prev_rate = EXCLUDED.prev_rate, direction = EXCLUDED.direction
        """,
        rows,
    )


def sync_dim_company(src_cur, dst_cur, corp_code: str) -> None:
    src_cur.execute(
        "SELECT corp_code, stock_code, corp_name, corp_cls FROM core.companies WHERE corp_code = %s",
        (corp_code,),
    )
    row = src_cur.fetchone()
    if not row:
        return
    corp_code, stock_code, corp_name, corp_cls = row
    market = {"Y": "코스피", "K": "코스닥", "N": "코넥스", "E": "기타"}.get(corp_cls)
    dst_cur.execute(
        """
        INSERT INTO dim_company (corp_code, stock_code, company_name, market, industry)
        VALUES (%s, %s, %s, %s, NULL)
        ON CONFLICT (corp_code) DO UPDATE SET
            stock_code = EXCLUDED.stock_code, company_name = EXCLUDED.company_name,
            market = EXCLUDED.market
        """,
        (corp_code, stock_code, corp_name, market),
    )


def sync_fact_disclosure(src_cur, dst_cur, corp_code: str) -> None:
    src_cur.execute(
        """
        SELECT rcept_no, rcept_dt, report_nm_clean, is_correction, orig_rcept_no
        FROM core.disclosures WHERE corp_code = %s
        """,
        (corp_code,),
    )
    rows = [
        (
            rcept_no, corp_code, rcept_dt, report_nm_clean,
            "정정" if is_correction else "신규", orig_rcept_no,
            f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
        )
        for rcept_no, rcept_dt, report_nm_clean, is_correction, orig_rcept_no in src_cur.fetchall()
    ]
    if not rows:
        return
    psycopg2.extras.execute_values(
        dst_cur,
        """
        INSERT INTO fact_disclosure
            (rcept_no, corp_code, rcept_date, report_name, disclosure_type, orig_rcept_no, url)
        VALUES %s
        ON CONFLICT (rcept_no) DO UPDATE SET
            rcept_date = EXCLUDED.rcept_date, report_name = EXCLUDED.report_name,
            disclosure_type = EXCLUDED.disclosure_type, orig_rcept_no = EXCLUDED.orig_rcept_no,
            url = EXCLUDED.url
        """,
        rows,
    )


def _find_linked_rcept_no(src_cur, corp_code: str, bsns_year: str, reprt_code: str) -> str | None:
    """재무제표는 공시 목록 API와 별도라 rcept_no가 원래 없다. 같은 사업연도의
    사업/반기/분기보고서를 제목으로 best-effort 매칭 (실패하면 None - 추측해서
    채우지 않음, PRD 결측 비추정 원칙)."""
    keyword = REPRT_CODE_KEYWORD.get(reprt_code)
    if not keyword:
        return None
    next_year = str(int(bsns_year) + 1)
    src_cur.execute(
        """
        SELECT rcept_no FROM core.disclosures
        WHERE corp_code = %s AND report_nm_clean ILIKE %s
          AND extract(year FROM rcept_dt)::text IN (%s, %s)
        ORDER BY rcept_dt DESC LIMIT 1
        """,
        (corp_code, keyword, bsns_year, next_year),
    )
    row = src_cur.fetchone()
    return row[0] if row else None


def sync_fact_financial_change(src_cur, dst_cur, corp_code: str) -> None:
    # reprt_code='11011'(사업보고서/연간)만 — 이 fact의 PK가 (corp_code, report_period=연도,
    # account_id, fs_div)라 연도당 한 행만 받는다. scripts/quarterly.py가 생긴 뒤로
    # core.financial_accounts에 분기 보고서(11012/11013 등)도 같은 연도로 같이 들어오는데,
    # 그것까지 여기 섞으면 같은 PK가 두 번 들어와 ON CONFLICT가 깨진다 - 분기 데이터는
    # core.quarterly_financials가 따로 맡는다.
    src_cur.execute(
        """
        SELECT bsns_year, reprt_code, fs_div, account_std_code, thstrm_amount, frmtrm_amount
        FROM core.financial_accounts WHERE corp_code = %s AND reprt_code = '11011'
        """,
        (corp_code,),
    )
    rows = []
    for bsns_year, reprt_code, fs_div, account_std_code, curr, prev in src_cur.fetchall():
        change_pct, change_label = classify_change(prev, curr)
        rcept_no = _find_linked_rcept_no(src_cur, corp_code, bsns_year, reprt_code)
        fs_div_label = "연결" if fs_div == "CFS" else "별도" if fs_div == "OFS" else fs_div
        rows.append(
            (corp_code, bsns_year, rcept_no, account_std_code, fs_div_label, prev, curr, change_pct, change_label)
        )
    if not rows:
        return
    psycopg2.extras.execute_values(
        dst_cur,
        """
        INSERT INTO fact_financial_change
            (corp_code, report_period, rcept_no, account_id, fs_div, prev_value, curr_value, change_pct, change_label)
        VALUES %s
        ON CONFLICT (corp_code, report_period, account_id, fs_div) DO UPDATE SET
            rcept_no = EXCLUDED.rcept_no, prev_value = EXCLUDED.prev_value, curr_value = EXCLUDED.curr_value,
            change_pct = EXCLUDED.change_pct, change_label = EXCLUDED.change_label
        """,
        rows,
    )


def sync_fact_rule_hit_and_priority(src_cur, dst_cur, corp_code: str, review_date: str) -> None:
    dst_cur.execute("DELETE FROM fact_rule_hit WHERE corp_code = %s AND review_date = %s", (corp_code, review_date))
    src_cur.execute(
        """
        SELECT ce.rule_id, ce.rcept_no, d.rcept_dt, ce.old_value, ce.new_value, ce.score
        FROM mart.change_events ce
        LEFT JOIN core.disclosures d ON d.rcept_no = ce.rcept_no
        WHERE ce.corp_code = %s AND ce.review_date = %s
        """,
        (corp_code, review_date),
    )
    hit_rows = [
        (corp_code, review_date, rule_id, rcept_no, event_date, old_value, new_value, score)
        for rule_id, rcept_no, event_date, old_value, new_value, score in src_cur.fetchall()
    ]
    if hit_rows:
        psycopg2.extras.execute_values(
            dst_cur,
            """
            INSERT INTO fact_rule_hit
                (corp_code, review_date, rule_id, rcept_no, event_date, prev_value, curr_value, score)
            VALUES %s
            """,
            hit_rows,
        )

    # v4.0: 점수를 합산하지 않으므로 score_correction/score_event/score_financial은
    # "합산 점수"가 아니라 "그 카테고리에서 가장 높은 점수"로 의미가 바뀐다(참고용 -
    # 등급 자체는 mart.company_priority의 대표사건 1개로 이미 확정돼 있음).
    src_cur.execute(
        """
        SELECT rule_id, score FROM mart.change_events
        WHERE corp_code = %s AND review_date = %s
        """,
        (corp_code, review_date),
    )
    score_correction = score_event = score_financial = 0
    for rule_id, score in src_cur.fetchall():
        if rule_id == "D5":
            score_correction = max(score_correction, score)
        elif rule_id in EVENT_RULES:
            score_event = max(score_event, score)
        elif rule_id in FINANCIAL_RULES:
            score_financial = max(score_financial, score)

    src_cur.execute(
        """
        SELECT priority_score, is_emergency, priority_level
        FROM mart.company_priority WHERE corp_code = %s AND review_date = %s
        """,
        (corp_code, review_date),
    )
    row = src_cur.fetchone()
    if not row:
        return
    priority_score, is_emergency, priority_level = row

    src_cur.execute(
        """
        SELECT count(*) + 1 FROM mart.company_priority
        WHERE review_date = %s
          AND (is_emergency > %s
               OR (is_emergency = %s AND COALESCE(priority_score, -1) > %s))
        """,
        (review_date, is_emergency, is_emergency, priority_score if priority_score is not None else -1),
    )
    rank = src_cur.fetchone()[0]

    dst_cur.execute(
        """
        INSERT INTO fact_priority
            (corp_code, review_date, total_score, score_correction, score_event, score_financial,
             override_flag, grade, rank)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (corp_code, review_date) DO UPDATE SET
            total_score = EXCLUDED.total_score, score_correction = EXCLUDED.score_correction,
            score_event = EXCLUDED.score_event, score_financial = EXCLUDED.score_financial,
            override_flag = EXCLUDED.override_flag, grade = EXCLUDED.grade, rank = EXCLUDED.rank
        """,
        (corp_code, review_date, priority_score, score_correction, score_event, score_financial,
         is_emergency, priority_level, rank),
    )


def sync_fact_disclosure_diff(src_cur, dst_cur, corp_code: str) -> None:
    src_cur.execute(
        "SELECT rcept_no, orig_rcept_no FROM core.disclosures WHERE corp_code = %s AND is_correction AND orig_rcept_no IS NOT NULL",
        (corp_code,),
    )
    for rcept_no, orig_rcept_no in src_cur.fetchall():
        new_sections = ensure_sections(src_cur, rcept_no)
        orig_sections = ensure_sections(src_cur, orig_rcept_no)
        if not new_sections or not orig_sections:
            continue
        orig_by_title = {s["section_title"]: s for s in orig_sections}
        new_by_title = {s["section_title"]: s for s in new_sections}
        seen, ordered_titles = set(), []
        for s in orig_sections + new_sections:
            if s["section_title"] not in seen:
                seen.add(s["section_title"])
                ordered_titles.append(s["section_title"])

        rows = []
        for seq, title in enumerate(ordered_titles, start=1):
            o, n = orig_by_title.get(title), new_by_title.get(title)
            before_text = o["section_text"][:3000] if o else None
            after_text = n["section_text"][:3000] if n else None
            if before_text == after_text:
                continue
            rows.append((rcept_no, orig_rcept_no, seq, title or "(제목 없음)", before_text, after_text))
        if not rows:
            continue
        dst_cur.execute("DELETE FROM fact_disclosure_diff WHERE rcept_no = %s", (rcept_no,))
        psycopg2.extras.execute_values(
            dst_cur,
            """
            INSERT INTO fact_disclosure_diff (rcept_no, orig_rcept_no, item_seq, item_name, before_text, after_text)
            VALUES %s
            """,
            rows,
        )


def sync_fact_explanation(src_cur, dst_cur, corp_code: str) -> None:
    src_cur.execute(
        """
        SELECT es.rcept_no, es.sentence_no, es.sentence_text,
               es.evidence_rcept_no, es.evidence_section, es.evidence_excerpt
        FROM mart.explanation_sentences es
        JOIN core.disclosures d ON d.rcept_no = es.rcept_no
        WHERE d.corp_code = %s
        """,
        (corp_code,),
    )
    rows = src_cur.fetchall()
    if not rows:
        return
    psycopg2.extras.execute_values(
        dst_cur,
        """
        INSERT INTO fact_explanation
            (rcept_no, sentence_seq, sentence, evidence_rcept_no, evidence_location, evidence_excerpt)
        VALUES %s
        ON CONFLICT (rcept_no, sentence_seq) DO UPDATE SET
            sentence = EXCLUDED.sentence, evidence_rcept_no = EXCLUDED.evidence_rcept_no,
            evidence_location = EXCLUDED.evidence_location, evidence_excerpt = EXCLUDED.evidence_excerpt
        """,
        rows,
    )


def sync_company(src_cur, dst_cur, corp_code: str, review_date: str) -> None:
    """한 기업의 현재 상태를 powerbi_db에 통째로 다시 써넣는다 (delete-then-insert
    조합이라 멱등 — webapp.py의 /api/analyze 완료 직후 호출)."""
    sync_dim_rule(src_cur, dst_cur)
    sync_dim_account(src_cur, dst_cur)
    sync_base_rate(src_cur, dst_cur)
    sync_dim_company(src_cur, dst_cur, corp_code)
    sync_fact_disclosure(src_cur, dst_cur, corp_code)
    sync_fact_financial_change(src_cur, dst_cur, corp_code)
    sync_fact_rule_hit_and_priority(src_cur, dst_cur, corp_code, review_date)
    sync_fact_disclosure_diff(src_cur, dst_cur, corp_code)
    sync_fact_explanation(src_cur, dst_cur, corp_code)


def delete_company(dst_cur, corp_code: str) -> None:
    """dim_company 삭제 하나로 끝 — 모든 fact_* FK가 ON DELETE CASCADE라 연쇄 삭제됨."""
    dst_cur.execute("DELETE FROM dim_company WHERE corp_code = %s", (corp_code,))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-date", required=True)
    args = parser.parse_args()

    src_conn = db.get_conn()
    dst_conn = db.get_powerbi_conn()
    try:
        with src_conn, src_conn.cursor() as src_cur, dst_conn, dst_conn.cursor() as dst_cur:
            src_cur.execute("SELECT corp_code FROM core.companies")
            corp_codes = [r[0] for r in src_cur.fetchall()]
            for corp_code in corp_codes:
                print(f"동기화: {corp_code}")
                sync_company(src_cur, dst_cur, corp_code, args.review_date)
        print(f"OK - {len(corp_codes)}개 기업 동기화")
    finally:
        src_conn.close()
        dst_conn.close()


if __name__ == "__main__":
    main()
