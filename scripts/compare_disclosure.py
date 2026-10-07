"""정정 전후 비교 (AI 없이, 결정론적). FR-01 AC: "정정공시는 원공시와 연결되어
정정 전·후를 확인할 수 있다"를 실제로 눈에 보이게 만드는 스크립트.

원공시·정정공시 원문을 섹션 단위로 받아서(scripts/sections.py), 같은 제목의 섹션끼리
단어 단위 diff를 떠서 [-삭제-]{+추가+} 표기로 보여준다. 수치·표현이 정확히 어디서 어떻게
바뀌었는지, 추측 없이 원문 그대로 비교한다.

Usage:
    python scripts/compare_disclosure.py --stock-code 005930 --review-date 2026-01-01
    python scripts/compare_disclosure.py --corp-code 00162461 --review-date 2026-01-01
    python scripts/compare_disclosure.py --rcept-no 20260916800377   # 단건 지정
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from sections import ensure_sections

CONTEXT_WORDS = 4
MAX_SECTION_CHARS = 1200


def word_diff(old_text: str, new_text: str) -> str:
    old_words = old_text.split()
    new_words = new_text.split()
    sm = difflib.SequenceMatcher(None, old_words, new_words)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            words = old_words[i1:i2]
            if len(words) > CONTEXT_WORDS * 2:
                out.append(" ".join(words[:CONTEXT_WORDS]) + " ... " + " ".join(words[-CONTEXT_WORDS:]))
            else:
                out.append(" ".join(words))
        elif tag == "replace":
            out.append(f"[-{' '.join(old_words[i1:i2])}-]{{+{' '.join(new_words[j1:j2])}+}}")
        elif tag == "delete":
            out.append(f"[-{' '.join(old_words[i1:i2])}-]")
        elif tag == "insert":
            out.append(f"{{+{' '.join(new_words[j1:j2])}+}}")
    return " ".join(out)


def diff_sections(orig_sections: list[dict], new_sections: list[dict]) -> list[dict]:
    orig_by_title = {s["section_title"]: s for s in orig_sections}
    new_by_title = {s["section_title"]: s for s in new_sections}
    seen = set()
    ordered_titles = []
    for s in orig_sections + new_sections:
        if s["section_title"] not in seen:
            seen.add(s["section_title"])
            ordered_titles.append(s["section_title"])

    results = []
    for title in ordered_titles:
        o = orig_by_title.get(title)
        n = new_by_title.get(title)
        if o and n:
            if o["section_text"] == n["section_text"]:
                continue
            results.append(
                {
                    "title": title,
                    "status": "changed",
                    "diff": word_diff(o["section_text"][:MAX_SECTION_CHARS], n["section_text"][:MAX_SECTION_CHARS]),
                }
            )
        elif n and not o:
            results.append({"title": title, "status": "added", "text": n["section_text"][:MAX_SECTION_CHARS]})
        elif o and not n:
            results.append({"title": title, "status": "removed", "text": o["section_text"][:MAX_SECTION_CHARS]})
    return results


def compare_one(cur, rcept_no: str, orig_rcept_no: str | None, report_nm: str) -> None:
    print(f"\n{'=' * 70}\n정정공시 {rcept_no}: {report_nm}")
    if not orig_rcept_no:
        print("  (정정 전 원공시를 찾지 못함 - 비교 불가, 정정공시 원문만 존재)")
        return

    new_sections = ensure_sections(cur, rcept_no)
    orig_sections = ensure_sections(cur, orig_rcept_no)
    if not new_sections or not orig_sections:
        print(f"  (원문 섹션을 가져오지 못함: 정정={len(new_sections)}개, 원공시={len(orig_sections)}개)")
        return

    changes = diff_sections(orig_sections, new_sections)
    if not changes:
        print("  섹션 텍스트 기준으로는 차이 없음 (표 서식/숫자 미세 차이는 못 잡을 수 있음)")
        return

    for c in changes:
        if c["status"] == "changed":
            print(f"\n  [변경] {c['title']}")
            print(f"    {c['diff'][:2000]}")
        elif c["status"] == "added":
            print(f"\n  [신규 섹션] {c['title']}")
            print(f"    {c['text'][:400]}")
        elif c["status"] == "removed":
            print(f"\n  [삭제된 섹션] {c['title']}")
            print(f"    {c['text'][:400]}")


def resolve_corp_code(cur, args) -> str | None:
    if args.corp_code:
        return args.corp_code
    if args.stock_code:
        cur.execute("SELECT corp_code FROM core.companies WHERE stock_code = %s", (args.stock_code,))
        row = cur.fetchone()
        return row[0] if row else None
    if args.corp:
        cur.execute("SELECT corp_code FROM core.companies WHERE corp_name ILIKE %s LIMIT 1", (f"%{args.corp}%",))
        row = cur.fetchone()
        return row[0] if row else None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock-code", help="6자리 종목코드 (예: 005930)")
    parser.add_argument("--corp-code", help="OpenDART corp_code (8자리)")
    parser.add_argument("--corp", help="회사명 부분일치 (core.companies에 이미 적재된 기업만)")
    parser.add_argument("--rcept-no", help="특정 정정공시 접수번호 1건만 비교")
    parser.add_argument("--review-date", help="이 날짜 이후 정정공시만 (기본: 전체)")
    args = parser.parse_args()

    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if args.rcept_no:
                    cur.execute(
                        "SELECT rcept_no, orig_rcept_no, report_nm_clean FROM core.disclosures WHERE rcept_no = %s",
                        (args.rcept_no,),
                    )
                    row = cur.fetchone()
                    if not row:
                        print(f"rcept_no {args.rcept_no}를 core.disclosures에서 찾을 수 없음")
                        return
                    compare_one(cur, row[0], row[1], row[2])
                    return

                corp_code = resolve_corp_code(cur, args)
                if not corp_code:
                    print("--stock-code, --corp-code, --corp 중 하나로 기업을 지정하세요 (core.companies에 적재되어 있어야 함)")
                    return

                query = "SELECT rcept_no, orig_rcept_no, report_nm_clean FROM core.disclosures WHERE corp_code = %s AND is_correction"
                params: list = [corp_code]
                if args.review_date:
                    query += " AND rcept_dt > %s"
                    params.append(args.review_date)
                query += " ORDER BY rcept_dt DESC"
                cur.execute(query, params)
                targets = cur.fetchall()

                if not targets:
                    print("해당 조건의 정정공시가 없습니다.")
                    return

                print(f"{len(targets)}건의 정정공시 비교")
                for rcept_no, orig_rcept_no, report_nm in targets:
                    compare_one(cur, rcept_no, orig_rcept_no, report_nm)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
