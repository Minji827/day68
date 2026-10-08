"""Rule Engine v3.3-core 테스트 (pytest -q)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.rules.rule_engine_v33 import evaluate

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


def test_f1_mid_band_hits_p2():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=1_000_000)
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F1"] == "HIT"
    ev = result["events"][0]
    assert ev["b"] == 2
    assert ev["u"] == "U2"
    assert result["review_priority"]["p"] == "P2"


def test_f1_large_b_forces_u3_p1():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=500_000)
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    ev = result["events"][0]
    assert ev["b"] == 3  # 30% >= 20 밴드
    assert ev["u"] == "U3"
    assert result["review_priority"]["p"] == "P1"


def test_f1_and_f2_both_hit_forces_u3():
    financial = base_financial(
        prev_op=100_000, curr_op=-10_000, curr_sales=5_000_000,  # B 작게(< 20)
        prev_cfo=50_000, curr_cfo=-5_000,
    )
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F1"] == "HIT"
    assert result["rule_status"]["F2"] == "HIT"
    by_rule = {e["rule_id"]: e for e in result["events"]}
    assert by_rule["F1"]["u"] == "U3"
    assert by_rule["F2"]["u"] == "U3"
    assert result["review_priority"]["p"] == "P1"


def test_f6_turnaround_mid_band_p2():
    financial = base_financial(prev_op=-100_000, curr_op=50_000, curr_sales=1_000_000)
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F6"] == "HIT"
    ev = result["events"][0]
    assert ev["u"] == "U2"
    assert result["review_priority"]["p"] == "P2"


def test_f7_growth_u1_always_p3():
    financial = base_financial(prev_op=100_000, curr_op=150_000, prev_sales=200_000, curr_sales=300_000)
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F7"] == "HIT"
    ev = result["events"][0]
    assert ev["u"] == "U1"
    assert result["review_priority"]["p"] == "P3"


def test_f8_equity_wipeout_p1_and_f4_not_evaluated():
    financial = base_financial(
        prev_equity=100_000, curr_equity=-5_000,
        prev_debt=200_000, curr_debt=250_000,
    )
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    assert result["rule_status"]["F8"] == "HIT"
    assert result["rule_status"]["F4"] == "NOT_EVALUATED"
    assert result["review_priority"]["p"] == "P1"
    assert result["review_priority"]["u"] == "U3"


def test_d3_adverse_opinion_p1():
    disclosures = [base_disclosure(disc_type="D3", rcept_no="D3-1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, disclosures)
    assert result["rule_status"]["D3"] == "HIT"
    assert result["review_priority"]["p"] == "P1"


def test_d5_typo_alone_is_none_but_event_exists():
    disclosures = [base_disclosure(disc_type="D5", correction_type="TYPO", rcept_no="D5-1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, disclosures)
    assert result["rule_status"]["D5"] == "HIT"
    assert len(result["events"]) == 1
    assert result["events"][0]["u"] == "U0"
    assert result["review_priority"]["p"] == "NONE"


def test_d5_unknown_alone_is_not_evaluated():
    disclosures = [base_disclosure(disc_type="D5", correction_type="UNKNOWN", rcept_no="D5-2")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, disclosures)
    assert result["rule_status"]["D5"] == "NOT_EVALUATED"
    assert result["events"] == []
    assert result["review_priority"]["p"] == "NOT_EVALUATED"


def test_skipped_financial_with_d4_gives_p2():
    financial = base_financial(financial_new_since_review=False)
    disclosures = [base_disclosure(disc_type="D4", rcept_no="D4-1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    for rid in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"):
        assert result["rule_status"][rid] == "SKIPPED_NO_NEW_FINANCIAL"
    assert result["rule_status"]["D4"] == "HIT"
    assert result["review_priority"]["p"] == "P2"  # A=2,B=0,C=0,D=0 -> M=2 -> U2,1~6 -> P2


def test_f1_missing_sales_gives_b_not_computable():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=None)
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, [])
    ev = result["events"][0]
    assert ev["b"] == 0
    assert "B_NOT_COMPUTABLE" in ev["status"]
    assert result["rule_status"]["F1"] == "HIT"


def test_event_group_dedup_d5_material_plus_f1_gives_p1_single_candidate():
    financial = base_financial(
        prev_op=100_000, curr_op=-10_000, curr_sales=5_000_000,  # F1 B 작게(U3 강제 안 걸리게)
        event_group_id="G1",
    )
    disclosures = [base_disclosure(disc_type="D5", correction_type="MATERIAL_FINANCIAL",
                                    rcept_no="D5-3", event_group_id="G1")]
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    assert len(result["events"]) == 2  # 둘 다 events엔 남음
    dup_count = sum(1 for e in result["events"] if "GROUPED_DUPLICATE" in e["status"])
    assert dup_count == 1
    assert result["review_priority"]["p"] == "P1"
    # 최종 후보(top_event_id)는 그룹에서 대표로 남은 쪽 하나뿐
    winners = [e for e in result["events"] if "GROUPED_DUPLICATE" not in e["status"]]
    assert len(winners) == 1
    assert result["review_priority"]["top_event_id"] == winners[0]["event_id"]


def test_zero_events_gives_none():
    result = evaluate(CORP, REVIEW_DATE, CUTOFF, None, [])
    assert result["events"] == []
    assert result["review_priority"]["p"] == "NONE"
    assert result["review_priority"]["reason_text"] == "마지막 검토일 이후 유의미한 변화 없음"


def test_reproducibility_same_input_same_output():
    financial = base_financial(prev_op=100_000, curr_op=-50_000, curr_sales=1_000_000)
    disclosures = [base_disclosure(disc_type="D4", rcept_no="D4-2")]
    r1 = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    r2 = evaluate(CORP, REVIEW_DATE, CUTOFF, financial, disclosures)
    assert r1 == r2
