"""RAW layer collector: pulls OpenDART + ECOS data and dumps raw responses to disk,
matching PRD section 9 (RAW = API 응답 원본을 그대로 저장).

Usage:
    python scripts/collect.py --corp 삼성전자 --bgn-de 20250101 --end-de 20261007
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ecos_client
import opendart_client

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> {path.relative_to(DATA_DIR.parent.parent)} ({path.stat().st_size} bytes)")


def collect_disclosures(corp_code: str, bgn_de: str, end_de: str) -> list[dict]:
    """OpenDART list.json은 한 번 호출에 최대 100건만 준다(page_count 상한). 공시가
    100건을 넘는 기업(예: 삼성전자는 기간 내 수천 건)은 total_page까지 순회하지 않으면
    가장 오래된 쪽 공시 대부분이 조용히 누락된다 — 실제로 발견된 버그, 전부 순회해서 모은다."""
    print(f"[disclosures] {corp_code} {bgn_de}~{end_de}")
    data = opendart_client.get_disclosure_list(corp_code, bgn_de, end_de, page_no=1, page_count=100)
    items = list(data.get("list", []))
    total_page = data.get("total_page") or 1
    for page_no in range(2, total_page + 1):
        more = opendart_client.get_disclosure_list(corp_code, bgn_de, end_de, page_no=page_no, page_count=100)
        items.extend(more.get("list", []))
    data["list"] = items
    print(f"  total_count={data.get('total_count')} total_page={total_page} -> {len(items)}건 수집")
    _write_json(DATA_DIR / "disclosures" / f"{corp_code}_{bgn_de}_{end_de}.json", data)
    return items


def collect_financials(corp_code: str, bsns_years: list[str], reprt_code: str = "11011") -> None:
    for year in bsns_years:
        for fs_div in ("CFS", "OFS"):
            print(f"[financials] {corp_code} {year} {reprt_code} {fs_div}")
            try:
                data = opendart_client.get_financial_statements(
                    corp_code, year, reprt_code=reprt_code, fs_div=fs_div
                )
            except opendart_client.OpenDartError as e:
                print(f"  SKIP ({e})")
                continue
            _write_json(
                DATA_DIR / "financials" / f"{corp_code}_{year}_{reprt_code}_{fs_div}.json", data
            )


def collect_documents(rcept_nos: list[str]) -> None:
    out_dir = DATA_DIR / "documents"
    out_dir.mkdir(parents=True, exist_ok=True)
    for rcept_no in rcept_nos:
        print(f"[document] {rcept_no}")
        try:
            content = opendart_client.get_document_original(rcept_no)
        except opendart_client.OpenDartError as e:
            print(f"  SKIP ({e})")
            continue
        path = out_dir / f"{rcept_no}.zip"
        path.write_bytes(content)
        print(f"  -> {path.relative_to(DATA_DIR.parent.parent)} ({len(content)} bytes)")


def collect_base_rate(start: str, end: str, cycle: str = "D") -> None:
    print(f"[ecos base rate] {start}~{end} ({cycle})")
    rows = ecos_client.get_base_rate(start, end, cycle=cycle)
    _write_json(DATA_DIR / "ecos" / f"base_rate_{cycle}_{start}_{end}.json", rows)


def resolve_corp(args) -> dict | None:
    """--stock-code(종목코드, 고유값)가 우선. --corp(회사명)는 보조 수단이며,
    후보가 여럿이면 추측하지 않고 목록을 보여준 뒤 중단한다."""
    if args.stock_code:
        corp = opendart_client.find_corp_by_stock_code(args.stock_code)
        if not corp:
            print(f"종목코드 '{args.stock_code}'에 해당하는 기업을 찾을 수 없습니다.")
            return None
        return corp

    matches = opendart_client.find_corp_code(args.corp)
    listed = [m for m in matches if m["stock_code"]]
    if not listed:
        print(f"'{args.corp}' 상장사 매칭 실패. 후보: {matches[:5]}")
        return None
    if len(listed) > 1:
        print(f"'{args.corp}'로 상장사가 {len(listed)}개 매칭됨 - 종목코드로 다시 지정하세요:")
        for m in listed:
            print(f"  {m['corp_name']} (종목코드 {m['stock_code']}, corp_code {m['corp_code']})")
        return None
    return listed[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock-code", help="6자리 종목코드 (예: 005930) - 모호함 없이 기업을 특정, 우선 사용")
    parser.add_argument("--corp", help="회사명 (부분일치). 후보가 여럿이면 --stock-code로 다시 지정 요구")
    parser.add_argument("--bgn-de", default="20250101")
    parser.add_argument("--end-de", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--years", nargs="*", default=["2024", "2025"], help="재무제표 사업연도")
    parser.add_argument("--skip-documents", action="store_true")
    args = parser.parse_args()

    if not args.stock_code and not args.corp:
        print("--stock-code 또는 --corp 중 하나는 필요합니다.")
        return

    corp = resolve_corp(args)
    if not corp:
        return
    print(f"대상 기업: {corp}")
    corp_code = corp["corp_code"]

    items = collect_disclosures(corp_code, args.bgn_de, args.end_de)
    collect_financials(corp_code, args.years)
    if not args.skip_documents and items:
        rcept_nos = [it["rcept_no"] for it in items[:5]]
        collect_documents(rcept_nos)
    collect_base_rate(args.bgn_de, args.end_de, cycle="D")
    print("\nDONE")


if __name__ == "__main__":
    main()
