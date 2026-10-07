"""Sanity-test OpenDART + ECOS against the real APIs (PRD section 11 risk checks)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ecos_client
import opendart_client


def test_opendart_corp_lookup():
    print("\n=== OpenDART: corp_code lookup (삼성전자) ===")
    matches = opendart_client.find_corp_code("삼성전자")
    for m in matches[:5]:
        print(m)
    assert matches, "corp_code lookup returned nothing"
    return matches[0]["corp_code"]


def test_opendart_disclosure_list(corp_code: str):
    print("\n=== OpenDART: disclosure list (최근 90일) ===")
    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=90)
    data = opendart_client.get_disclosure_list(
        corp_code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    )
    items = data.get("list", [])
    print(f"총 {len(items)}건")
    for it in items[:5]:
        print(
            {
                "rcept_no": it.get("rcept_no"),
                "report_nm": it.get("report_nm"),
                "rcept_dt": it.get("rcept_dt"),
                "rm": it.get("rm"),
            }
        )
    return items


def test_correction_linkage(items: list[dict]):
    print("\n=== 리스크 체크: 정정공시가 원공시 접수번호를 포함하는가 ===")
    corrections = [it for it in items if "정정" in (it.get("report_nm") or "")]
    if not corrections:
        print("최근 90일 내 정정공시 없음 (다른 기업/기간으로 재확인 필요)")
        return
    for c in corrections[:3]:
        print(json.dumps(c, ensure_ascii=False, indent=2))
    print(
        "-> list.json 응답 필드에 원공시 접수번호가 별도로 없다면, "
        "report_nm/rcept_dt 매칭 규칙(제약 11번 대응안)으로 가야 함."
    )


def test_opendart_financials(corp_code: str):
    print("\n=== OpenDART: 재무제표 (2024 사업보고서, 연결) ===")
    try:
        data = opendart_client.get_financial_statements(
            corp_code, "2024", reprt_code="11011", fs_div="CFS"
        )
        rows = data.get("list", [])
        wanted = {"매출액", "영업이익", "부채총계", "영업활동현금흐름"}
        found = [r for r in rows if r.get("account_nm") in wanted]
        print(f"총 {len(rows)}행 중 타겟 계정 {len(found)}건 매칭")
        for r in found[:6]:
            print(
                {
                    "account_nm": r.get("account_nm"),
                    "thstrm_amount": r.get("thstrm_amount"),
                    "frmtrm_amount": r.get("frmtrm_amount"),
                }
            )
    except opendart_client.OpenDartError as e:
        print(f"FAILED: {e}")


def test_opendart_document(items: list[dict]):
    print("\n=== OpenDART: 공시원문 다운로드 ===")
    if not items:
        print("skip (no disclosures to test with)")
        return
    rcept_no = items[0]["rcept_no"]
    try:
        content = opendart_client.get_document_original(rcept_no)
        print(f"rcept_no={rcept_no} -> {len(content)} bytes zip 수신 성공")
    except opendart_client.OpenDartError as e:
        print(f"FAILED: {e}")


def test_ecos_item_list():
    print("\n=== ECOS: StatisticItemList(722Y001) 항목 코드 확인 ===")
    rows = ecos_client.statistic_item_list(ecos_client.BASE_RATE_STAT_CODE)
    for r in rows[:10]:
        print({"ITEM_CODE": r.get("ITEM_CODE"), "ITEM_NAME": r.get("ITEM_NAME")})


def test_ecos_base_rate():
    print("\n=== ECOS: 기준금리 조회 (최근 1년, 일별) ===")
    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=365)
    try:
        rows = ecos_client.get_base_rate(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), cycle="D")
        print(f"총 {len(rows)}건")
        for r in rows[:3]:
            print(r)
        for r in rows[-3:]:
            print(r)
    except ecos_client.EcosError as e:
        print(f"FAILED (일별 실패, 코드/주기 재확인 필요): {e}")


if __name__ == "__main__":
    corp_code = test_opendart_corp_lookup()
    items = test_opendart_disclosure_list(corp_code)
    test_correction_linkage(items)
    test_opendart_financials(corp_code)
    test_opendart_document(items)
    test_ecos_item_list()
    test_ecos_base_rate()
    print("\n=== DONE ===")
