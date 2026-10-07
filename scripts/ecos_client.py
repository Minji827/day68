"""ECOS (한국은행 경제통계시스템) Open API client.

Docs: https://ecos.bok.or.kr/api/#/
"""
from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://ecos.bok.or.kr/api"
API_KEY = os.environ.get("ECOS_API_KEY", "")

# 한국은행 기준금리: 통계표 코드 722Y001 (한국은행 기준금리 및 여수신금리),
# 항목 코드 0101000 (한국은행 기준금리). Confirmed via StatisticItemList below.
BASE_RATE_STAT_CODE = "722Y001"
BASE_RATE_ITEM_CODE = "0101000"


class EcosError(Exception):
    pass


def _check_result(data: dict, key: str) -> dict:
    if "RESULT" in data.get(key, {}):
        result = data[key]["RESULT"]
        raise EcosError(f"{result.get('CODE')}: {result.get('MESSAGE')}")
    return data


def statistic_item_list(stat_code: str, start: int = 1, end: int = 100) -> list[dict]:
    """List the item codes (세부 항목) under a statistic table code."""
    url = f"{BASE_URL}/StatisticItemList/{API_KEY}/json/kr/{start}/{end}/{stat_code}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _check_result(data, "StatisticItemList")
    return data.get("StatisticItemList", {}).get("row", [])


def statistic_search(
    stat_code: str,
    item_code1: str,
    cycle: str,
    start: str,
    end: str,
    page_start: int = 1,
    page_end: int = 1000,
) -> list[dict]:
    """StatisticSearch: fetch observations for a stat_code/item_code over a period.

    cycle: A=연, Q=분기, M=월, D=일
    start/end: format depends on cycle (e.g. M -> YYYYMM, D -> YYYYMMDD)
    """
    url = (
        f"{BASE_URL}/StatisticSearch/{API_KEY}/json/kr/{page_start}/{page_end}/"
        f"{stat_code}/{cycle}/{start}/{end}/{item_code1}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _check_result(data, "StatisticSearch")
    return data.get("StatisticSearch", {}).get("row", [])


def get_base_rate(start: str, end: str, cycle: str = "D") -> list[dict]:
    """한국은행 기준금리 관측값 조회."""
    return statistic_search(BASE_RATE_STAT_CODE, BASE_RATE_ITEM_CODE, cycle, start, end)


if __name__ == "__main__":
    print("ECOS_API_KEY set:", bool(API_KEY))
