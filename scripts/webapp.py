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
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect
import data_export
import db
import explain as explain_mod
import load_core
import load_raw
import opendart_client
import run_rules

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"

app = FastAPI(title="DeltaWatch Local API")


class AnalyzeRequest(BaseModel):
    stock_code: str
    review_date: str = "2026-01-01"
    bgn_de: str = "20250101"


@app.on_event("startup")
def on_startup() -> None:
    conn = db.get_conn()
    try:
        with conn, conn.cursor() as cur:
            for f in sorted((ROOT / "sql").glob("*.sql")):
                cur.execute(f.read_text(encoding="utf-8"))
    finally:
        conn.close()


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
    """CORE/MART에서 이 기업 데이터를 전부 지운다. RAW(원본 API 응답 로그)는 감사 추적용이라
    건드리지 않는다 - 다시 수집하면 동일 raw 레코드가 그대로 재사용된다."""
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
            cur.execute(
                "DELETE FROM core.disclosure_sections WHERE rcept_no IN "
                "(SELECT rcept_no FROM core.disclosures WHERE corp_code = %s)",
                (corp_code,),
            )
            cur.execute("DELETE FROM core.disclosures WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM core.financial_accounts WHERE corp_code = %s", (corp_code,))
            cur.execute("DELETE FROM core.companies WHERE corp_code = %s", (corp_code,))
    finally:
        conn.close()
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


RULE_FNS = [
    ("F1", run_rules.rule_f1), ("F2", run_rules.rule_f2), ("F3", run_rules.rule_f3),
    ("F4", run_rules.rule_f4), ("F5", run_rules.rule_f5),
    ("D1", run_rules.rule_d1), ("D2", run_rules.rule_d2), ("D3", run_rules.rule_d3),
    ("D4", run_rules.rule_d4), ("D5", run_rules.rule_d5),
]


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
    collect.collect_financials(corp_code, ["2024", "2025"])

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
            load_raw.load_disclosure_calls(cur)
            load_raw.load_financial_calls(cur)
            load_raw.load_document_files(cur)
            load_raw.load_ecos_calls(cur)

            step("CORE 변환 중 (정정공시 연결, 계정 표준화)...")
            load_core.load_companies(cur)
            load_core.load_disclosures(cur)
            load_core.load_financial_accounts(cur)
            load_core.load_rate_observations(cur)

            step("변화 탐지 규칙 실행 중...")
            weights = run_rules.get_weights(cur)
            run_rules.clear_review(cur, req.review_date)
            for rule_id, fn in RULE_FNS:
                n = fn(cur, req.review_date, weights[rule_id])
                if n:
                    step(f"  {rule_id}: {n}건")
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
                cur.execute(
                    """
                    SELECT rcept_no, corp_code, report_nm_clean, orig_rcept_no
                    FROM core.disclosures
                    WHERE corp_code = %s AND is_correction AND rcept_dt > %s
                    ORDER BY rcept_dt DESC LIMIT 3
                    """,
                    (corp_code, req.review_date),
                )
                targets = [
                    {"rcept_no": r[0], "corp_code": r[1], "report_nm_clean": r[2], "orig_rcept_no": r[3]}
                    for r in cur.fetchall()
                ]
                total = 0
                for t in targets:
                    total += explain_mod.process_target(cur, t)
                step(f"  AI 해설 {total}개 문장 저장")
            else:
                step("OPENAI_API_KEY 없음 - AI 해설 생략")
    finally:
        conn.close()

    step("완료")
    return {"ok": True, "corp_code": corp_code, "corp_name": corp["corp_name"], "log": log}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8900)
