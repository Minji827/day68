"""공시원문을 섹션 단위로 받아오고 core.disclosure_sections에 캐싱.
explain.py(AI 해설)와 compare_disclosure.py(AI 없는 결정론적 diff)가 공유한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dart_xml
import opendart_client

DOCS_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "documents"


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
