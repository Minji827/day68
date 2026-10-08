"""DeltaWatch 로컬 웹앱: 종목코드 입력 -> 실시간 수집/분석/AI요약.
claude.ai 아티팩트는 외부 서버 호출이 CSP로 막혀있어 못 하는 부분 -
이건 로컬에서 직접 여는 웹서버라 제약이 없다.

Usage:
    .venv/Scripts/python scripts/webapp.py
    -> http://localhost:8900
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect
import data_export
import db
import explain as explain_mod
import export_powerbi
import init_powerbi_db
import load_core
import load_raw
import opendart_client
import quarterly
import run_rules

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"

app = FastAPI(title="DeltaWatch Local API")


class AnalyzeRequest(BaseModel):
    stock_code: str
    # "검토 기준일" 기본값은 고정된 과거 날짜가 아니라 실시간(요청 시점의 오늘)로 — 요청마다
    # default_factory가 다시 계산하므로 서버를 며칠씩 켜놔도 매번 그날의 "오늘"이 된다.
    review_date: str = Field(default_factory=lambda: date.today().isoformat())
    # 공시 수집 시작일도 고정 날짜(20250101) 대신 롤링 12개월 - 매년 그대로 두면 범위가
    # 계속 넓어져서(삼성전자 같은 대형주는 2년치가 3,600건) 수집량이 끝없이 불어난다.
    bgn_de: str = Field(default_factory=lambda: (date.today() - timedelta(days=365)).strftime("%Y%m%d"))


@app.on_event("startup")
def on_startup() -> None:
    conn = db.get_conn()
    try:
        with conn, conn.cursor() as cur:
            for f in sorted((ROOT / "sql").glob("*.sql")):
                cur.execute(f.read_text(encoding="utf-8"))
    finally:
        conn.close()

    # powerbi_db는 별도 물리 DB라 deltawatch 스키마 적용과 별개로 준비해야 한다.
    # 실패해도(예: 권한 문제) 본 서비스는 계속 돌아가야 하므로 막지 않는다 — 동기화
    # 시점에 다시 실패 로그만 남고 넘어감.
    try:
        init_powerbi_db.ensure_database_exists()
        init_powerbi_db.apply_schema()
        init_powerbi_db.seed_dim_date(date(2024, 1, 1), date(2030, 12, 31))
    except Exception as e:
        print(f"[powerbi_db 준비 실패, 무시하고 계속] {e}")


@app.get("/")
def index():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/companies")
def companies():
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT corp_code, corp_name, stock_code FROM core.companies ORDER BY corp_name")
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


@app.delete("/api/companies/{stock_code}")
def delete_company(stock_code: str):
    """RAW/CORE/MART 전부에서 이 기업 데이터를 완전히 지운다 (감사 추적용으로 RAW를
    남겨두던 이전 방식 폐기 - 재분석 시 되살아나는 버그는 이미 corp_code 스코핑으로
    막혀있어서 더 이상 RAW를 남겨둘 이유가 없고, 사용자가 "완전 삭제"를 원함). 한 번
    지우면 원본 API 응답 로그까지 같이 사라지므로 되돌릴 수 없다 - 다시 보려면
    OpenDART에서 처음부터 다시 수집해야 한다."""
    conn = db.get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT corp_code, corp_name FROM core.companies WHERE stock_code = %s", (stock_code,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"종목코드 '{stock_code}' 기업이 없습니다.")
            corp_code, corp_name = row

            cur.execute(
                "DELETE FROM mart.explanation_sentences WHERE rcept_no IN "
                "(SELECT rcept_no FROM core.disclosures WHERE corp_code = %s)",
                (corp_code,),
            )
            cur.execute("DELETE FROM mart.change_events WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM mart.company_priority WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM core.quarterly_financials WHERE corp_code = %s", (corp_code,))
            # raw.opendart_document_files는 rcept_no로만 찾을 수 있어서, core.disclosures를
            # 지우기 전에(아래) 그 기업 rcept_no로 먼저 지워야 한다.
            cur.execute(
                "DELETE FROM raw.opendart_document_files WHERE rcept_no IN "
                "(SELECT rcept_no FROM core.disclosures WHERE corp_code = %s)",
                (corp_code,),
            )
            cur.execute(
                "DELETE FROM core.disclosure_sections WHERE rcept_no IN "
                "(SELECT rcept_no FROM core.disclosures WHERE corp_code = %s)",
                (corp_code,),
            )
            cur.execute("DELETE FROM core.disclosures WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM core.financial_accounts WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM raw.opendart_disclosure_calls WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM raw.opendart_financial_calls WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM core.companies WHERE corp_code = %s", (corp_code,))
            # raw.opendart_corp_code(전체 상장기업 마스터 목록)와 raw.ecos_rate_calls(기준금리,
            # 기업과 무관한 공용 데이터)는 이 기업만의 데이터가 아니라서 건드리지 않는다.
    finally:
        conn.close()

    # powerbi_db는 dim_company에 ON DELETE CASCADE가 걸려 있어서 이 한 줄로 관련
    # fact_* 전부 같이 지워진다. 물리적으로 분리된 DB라 위 트랜잭션과 원자적으로
    # 묶이진 않음 - 실패해도 deltawatch 쪽 삭제 자체는 이미 끝난 뒤라 막지 않는다.
    try:
        pb_conn = db.get_powerbi_conn()
        try:
            with pb_conn, pb_conn.cursor() as pb_cur:
                export_powerbi.delete_company(pb_cur, corp_code)
        finally:
            pb_conn.close()
    except Exception as e:
        print(f"[powerbi_db 삭제 실패, 무시하고 계속] {corp_code}: {e}")

    return {"ok": True, "corp_code": corp_code, "corp_name": corp_name}


@app.get("/api/dashboard")
def dashboard(review_date: str | None = None):
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            payload = json.dumps(
                data_export.export_all(cur, review_date), ensure_ascii=False, default=data_export._default
            )
            return Response(content=payload, media_type="application/json")
    finally:
        conn.close()


# v4.0: 룰 ID 세트(F8 신설, D6A~D6D 분할, D7~D9 신설)는 run_rules.RULE_FNS가 정의하는
# 순서(F8이 F4보다 먼저 - 적용제외 판정에 필요)를 그대로 따른다.
RULE_FNS = run_rules.RULE_FNS


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    log: list[str] = []

    def step(msg: str) -> None:
        print(msg)
        log.append(msg)

    corp = opendart_client.find_corp_by_stock_code(req.stock_code)
    if not corp:
        raise HTTPException(404, f"종목코드 '{req.stock_code}' 기업을 찾을 수 없습니다.")
    corp_code = corp["corp_code"]
    end_de = date.today().strftime("%Y%m%d")
    step(f"대상 기업: {corp['corp_name']} ({req.stock_code})")

    step("OpenDART 공시목록 수집 중...")
    items = collect.collect_disclosures(corp_code, req.bgn_de, end_de)
    step(f"  {len(items)}건 수집")

    step("재무제표 수집 중...")
    curr_year = date.today().year
    years = [str(curr_year - 1), str(curr_year)]
    collect.collect_financials(corp_code, years)  # 사업보고서(연간) - 기본 reprt_code
    step("분기별 재무제표 수집 중 (1분기/반기/3분기보고서, 작년+올해)...")
    # 작년도 올해와 똑같이 1/2/3분기보고서를 따로 수집해야 한다 - 올해 보고서의
    # "전년동기" 비교 필드만 믿으면, 올해 아직 안 올라온 분기(3·4분기)만큼 작년도
    # 같이 비어버린다. 작년은 이미 다 지난 해라 4개 보고서가 전부 있는 게 정상이므로
    # 작년 몫은 작년 자체 보고서에서 받아야 "작년 건 항상 전부" 나온다.
    for yr in (curr_year, curr_year - 1):
        for reprt_code in ("11013", "11012", "11014"):
            collect.collect_financials(corp_code, [str(yr)], reprt_code=reprt_code)

    if items:
        step("공시원문(최근 5건) 수집 중...")
        collect.collect_documents([it["rcept_no"] for it in items[:5]])

    step("ECOS 기준금리 수집 중...")
    collect.collect_base_rate(req.bgn_de, end_de, cycle="D")

    conn = db.get_conn()
    try:
        with conn, conn.cursor() as cur:
            step("RAW 적재 중...")
            load_raw.load_corp_code(cur)
            load_raw.load_disclosure_calls(cur, corp_code=corp_code)
            load_raw.load_financial_calls(cur, corp_code=corp_code)
            load_raw.load_document_files(cur)
            load_raw.load_ecos_calls(cur)

            step("CORE 변환 중 (정정공시 연결, 계정 표준화)...")
            load_core.load_companies(cur, corp_code=corp_code)
            load_core.load_disclosures(cur, corp_code=corp_code)
            load_core.load_financial_accounts(cur, corp_code=corp_code)
            load_core.load_rate_observations(cur)

            step("분기별 순수값 계산 중 (누적치 차감)...")
            n_q = quarterly.derive_quarters(cur, corp_code, str(curr_year))
            step(f"  core.quarterly_financials: {n_q}건")

            step("변화 탐지 규칙 실행 중...")
            base_scores = run_rules.get_base_scores(cur)
            run_rules.clear_review(cur, req.review_date)
            for rule_id, fn in RULE_FNS:
                n = fn(cur, req.review_date, base_scores[rule_id])
                if n:
                    step(f"  {rule_id}: {n}건")
            promoted = run_rules.promote_f1_f2_emergency(cur, req.review_date)
            if promoted:
                step(f"  F1+F2 동시발생 긴급확인 승격: {promoted}건")
            run_rules.context_rate_vs_borrowings(cur, req.review_date)
            run_rules.aggregate_company_priority(cur, req.review_date)
            run_rules.aggregate_kpi(cur, req.review_date)

            # 변화가 0건이면 aggregate_company_priority가 아예 행을 안 만든다 -
            # "유의미한 변화 없음"으로 명시적으로 표시한다 (PRD 예외 처리, 조용히 사라지면 안 됨).
            cur.execute(
                "SELECT 1 FROM mart.company_priority WHERE corp_code = %s AND review_date = %s",
                (corp_code, req.review_date),
            )
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO mart.company_priority
                        (corp_code, corp_name, review_date, priority_level, priority_score,
                         change_count, financial_rule_count, disclosure_rule_count)
                    VALUES (%s, %s, %s, 'none', 0, 0, 0, 0)
                    ON CONFLICT (corp_code, review_date) DO NOTHING
                    """,
                    (corp_code, corp["corp_name"], req.review_date),
                )
                step(f"  {corp['corp_name']}: 이 검토일 기준 유의미한 변화 없음")

            if os.environ.get("OPENAI_API_KEY"):
                step("AI 해설 생성 중 (OpenAI, 근거 검증 포함)...")
                # 검토기준일 "이후"(>)로 걸면, 검토기준일이 실시간(오늘)일 때 미래 공시가
                # 있을 리 없어 항상 0건이 된다. "그 시점까지의 최근 N건"(<=)으로 바꾸면
                # 검토기준일을 오늘로 두든 과거로 두든 항상 의미 있는 N건이 나온다 -
                # explain.py의 AI_EXPLAIN_LIMIT와 동일한 기준으로 통일.
                cur.execute(
                    """
                    SELECT rcept_no, corp_code, report_nm_clean, orig_rcept_no
                    FROM core.disclosures
                    WHERE corp_code = %s AND is_correction AND rcept_dt <= %s
                    ORDER BY rcept_dt DESC LIMIT %s
                    """,
                    (corp_code, req.review_date, explain_mod.AI_EXPLAIN_LIMIT),
                )
                targets = [
                    {"rcept_no": r[0], "corp_code": r[1], "report_nm_clean": r[2], "orig_rcept_no": r[3]}
                    for r in cur.fetchall()
                ]
                total = 0
                for t in targets:
                    total += explain_mod.process_target(cur, t, req.review_date)
                step(f"  AI 해설 {total}개 문장 저장")
            else:
                step("OPENAI_API_KEY 없음 - AI 해설 생략")

            # powerbi_db 동기화 - 물리적으로 분리된 DB라 완전한 원자성은 없음(여기서
            # 실패해도 위 deltawatch 쪽 분석 결과는 이미 커밋 대상). 실패해도 분석
            # 자체는 성공으로 보고한다.
            try:
                pb_conn = db.get_powerbi_conn()
                try:
                    with pb_conn, pb_conn.cursor() as pb_cur:
                        export_powerbi.sync_company(cur, pb_cur, corp_code, req.review_date)
                    step("  powerbi_db 동기화 완료")
                finally:
                    pb_conn.close()
            except Exception as e:
                step(f"  powerbi_db 동기화 실패(무시하고 계속): {e}")
    finally:
        conn.close()

    step("완료")
    return {"ok": True, "corp_code": corp_code, "corp_name": corp["corp_name"], "log": log}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8900)
