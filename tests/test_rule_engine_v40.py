"""Rule Engine v4.0 테스트 (pytest -q)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.rules.rule_engine_v40 import evaluate

CORP = "00126380"
REVIEW_DATE = "2026-10-08"
CUTOFF = "2026-10-08"


def base_financial(**overrides) -> dict:
    f = {
        "prev_op": None, "curr_op": None, "prev_cfo": None, "curr_cfo": None,
        "prev_sales": None, "curr_sales": None, "prev_debt": None, "curr_debt": None,
        "prev_equity": None, "curr_equity": None, "prev_assets": None, "curr_assets": None,
        "prev_borrowings": None, "curr_borrowings": None,
        "financial_new_since_review": True, "same_comparable_periods": True,
        "financial_report_id": "FR1", "rcept_no": "20260101000001",
    }
    f.update(overrides)
    return f


def base_disclosure(**overrides) -> dict:
    d = {
        "rcept_no": "20260102000001", "rcept_dt": "20260102", "disc_type": None,
        "report_nm": "테스트공시", "event_group_id": None, "correction_type": None,
        "structured": None,
    }
    d.update(overrides)
    return d


def test_f1_mid_band_is_mid_grade():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=2_000_000)  # b=7.5%
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F1"] == "HIT"
    ev = result["events"][0]
    assert ev["score"] == 7  # base6 + band1
    assert result["review_priority"]["grade"] == "중간"


def test_f1_large_band_is_high_grade():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=500_000)  # b=30%
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    ev = result["events"][0]
    assert ev["score"] == 9  # base6 + band3
    assert result["review_priority"]["grade"] == "높음"


def test_f1_and_f2_together_forces_emergency():
    financial = base_financial(
        prev_op=100_000, curr_op=-10_000, curr_sales=5_000_000,
        prev_cfo=50_000, curr_cfo=-5_000,
    )
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    by_rule = {e["rule_id"]: e for e in result["events"]}
    assert by_rule["F1"]["is_emergency"] is True
    assert by_rule["F1"]["grade"] == "긴급확인"
    assert by_rule["F2"]["is_emergency"] is True
    assert result["review_priority"]["grade"] == "긴급확인"


def test_f3_bidirectional_increase_is_positive():
    financial = base_financial(prev_sales=100_000, curr_sales=130_000)  # +30%
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F3"] == "HIT"
    ev = result["events"][0]
    assert ev["direction"] == "긍정"
    assert ev["score"] == 5  # base3 + band2(>=30)


def test_f6_profit_sustained_increase_is_mid():
    financial = base_financial(prev_op=100_000, curr_op=140_000)  # +40%, 둘 다 양수
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F6"] == "HIT"
    assert result["rule_status"]["F1"] == "NO_HIT"  # 부호 전환 아님 - F1과 안 겹침
    ev = result["events"][0]
    assert ev["direction"] == "긍정"
    assert result["review_priority"]["grade"] == "중간"


def test_f7_loss_sustained_widening_is_negative():
    financial = base_financial(prev_op=-100_000, curr_op=-140_000)  # 손실 40% 확대, 둘 다 음수
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F7"] == "HIT"
    ev = result["events"][0]
    assert ev["direction"] == "부정"
    assert result["review_priority"]["grade"] == "중간"


def test_f8_equity_wipeout_is_emergency_and_excludes_f4():
    financial = base_financial(
        prev_equity=100_000, curr_equity=-5_000,
        prev_debt=200_000, curr_debt=250_000,
    )
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F8"] == "HIT"
    assert result["rule_status"]["F4"] == "EXCLUDED"  # NOT_EVALUATED가 아니라 적용제외
    assert result["review_priority"]["grade"] == "긴급확인"


def test_d3_adverse_opinion_is_emergency():
    disclosures = [base_disclosure(disc_type="D3", rcept_no="D3-1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, disclosures)
    assert result["review_priority"]["grade"] == "긴급확인"


def test_d7_listing_concern_is_emergency():
    disclosures = [base_disclosure(disc_type="D7", rcept_no="D7-1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, disclosures)
    assert result["rule_status"]["D7"] == "HIT"
    assert result["review_priority"]["grade"] == "긴급확인"


def test_d5_typo_alone_is_low_grade_not_no_change():
    # v3.3-core는 U0(TYPO)을 후보에서 아예 제외해서 기업등급이 "변화없음"이 됐지만,
    # v4.0은 점수 기반(1점이라도 최소 "낮음")이라 이벤트가 있으면 "변화없음"이 아니다 -
    # v3.3 대비 의도적으로 달라진 지점.
    disclosures = [base_disclosure(disc_type="D5", correction_type="TYPO", rcept_no="D5-1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, disclosures)
    assert len(result["events"]) == 1
    assert result["events"][0]["score"] == 1
    assert result["review_priority"]["grade"] == "낮음"


def test_d5_unknown_alone_is_cannot_determine():
    disclosures = [base_disclosure(disc_type="D5", correction_type="UNKNOWN", rcept_no="D5-2")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, disclosures)
    assert result["rule_status"]["D5"] == "NOT_EVALUATED"
    assert result["events"] == []
    assert result["review_priority"]["grade"] == "판정불가"


def test_skipped_financial_with_d4_gives_mid_grade():
    financial = base_financial(financial_new_since_review=False)
    disclosures = [base_disclosure(disc_type="D4", rcept_no="D4-1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    for rid in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"):
        assert result["rule_status"][rid] == "SKIPPED_NO_NEW_FINANCIAL"
    assert result["rule_status"]["D4"] == "HIT"
    assert result["review_priority"]["grade"] == "중간"  # D4 base=5, 밴드 없음


def test_f1_missing_sales_gives_ratio_not_computable():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=None)
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    ev = result["events"][0]
    assert ev["score"] == 6  # base만, intensity 0
    assert "RATIO_NOT_COMPUTABLE" in ev["status"]
    assert result["rule_status"]["F1"] == "HIT"


def test_group_dedup_emergency_wins_over_equal_score_normal_event():
    financial = base_financial(
        prev_op=100_000, curr_op=-50_000, curr_sales=500_000,  # F1 score=9 (band3)
        event_group_id="G1",
    )
    disclosures = [base_disclosure(
        disc_type="D5", correction_type="CORE_FINANCIAL", rcept_no="D5-3", event_group_id="G1",
        structured={"changes_pl_or_equity_state": True},  # D5 score=9, 긴급확인
    )]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    assert len(result["events"]) == 2
    by_rule = {e["rule_id"]: e for e in result["events"]}
    assert by_rule["F1"]["score"] == by_rule["D5"]["score"] == 9  # 동점 확인
    dup_count = sum(1 for e in result["events"] if "GROUPED_DUPLICATE" in e["status"])
    assert dup_count == 1
    assert "GROUPED_DUPLICATE" in by_rule["F1"]["status"]  # 긴급확인(D5)이 동점을 이김
    assert result["review_priority"]["grade"] == "긴급확인"
    assert result["review_priority"]["top_event_id"] == by_rule["D5"]["event_id"]


def test_zero_events_gives_no_change():
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, [])
    assert result["events"] == []
    assert result["review_priority"]["grade"] == "변화없음"
    assert result["review_priority"]["reason_text"] == "마지막 검토일 이후 유의미한 변화 없음"


def test_reproducibility_same_input_same_output():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=2_000_000)
    disclosures = [base_disclosure(disc_type="D4", rcept_no="D4-2")]
    r1 = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    r2 = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    assert r1 == r2
