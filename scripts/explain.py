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

import db
from sections import ensure_sections

load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# PRD 기본값("핵심 N건") 그대로 - webapp.py의 /api/analyze도 이 상수를 그대로 가져다 쓴다.
# 예전엔 둘이 따로 3/2로 어긋나 있었음 - 하나로 통일.
AI_EXPLAIN_LIMIT = 2

# v4.1 5단계: "①무엇이 바뀌었나(change)"만 LLM이 쓰고, "②왜 재검토 대상인가(사유)"와
# "③추가 확인사항(확인)"은 더 이상 LLM이 안 쓴다 - rule_catalog.description/
# core.rule_checklist에서 결정론적으로 조립한다(아래 process_target). "점수·등급·판정은
# 코드가 계산하고 LLM은 계산하지 않는다"를 AI 해설에도 동일 적용(팀 리뷰) - 예전엔
# meaning/check도 LLM이 자유 생성해서 Rule 목록을 "참고"만 했을 뿐 매번 다른 문장이
# 나올 수 있었다.
SYSTEM_PROMPT = """\
당신은 금융 공시 변경 해설가입니다. 아래로 정정공시 원문(그리고 있다면 정정 전 원공시 원문)이
섹션 단위로 주어집니다. "무엇이 바뀌었는지"만 1~2개 항목으로 작성하세요 - 이유나 확인사항은
쓰지 않습니다(그건 다른 곳에서 결정론적으로 채웁니다).

각 항목(change)은 원문에서 실제로 확인되는 사실만 적습니다. 추측하거나 원문에 없는 수치·사실을
만들어내지 않는다. 반드시 근거를 함께 제시: evidence_rcept_no(그 내용이 나온 공시의 접수번호),
evidence_section_no(섹션 번호), evidence_excerpt(그 섹션 원문에서 "토씨 하나 틀리지 않고 그대로
복사한" 짧은 발췌, 최대 150자). 근거를 찾을 수 없으면 그 항목 자체를 만들지 않는다.

반드시 아래 JSON 형식으로만 응답한다 (근거 없으면 items가 빈 배열이어도 됨):
{"items": [{"change": "...",
            "evidence_rcept_no": "...", "evidence_section_no": 0, "evidence_excerpt": "..."}]}
"""


def fired_rules(cur, corp_code: str, review_date: str) -> list[dict]:
    """이 기업이 이번 검토에서 실제로 받은 Rule 목록(점수 높은 순). v4.1부터는 LLM
    프롬프트가 아니라 process_target이 "사유"/"확인" 문장을 결정론적으로 조립할 때
    쓴다 - rules[0]이 대표 Rule(= company_priority.top_rule_id와 같은 선정 기준:
    가장 점수가 높은 것)."""
    cur.execute(
        """
        SELECT ce.rule_id, rc.description, ce.score
        FROM mart.change_events ce
        JOIN core.rule_catalog rc ON rc.rule_id = ce.rule_id
        WHERE ce.corp_code = %s AND ce.review_date = %s
        ORDER BY ce.score DESC
        """,
        (corp_code, review_date),
    )
    return [{"rule_id": r[0], "description": r[1], "score": r[2]} for r in cur.fetchall()]


def checklist_for(cur, rule_id: str) -> list[str]:
    """core.rule_checklist에서 그 rule_id의 확인사항만 가져온다(없으면 빈 리스트) -
    LLM이 확인사항을 즉석에서 지어내지 않도록 이 목록 밖으로 못 나가게 한다."""
    cur.execute(
        "SELECT check_item FROM core.rule_checklist WHERE rule_id = %s ORDER BY seq",
        (rule_id,),
    )
    return [r[0] for r in cur.fetchall()]


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
        return json.loads(content).get("items", [])
    except json.JSONDecodeError:
        print(f"  JSON 파싱 실패, 원본: {content[:300]}")
        return []


def validate_item(item: dict, sections_by_rcept: dict[str, dict[int, dict]]) -> bool:
    """LLM이 쓰는 유일한 파트(변화/change)에 대한 근거 검증 - 원문 섹션에 실제로 있는
    발췌인지 확인. v4.1부터 사유/확인은 LLM이 안 쓰므로(결정론적으로 조립) 이 함수의
    검증 대상이 아니다."""
    rcept_no = item.get("evidence_rcept_no")
    section_no = item.get("evidence_section_no")
    excerpt = (item.get("evidence_excerpt") or "").strip()
    change = (item.get("change") or "").strip()
    if not (rcept_no and section_no is not None and excerpt and change):
        return False
    section = sections_by_rcept.get(rcept_no, {}).get(int(section_no))
    if not section:
        return False
    return excerpt in section["section_text"]


def _insert_sentence(cur, rcept_no: str, sentence_no: int, sentence_type: str, sentence_text: str,
                      evidence_rcept_no: str, evidence_section: str, evidence_excerpt: str) -> None:
    cur.execute(
        """
        INSERT INTO mart.explanation_sentences
            (rcept_no, sentence_no, sentence_type, sentence_text,
             evidence_rcept_no, evidence_section, evidence_excerpt)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (rcept_no, sentence_no) DO UPDATE SET
            sentence_type = EXCLUDED.sentence_type,
            sentence_text = EXCLUDED.sentence_text,
            evidence_rcept_no = EXCLUDED.evidence_rcept_no,
            evidence_section = EXCLUDED.evidence_section,
            evidence_excerpt = EXCLUDED.evidence_excerpt
        """,
        (rcept_no, sentence_no, sentence_type, sentence_text, evidence_rcept_no, evidence_section, evidence_excerpt),
    )


def process_target(cur, target: dict, review_date: str) -> int:
    """①변화는 LLM(원문 근거 검증 필수), ②사유·③확인은 rule_catalog/rule_checklist에서
    결정론적으로 조립(v4.1 5단계) - "점수·등급·판정은 코드가 계산하고 LLM은 계산하지
    않는다"를 AI 해설에도 적용. 대표 Rule(가장 점수 높은 것, company_priority.top_rule_id와
    같은 기준)의 description이 사유, 그 rule_id의 체크리스트가 확인사항이 된다."""
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
    items = call_openai(prompt)

    n = 0
    changes_kept = 0
    for item in items:
        if not validate_item(item, sections_by_rcept):
            print(f"  REJECTED (근거 불일치): {item.get('change', '')[:60]}")
            continue
        n += 1
        changes_kept += 1
        ev_section = sections_by_rcept[item["evidence_rcept_no"]][int(item["evidence_section_no"])]
        _insert_sentence(
            cur, target["rcept_no"], n, "변화", item["change"].strip(),
            item["evidence_rcept_no"], ev_section["section_title"], item["evidence_excerpt"],
        )
    if changes_kept == 0:
        print(f"  {target['rcept_no']}: 생성 {len(items)}건 중 0건 근거 검증 통과 - 사유/확인도 생략")
        return 0

    # ②③은 "변화"가 최소 1건 살아남았을 때만 붙인다 - 근거 없는 변화에 사유/확인만
    # 달리는 건 맥락이 없어 의미가 없다. 대표 Rule(가장 점수 높은 것) 하나의 사유/
    # 체크리스트만 쓴다 - 여러 Rule이 떴어도 "가장 중요한 단일 사건" 원칙(v4.0/4.1)과
    # 일관되게.
    rules = fired_rules(cur, target["corp_code"], review_date)
    if rules:
        top_rule = rules[0]
        n += 1
        _insert_sentence(
            cur, target["rcept_no"], n, "사유", top_rule["description"],
            top_rule["rule_id"], "규칙 설명 (rule_catalog)", top_rule["description"],
        )
        checklist_items = checklist_for(cur, top_rule["rule_id"])
        for item_text in checklist_items:
            n += 1
            _insert_sentence(
                cur, target["rcept_no"], n, "확인", item_text,
                top_rule["rule_id"], "체크리스트 (rule_checklist)", item_text,
            )
        print(f"  {target['rcept_no']}: 변화 {changes_kept}건 근거 검증 통과, "
              f"사유 1건 + 확인 {len(checklist_items)}건 ({top_rule['rule_id']} 기준)")
    else:
        print(f"  {target['rcept_no']}: 변화 {changes_kept}건 근거 검증 통과, "
              f"사유·확인 생략(이번 검토에서 발동한 Rule 없음)")
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-date", required=True)
    parser.add_argument("--limit-per-company", type=int, default=AI_EXPLAIN_LIMIT)
    parser.add_argument("--stock-code", help="6자리 종목코드 - 이 기업만 처리 (우선)")
    parser.add_argument("--corp-code", help="OpenDART corp_code - 이 기업만 처리")
    parser.add_argument("--corp", help="회사명 부분일치 - 이 기업만 처리 (core.companies에 이미 적재된 기업만)")
    args = parser.parse_args()

    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY가 .env에 없습니다. 설정 후 다시 실행하세요.")
        sys.exit(1)

    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                corp_code = None
                if args.stock_code:
                    cur.execute("SELECT corp_code FROM core.companies WHERE stock_code = %s", (args.stock_code,))
                    row = cur.fetchone()
                    if not row:
                        print(f"종목코드 '{args.stock_code}' 기업을 core.companies에서 찾을 수 없습니다 (먼저 수집/적재하세요).")
                        return
                    corp_code = row[0]
                elif args.corp_code:
                    corp_code = args.corp_code
                elif args.corp:
                    cur.execute("SELECT corp_code FROM core.companies WHERE corp_name ILIKE %s", (f"%{args.corp}%",))
                    rows_ = cur.fetchall()
                    if len(rows_) != 1:
                        print(f"'{args.corp}'로 {len(rows_)}개 매칭됨 - --stock-code로 특정하세요.")
                        return
                    corp_code = rows_[0][0]

                cur.execute(
                    """
                    SELECT rcept_no, corp_code, report_nm_clean, orig_rcept_no
                    FROM (
                        SELECT d.*, row_number() OVER (
                            PARTITION BY corp_code ORDER BY rcept_dt DESC
                        ) AS rn
                        FROM core.disclosures d
                        WHERE is_correction AND rcept_dt <= %s
                          AND (%s::text IS NULL OR corp_code = %s)
                    ) ranked
                    WHERE rn <= %s
                    ORDER BY corp_code, rcept_dt DESC
                    """,
                    (args.review_date, corp_code, corp_code, args.limit_per_company),
                )
                targets = [
                    {"rcept_no": r[0], "corp_code": r[1], "report_nm_clean": r[2], "orig_rcept_no": r[3]}
                    for r in cur.fetchall()
                ]
                print(f"대상 정정공시 {len(targets)}건 (기업당 최대 {args.limit_per_company}건)")
                total = 0
                for t in targets:
                    print(f"- {t['rcept_no']} ({t['report_nm_clean']})")
                    total += process_target(cur, t, args.review_date)
        print(f"\nOK, 총 {total}개 해설 문장 저장")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
