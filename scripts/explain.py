"""FR-05: 공시 변경 해설서 생성 (OpenAI).

범위 (PRD 7번 Must③ / 제약 11번): 변경·정정 공시 중 기업당 핵심 N건(기본 2건)에 한정.
정정공시 원문 + (있으면) 원공시 원문을 섹션 단위로 모델에 주고, 신구 대비 해설 문장을
생성시킨다. 각 문장은 반드시 (rcept_no, section_no, 원문 그대로의 발췌)를 달고 와야 하며,
발췌가 실제 섹션 텍스트의 부분 문자열이 아니면 그 문장은 버린다 (FR-05 AC: 근거 필드가
비어 있거나 원문과 맞지 않는 문장은 제외, 데이터에 없는 수치는 쓰지 않는다).

Usage:
    python scripts/explain.py --review-date 2026-01-01 --limit-per-company 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dart_xml
import db
import opendart_client

load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
DOCS_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "documents"

SYSTEM_PROMPT = """\
당신은 금융 공시 변경 해설가입니다. 아래로 정정공시 원문(그리고 있다면 정정 전 원공시 원문)이
섹션 단위로 주어집니다. 무엇이 어떻게 바뀌었는지 투자 리서치 담당자가 바로 이해할 수 있도록
한국어로 1~3개의 짧은 문장을 작성하세요.

반드시 지켜야 할 규칙:
1. 모든 문장은 주어진 원문 섹션에서 실제로 확인되는 내용만 담아야 한다. 추측하거나 원문에
   없는 수치·사실을 만들어내지 않는다.
2. 각 문장마다 근거를 반드시 함께 제시한다: evidence_rcept_no(그 내용이 나온 공시의 접수번호),
   evidence_section_no(그 섹션 번호), evidence_excerpt(그 섹션 원문에서 "토씨 하나 틀리지 않고
   그대로 복사한" 짧은 발췌, 최대 150자).
3. 원문에서 근거를 찾을 수 없으면 그 문장은 아예 만들지 않는다. 문장 수가 0개여도 된다.
4. 반드시 아래 JSON 형식으로만 응답한다:
{"sentences": [{"text": "...", "evidence_rcept_no": "...", "evidence_section_no": 0, "evidence_excerpt": "..."}]}
"""


def ensure_sections(cur, rcept_no: str) -> list[dict]:
    """core.disclosure_sections에 이미 있으면 그대로, 없으면 원문 받아서 파싱 후 적재."""
    cur.execute(
        "SELECT section_no, section_title, section_text FROM core.disclosure_sections "
        "WHERE rcept_no = %s ORDER BY section_no",
        (rcept_no,),
    )
    rows = cur.fetchall()
    if rows:
        return [{"section_no": r[0], "section_title": r[1], "section_text": r[2]} for r in rows]

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DOCS_DIR / f"{rcept_no}.zip"
    if zip_path.exists():
        zip_bytes = zip_path.read_bytes()
    else:
        try:
            zip_bytes = opendart_client.get_document_original(rcept_no)
        except opendart_client.OpenDartError as e:
            print(f"  원문 다운로드 실패 {rcept_no}: {e}")
            return []
        zip_path.write_bytes(zip_bytes)

    sections = dart_xml.parse_sections(zip_bytes)
    if not sections:
        print(f"  섹션 파싱 결과 없음: {rcept_no}")
        return []

    for s in sections:
        cur.execute(
            """
            INSERT INTO core.disclosure_sections (rcept_no, section_no, section_title, section_text)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (rcept_no, section_no) DO UPDATE SET
                section_title = EXCLUDED.section_title, section_text = EXCLUDED.section_text
            """,
            (rcept_no, s["section_no"], s["section_title"], s["section_text"]),
        )
    return sections


def build_prompt(target: dict, target_sections: list[dict], orig_sections: list[dict]) -> str:
    parts = [f"## 정정공시 (접수번호 {target['rcept_no']}): {target['report_nm_clean']}"]
    for s in target_sections:
        parts.append(f"[섹션 {s['section_no']}] {s['section_title']}\n{s['section_text']}")
    if orig_sections:
        parts.append(f"\n## 정정 전 원공시 (접수번호 {target['orig_rcept_no']})")
        for s in orig_sections:
            parts.append(f"[섹션 {s['section_no']}] {s['section_title']}\n{s['section_text']}")
    else:
        parts.append("\n(정정 전 원공시를 찾지 못했습니다. 정정공시 원문만으로 해설하세요.)")
    return "\n\n".join(parts)


def call_openai(user_prompt: str) -> list[dict]:
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content).get("sentences", [])
    except json.JSONDecodeError:
        print(f"  JSON 파싱 실패, 원본: {content[:300]}")
        return []


def validate_sentence(sentence: dict, sections_by_rcept: dict[str, dict[int, dict]]) -> bool:
    rcept_no = sentence.get("evidence_rcept_no")
    section_no = sentence.get("evidence_section_no")
    excerpt = (sentence.get("evidence_excerpt") or "").strip()
    text = (sentence.get("text") or "").strip()
    if not (rcept_no and section_no is not None and excerpt and text):
        return False
    section = sections_by_rcept.get(rcept_no, {}).get(int(section_no))
    if not section:
        return False
    return excerpt in section["section_text"]


def process_target(cur, target: dict) -> int:
    target_sections = ensure_sections(cur, target["rcept_no"])
    orig_sections = ensure_sections(cur, target["orig_rcept_no"]) if target["orig_rcept_no"] else []
    if not target_sections:
        print(f"  {target['rcept_no']}: 원문 섹션 없음 - skip")
        return 0

    sections_by_rcept = {
        target["rcept_no"]: {s["section_no"]: s for s in target_sections},
    }
    if orig_sections:
        sections_by_rcept[target["orig_rcept_no"]] = {s["section_no"]: s for s in orig_sections}

    prompt = build_prompt(target, target_sections, orig_sections)
    sentences = call_openai(prompt)

    kept = 0
    for i, sent in enumerate(sentences, start=1):
        if not validate_sentence(sent, sections_by_rcept):
            print(f"  REJECTED (근거 불일치): {sent.get('text', '')[:60]}")
            continue
        ev_section = sections_by_rcept[sent["evidence_rcept_no"]][int(sent["evidence_section_no"])]
        cur.execute(
            """
            INSERT INTO mart.explanation_sentences
                (rcept_no, sentence_no, sentence_text, evidence_rcept_no, evidence_section, evidence_excerpt)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (rcept_no, sentence_no) DO UPDATE SET
                sentence_text = EXCLUDED.sentence_text,
                evidence_rcept_no = EXCLUDED.evidence_rcept_no,
                evidence_section = EXCLUDED.evidence_section,
                evidence_excerpt = EXCLUDED.evidence_excerpt
            """,
            (
                target["rcept_no"],
                kept + 1,
                sent["text"],
                sent["evidence_rcept_no"],
                ev_section["section_title"],
                sent["evidence_excerpt"],
            ),
        )
        kept += 1
    print(f"  {target['rcept_no']}: 생성 {len(sentences)}건 중 {kept}건 근거 검증 통과")
    return kept


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-date", required=True)
    parser.add_argument("--limit-per-company", type=int, default=2)
    args = parser.parse_args()

    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY가 .env에 없습니다. 설정 후 다시 실행하세요.")
        sys.exit(1)

    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rcept_no, corp_code, report_nm_clean, orig_rcept_no
                    FROM (
                        SELECT d.*, row_number() OVER (
                            PARTITION BY corp_code ORDER BY rcept_dt DESC
                        ) AS rn
                        FROM core.disclosures d
                        WHERE is_correction AND rcept_dt > %s
                    ) ranked
                    WHERE rn <= %s
                    ORDER BY corp_code, rcept_dt DESC
                    """,
                    (args.review_date, args.limit_per_company),
                )
                targets = [
                    {"rcept_no": r[0], "corp_code": r[1], "report_nm_clean": r[2], "orig_rcept_no": r[3]}
                    for r in cur.fetchall()
                ]
                print(f"대상 정정공시 {len(targets)}건 (기업당 최대 {args.limit_per_company}건)")
                total = 0
                for t in targets:
                    print(f"- {t['rcept_no']} ({t['report_nm_clean']})")
                    total += process_target(cur, t)
        print(f"\nOK, 총 {total}개 해설 문장 저장")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
