"""OpenDART (전자공시시스템) API client.

Docs: https://opendart.fss.or.kr/guide/main.do
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://opendart.fss.or.kr/api"
API_KEY = os.environ.get("OPENDART_API_KEY", "")
CORP_CODE_CACHE = Path(__file__).resolve().parent.parent / "data" / "raw" / "corpCode.xml"


class OpenDartError(Exception):
    pass


def _get(path: str, params: dict) -> requests.Response:
    params = {"crtfc_key": API_KEY, **params}
    resp = requests.get(f"{BASE_URL}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp


def _check_status(data: dict) -> dict:
    # OpenDART returns status "000" on success, other codes are documented errors.
    status = data.get("status")
    if status != "000":
        raise OpenDartError(f"{status}: {data.get('message')}")
    return data


def download_corp_code(force: bool = False) -> Path:
    """Download and cache the full corp_code <-> corp_name mapping (zip of XML)."""
    CORP_CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if CORP_CODE_CACHE.exists() and not force:
        return CORP_CODE_CACHE

    resp = _get("corpCode.xml", {})
    content_type = resp.headers.get("Content-Type", "")
    if "zip" not in content_type and not resp.content[:2] == b"PK":
        # Error responses come back as XML/JSON instead of a zip.
        raise OpenDartError(f"corpCode.xml download failed: {resp.text[:300]}")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")
    CORP_CODE_CACHE.write_bytes(xml_bytes)
    return CORP_CODE_CACHE


def find_corp_code(corp_name: str) -> list[dict]:
    """Look up corp_code(s) by exact/partial corp_name from the cached mapping."""
    import xml.etree.ElementTree as ET

    path = download_corp_code()
    root = ET.parse(path).getroot()
    matches = []
    for node in root.findall("list"):
        name = (node.findtext("corp_name") or "").strip()
        if corp_name in name:
            matches.append(
                {
                    "corp_code": node.findtext("corp_code"),
                    "corp_name": name,
                    "stock_code": (node.findtext("stock_code") or "").strip(),
                    "modify_date": node.findtext("modify_date"),
                }
            )
    return matches


def get_disclosure_list(
    corp_code: str,
    bgn_de: str,
    end_de: str,
    page_no: int = 1,
    page_count: int = 100,
) -> dict:
    """공시검색: list of disclosures for a company within a date range (YYYYMMDD)."""
    data = _get(
        "list.json",
        {
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": page_no,
            "page_count": page_count,
        },
    ).json()
    if data.get("status") == "013":
        # "조회된 데이터가 없습니다" is a valid empty result, not an error.
        return {**data, "list": []}
    return _check_status(data)


def get_financial_statements(
    corp_code: str,
    bsns_year: str,
    reprt_code: str = "11011",
    fs_div: str = "OFS",
) -> dict:
    """단일회사 전체 재무제표 (fnlttSinglAcntAll).

    reprt_code: 11013=1분기, 11012=반기, 11014=3분기, 11011=사업보고서(연간)
    fs_div: OFS=개별재무제표, CFS=연결재무제표
    """
    data = _get(
        "fnlttSinglAcntAll.json",
        {
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        },
    ).json()
    return _check_status(data)


def get_document_original(rcept_no: str) -> bytes:
    """공시서류원본파일 (zip containing XML), by 접수번호."""
    resp = _get("document.xml", {"rcept_no": rcept_no})
    if resp.content[:2] != b"PK":
        raise OpenDartError(f"document.xml download failed for {rcept_no}: {resp.text[:300]}")
    return resp.content


if __name__ == "__main__":
    print("OPENDART_API_KEY set:", bool(API_KEY))
