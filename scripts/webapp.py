"""DeltaWatch 로컬 웹앱: 종목코드 입력 -> 실시간 수집/분석/AI요약.
claude.ai 아티팩트는 외부 서버 호출이 CSP로 막혀있어 못 하는 부분 -
이건 로컬에서 직접 여는 웹서버라 제약이 없다.

Usage:
    .venv/Scripts/python scripts/webapp.py
    -> http://localhost:8900
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
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
import schema

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
        # sql/*.sql(현재 스키마, 멱등)은 매 기동마다, sql/migrations/*.sql(일회성
        # 변형)은 meta.schema_migrations에 기록해 한 번만. scripts/schema.py 참고.
        schema.apply_all(conn)
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


def _json(payload) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, default=data_export._default),
        media_type="application/json",
    )


@app.get("/api/dashboard")
def dashboard(review_date: str | None = None):
    """전체 모니터링 화면용 요약. 상세 데이터는 /api/company/{corp_code}로 분리됐다
    - 예전엔 여기서 DB를 통째로 덤프했다(scripts/data_export.py 모듈 주석 참고)."""
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            return _json(data_export.export_dashboard(cur, review_date))
    finally:
        conn.close()


@app.get("/api/company/{corp_code}")
def company_detail(corp_code: str, review_date: str | None = None):
    """기업 상세 화면이 그 기업을 선택한 시점에 받아가는 데이터."""
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM core.companies WHERE corp_code = %s", (corp_code,))
            if not cur.fetchone():
                raise HTTPException(404, f"기업코드 '{corp_code}'를 찾을 수 없습니다.")
            return _json(data_export.export_company(cur, corp_code, review_date))
    finally:
        conn.close()


@app.get("/api/export/quarterly.csv")
def export_quarterly_csv(corp_code: str | None = None):
    """"분기별 지표 추이" 차트 데이터를 Power BI용 CSV로. corp_code 생략 시 전 기업.

    핵심은 **미공시 분기도 행을 만든다**는 것. core.quarterly_financials는 값이 있는
    분기만 들고 있어서 그대로 뽑으면 3개년 12분기 중 9개만 나오고 Power BI 축에
    구멍이 생긴다. data_export.quarterly_grid()가 화면 차트와 같은 규칙으로 격자를
    채우고, 값이 없는 칸은 빈칸 + is_disclosed=false로 둔다.

    팀 CSV 규칙(scripts/export_powerbi_csv.py와 동일): UTF-8 BOM, 결측은 빈칸,
    숫자는 숫자만. corp_code/stock_code는 앞자리 0이 있으므로 Power BI Query
    Editor에서 "텍스트"로 지정할 것.
    """
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            if corp_code:
                cur.execute("SELECT 1 FROM core.companies WHERE corp_code = %s", (corp_code,))
                if not cur.fetchone():
                    raise HTTPException(404, f"기업코드 '{corp_code}'를 찾을 수 없습니다.")
            rows = data_export.quarterly_grid(cur, corp_code)
    finally:
        conn.close()

    # 금액은 원 단위 정수로(Decimal을 float으로 바꾸면 "71915601000000.0"처럼 .0이
    # 붙는다), 비율은 소수 넷째자리까지, 불리언은 소문자로. 결측은 빈칸.
    margin_cols = {"op_margin_pct", "net_margin_pct"}

    def cell(key, v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if key in margin_cols:
            return f"{float(v):.4f}"
        if isinstance(v, Decimal):
            return str(int(v)) if v == v.to_integral_value() else str(v)
        return v

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=data_export.QUARTERLY_CSV_COLUMNS,
                            extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: cell(k, r[k]) for k in data_export.QUARTERLY_CSV_COLUMNS})

    stamp = date.today().strftime("%Y%m%d")
    name = f"quarterly_{corp_code or 'all'}_{stamp}.csv"
    return Response(
        # BOM: Excel이 UTF-8임을 알아보게. Power BI는 BOM 없이도 읽지만 팀 규칙을 따름.
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# v4.0: 룰 ID 세트(F8 신설, D6A~D6D 분할, D7~D9 신설)는 run_rules.RULE_FNS가 정의하는
# 순서(F8이 F4보다 먼저 - 적용제외 판정에 필요)를 그대로 따른다.
RULE_FNS = run_rules.RULE_FNS


def _log(msg: str) -> dict:
    """진행 로그 이벤트 하나. 서버 콘솔에도 그대로 남긴다(기존 step()과 동일)."""
    print(msg)
    return {"type": "log", "message": msg}


def _analyze_steps(req: AnalyzeRequest) -> Iterator[dict]:
    """수집 -> 적재 -> 룰 -> AI해설을 진행하면서 단계마다 이벤트를 하나씩 내보낸다.

    제너레이터인 이유: 예전엔 step() 로그를 리스트에 쌓아뒀다가 전부 끝난 "뒤에"
    한꺼번에 응답으로 돌려줬다. 사용자는 30초~1분 동안 "분석 중..." 한 줄과 빈
    로그창만 보고 있어야 했다 - 서버는 진행 상황을 이미 알고 있는데 안 보내줄
    이유가 없다. /api/analyze/stream(SSE)이 이 이벤트를 실시간으로 흘려보내고,
    기존 POST /api/analyze는 같은 제너레이터를 끝까지 소비해서 예전 형태로 돌려준다.

    이벤트: {"type":"log","message":...} / {"type":"done", ...}
    """
    corp = opendart_client.find_corp_by_stock_code(req.stock_code)
    if not corp:
        raise LookupError(f"종목코드 '{req.stock_code}' 기업을 찾을 수 없습니다.")
    corp_code = corp["corp_code"]
    end_de = date.today().strftime("%Y%m%d")
    yield _log(f"대상 기업: {corp['corp_name']} ({req.stock_code})")

    yield _log("OpenDART 공시목록 수집 중...")
    items = collect.collect_disclosures(corp_code, req.bgn_de, end_de)
    yield _log(f"  {len(items)}건 수집")

    yield _log("재무제표 수집 중...")
    curr_year = date.today().year
    # v4.4-1: 분기 차트를 12분기(3개년) 연속 타임라인으로 보여달라는 요청 - 올해/작년
    # 2개년만으로는 1개년(4분기)밖에 "온전한 전년" 데이터가 안 나와서 재작년까지 받는다.
    years = [str(curr_year - 2), str(curr_year - 1), str(curr_year)]
    collect.collect_financials(corp_code, years)  # 사업보고서(연간) - 기본 reprt_code
    yield _log("분기별 재무제표 수집 중 (1분기/반기/3분기보고서, 최근 3개년)...")
    # 각 연도가 그 해 자체의 1/2/3분기보고서를 따로 수집해야 한다 - 올해 보고서의
    # "전년동기" 비교 필드만 믿으면, 올해 아직 안 올라온 분기(3·4분기)만큼 작년도
    # 같이 비어버린다. 지난 연도는 이미 다 지난 해라 4개 보고서가 전부 있는 게
    # 정상이므로, 그 해 몫은 그 해 자체 보고서에서 받아야 "항상 전부" 나온다.
    for yr in (curr_year, curr_year - 1, curr_year - 2):
        for reprt_code in ("11013", "11012", "11014"):
            collect.collect_financials(corp_code, [str(yr)], reprt_code=reprt_code)

    if items:
        yield _log("공시원문(최근 5건) 수집 중...")
        collect.collect_documents([it["rcept_no"] for it in items[:5]])

    yield _log("ECOS 기준금리 수집 중...")
    collect.collect_base_rate(req.bgn_de, end_de, cycle="D")

    conn = db.get_conn()
    try:
        with conn, conn.cursor() as cur:
            yield _log("RAW 적재 중...")
            load_raw.load_corp_code(cur)
            load_raw.load_disclosure_calls(cur, corp_code=corp_code)
            load_raw.load_financial_calls(cur, corp_code=corp_code)
            load_raw.load_document_files(cur)
            load_raw.load_ecos_calls(cur)

            yield _log("CORE 변환 중 (정정공시 연결, 계정 표준화)...")
            load_core.load_companies(cur, corp_code=corp_code)
            load_core.load_disclosures(cur, corp_code=corp_code)
            load_core.load_financial_accounts(cur, corp_code=corp_code)
            load_core.load_rate_observations(cur)

            yield _log("분기별 순수값 계산 중 (누적치 차감)...")
            # derive_quarters(년)는 "그 해 + 그 해의 전년" 2개년을 뽑는다 - curr_year와
            # curr_year-1 두 번 호출하면 겹치는 중간 연도(curr_year-1) 없이 3개년
            # (curr_year-2 ~ curr_year)이 전부 채워진다(서로 다른 bsns_year 행이라
            # upsert 충돌 없음).
            n_q = quarterly.derive_quarters(cur, corp_code, str(curr_year))
            n_q += quarterly.derive_quarters(cur, corp_code, str(curr_year - 1))
            yield _log(f"  core.quarterly_financials: {n_q}건")

            yield _log("변화 탐지 규칙 실행 중...")
            base_scores = run_rules.get_base_scores(cur)
            run_rules.clear_review(cur, req.review_date)
            for rule_id, fn in RULE_FNS:
                n = fn(cur, req.review_date, base_scores[rule_id])
                if n:
                    yield _log(f"  {rule_id}: {n}건")
            promoted = run_rules.promote_f1_f2_emergency(cur, req.review_date)
            if promoted:
                yield _log(f"  F1+F2 동시발생 긴급확인 승격: {promoted}건")
            run_rules.context_rate_vs_borrowings(cur, req.review_date)
            run_rules.aggregate_company_priority(cur, req.review_date)
            run_rules.aggregate_kpi(cur, req.review_date)

            # 변화 0건이어도 "유의미한 변화 없음"으로 명시 표시해야 한다(PRD 예외 처리,
            # 조용히 사라지면 안 됨) - 이 보장은 run_rules.aggregate_company_priority()
            # 안에서 전체 기업 공통으로 처리한다(이전엔 여기서 기업 1개씩 따로 땜질
            # 했었는데, CLI로 run_rules.py를 직접 돌릴 땐 이 땜질이 없어서 이벤트 0건
            # 기업이 company_priority에서 통째로 빠지는 버그가 있었음 - 한 곳으로 통합).
            cur.execute(
                "SELECT 1 FROM mart.change_events WHERE corp_code = %s AND review_date = %s",
                (corp_code, req.review_date),
            )
            if not cur.fetchone():
                yield _log(f"  {corp['corp_name']}: 이 검토일 기준 유의미한 변화 없음")

            if os.environ.get("OPENAI_API_KEY"):
                yield _log("AI 해설 생성 중 (OpenAI, 근거 검증 포함)...")
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
                # AI 해설은 부가 기능이라 실패해도 분석 본체를 죽이면 안 된다 -
                # process_target_safe가 공시 1건마다 SAVEPOINT를 잡아 실패한 건만
                # 되돌린다(예전엔 OpenAI 429 하나에 수집 결과까지 통째로 롤백됐음).
                total = 0
                failed = 0
                for t in targets:
                    n, err = explain_mod.process_target_safe(cur, t, req.review_date)
                    total += n
                    if err:
                        failed += 1
                        yield _log(f"  AI 해설 실패(건너뜀) {t['rcept_no']}: {err}")
                if failed:
                    yield _log(f"  AI 해설 {total}개 문장 저장 ({failed}건 실패 - 분석 결과는 정상 저장됨)")
                else:
                    yield _log(f"  AI 해설 {total}개 문장 저장")
            else:
                yield _log("OPENAI_API_KEY 없음 - AI 해설 생략")

            # powerbi_db 동기화 - 물리적으로 분리된 DB라 완전한 원자성은 없음(여기서
            # 실패해도 위 deltawatch 쪽 분석 결과는 이미 커밋 대상). 실패해도 분석
            # 자체는 성공으로 보고한다.
            try:
                pb_conn = db.get_powerbi_conn()
                try:
                    with pb_conn, pb_conn.cursor() as pb_cur:
                        export_powerbi.sync_company(cur, pb_cur, corp_code, req.review_date)
                    yield _log("  powerbi_db 동기화 완료")
                finally:
                    pb_conn.close()
            except Exception as e:
                yield _log(f"  powerbi_db 동기화 실패(무시하고 계속): {e}")
    finally:
        conn.close()

    yield _log("완료")
    yield {"type": "done", "corp_code": corp_code,
           "corp_name": corp["corp_name"], "review_date": req.review_date}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    """스트리밍을 못 쓰는 호출자(스크립트 등)를 위한 기존 형태 - 끝까지 돌리고
    로그를 한꺼번에 돌려준다. 화면은 아래 /api/analyze/stream을 쓴다."""
    log: list[str] = []
    done: dict = {}
    try:
        for ev in _analyze_steps(req):
            if ev["type"] == "log":
                log.append(ev["message"])
            elif ev["type"] == "done":
                done = ev
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "corp_code": done.get("corp_code"),
            "corp_name": done.get("corp_name"), "log": log}


@app.get("/api/analyze/stream")
def analyze_stream(
    stock_code: str,
    review_date: str | None = None,
    bgn_de: str | None = None,
):
    """같은 분석을 SSE로 흘려보낸다 - 단계가 끝날 때마다 바로 화면에 찍히도록.

    EventSource가 GET만 지원해서 POST가 아니라 쿼리 파라미터를 받는다.
    예외는 올리지 않고 error 이벤트로 바꿔 보낸다 - 스트림 중간에 raise하면
    이미 200 OK 헤더가 나간 뒤라 브라우저가 "연결 끊김"으로만 보고 사유를 모른다.
    """
    fields = {"stock_code": stock_code}
    if review_date:
        fields["review_date"] = review_date
    if bgn_de:
        fields["bgn_de"] = bgn_de
    req = AnalyzeRequest(**fields)

    def gen():
        try:
            for ev in _analyze_steps(req):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except LookupError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"[analyze 실패] {msg}")
            yield f"data: {json.dumps({'type': 'error', 'message': msg}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        # 프록시/버퍼가 중간에 끼면 스트리밍이 의미가 없어진다.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8900)
