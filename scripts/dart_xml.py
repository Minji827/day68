"""OpenDART 공시서류원본(document.xml zip) 파서.

DART 원문 XML은 ATOC="Y" 속성이 붙은 제목 요소(목차 항목)로 섹션이 구분된다
(실측 확인: COVER-TITLE 등). 이 마커를 기준으로 텍스트를 섹션 단위로 쪼갠다.
"""
from __future__ import annotations

import html
import io
import re
import xml.etree.ElementTree as ET
import zipfile


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_html_fallback(raw_bytes: bytes, max_chars: int) -> list[dict]:
    """일부 정정신고(자율공시)는 DART DOCUMENT XML이 아니라 깨진 HTML(XFORM 템플릿)로
    내려옴 (실측 확인, mismatched tag로 strict XML 파싱 실패). PRD 11번 대응안대로
    섹션 구분 없이 "원문 발췌" 단위(제목 1개 + 본문 1개)로 대체한다."""
    text = raw_bytes.decode("utf-8", errors="replace")
    title_m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    title = _clean(html.unescape(re.sub(r"<[^>]+>", " ", title_m.group(1)))) if title_m else "(제목없음)"

    body_m = re.search(r"<body[^>]*>(.*)</body>", text, re.IGNORECASE | re.DOTALL)
    body_html = body_m.group(1) if body_m else text
    body_text = _clean(html.unescape(re.sub(r"<[^>]+>", " ", body_html)))[:max_chars]

    sections = []
    if title:
        sections.append({"section_no": 1, "section_title": "제목", "section_text": title})
    if body_text:
        sections.append({"section_no": len(sections) + 1, "section_title": "본문(원문 발췌)", "section_text": body_text})
    return sections


def parse_sections(zip_bytes: bytes, max_chars: int = 6000) -> list[dict]:
    """zip 바이트 -> [{"section_no", "section_title", "section_text"}, ...]."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            return []
        xml_bytes = zf.read(xml_names[0])

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return _parse_html_fallback(xml_bytes, max_chars)

    sections: list[dict] = []
    current_title: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_title is None and not buffer:
            return
        text = _clean(" ".join(buffer))[:max_chars]
        if text:
            sections.append(
                {
                    "section_no": len(sections) + 1,
                    "section_title": current_title or "(제목없음)",
                    "section_text": text,
                }
            )

    for el in root.iter():
        if el.attrib.get("ATOC") == "Y":
            flush()
            current_title = _clean("".join(el.itertext()))
            buffer = []
            continue
        if el.text and el.text.strip():
            buffer.append(el.text.strip())
        if el.tail and el.tail.strip():
            buffer.append(el.tail.strip())
    flush()
    if not sections:
        # ATOC 마커가 없는 DOCUMENT 포맷 - 전체 텍스트를 단일 섹션으로 반환
        text = _clean("".join(root.itertext()))[:max_chars]
        if text:
            sections.append({"section_no": 1, "section_title": "본문(원문 발췌)", "section_text": text})
    return sections


if __name__ == "__main__":
    import sys

    path = sys.argv[1]
    secs = parse_sections(open(path, "rb").read())
    print(f"{len(secs)}개 섹션")
    for s in secs[:10]:
        print(f"[{s['section_no']}] {s['section_title']}: {s['section_text'][:120]}")
