"""DeltaWatch Rule Engine v4.0.

순수 함수 엔진 - DB/API 접근 없음. 정규화된 dict를 받아 dict를 반환한다. 같은 입력이면
항상 같은 출력(random/datetime.now() 미사용).

v3.3-core(이 패키지의 rule_engine_v33.py)와의 근본적인 차이: "점수를 전부 더해서
등급을 매기지 않는다." 기업의 재검토 등급은 **가장 중요한 단일 사건**으로 정해지고,
긴급확인 사건이 하나라도 있으면 점수와 무관하게 그게 등급을 결정한다 (스펙 8번).
등급 용어도 P1/P2/P3/NONE 대신 긴급확인/높음/중간/낮음/변화없음/판정불가로 바뀌었고,
점수는 "절대적인 위험도"가 아니라 "같은 등급 안에서의 순서 정렬용 보조 척도"(1~10)다.

스펙에 공식이 없어 직접 정한 부분(팀 설정값, 실제 사례로 검증 필요 - 스펙 11번과
같은 취지로 명시):
- score = clamp(base_score(룰) + intensity_bonus(0~3, 3단계 임계값 밴드), 1, 10).
  base_score는 룰별 "기본 중요성"을 내가 1~10 사이로 배정한 값 (THRESHOLDS 참고).
- 점수->등급 매핑은 스펙 4번 표 그대로: 8~10=높음, 4~7=중간, 1~3=낮음.
- D2(유상증자)/D6A~D6D의 밴드 숫자는 스펙에 명시가 없어 D1과 같은 패턴(비율 5/10/20,
  D2는 10/20/30)으로 통일. F6/F7의 2·3차 밴드(50/80)도 스펙이 준 1차 임계값(30%)
  이후를 내가 등간격으로 확장한 것 - 전부 THRESHOLDS 주석 참고.
- "관리종목/상장적격성/상장폐지"(D7), "거래정지/회생절차/부도"(D8), "영업정지/
  핵심사업중단"(D9)은 스펙 7번에 조건만 있고 룰 ID가 없어서 내가 새로 번호를 매김.

Usage:
    from src.rules.rule_engine_v40 import evaluate
    result = evaluate(
        corp_code="00126380", review_date="2026-10-08", data_cutoff="2026-10-08",
        financial={"prev_op": 1000, "curr_op": -500, "curr_sales": 10000,
                   "financial_new_since_review": True, "same_comparable_periods": True,
                   "financial_report_id": "FR1", "rcept_no": "20260101000001"},
        disclosures=[],
    )
    print(result["review_priority"]["grade"])  # '높음' 같은 값
"""
from __future__ import annotations

from typing import Any

from .domain_map import D_RULE_IDS_V40, F_RULE_IDS_V40, RULE_DOMAIN_V40

RULE_VERSION_DEFAULT = "4.0"

# 임계값·밴드·기본점수는 전부 여기 하나로 모은다 - 아래 로직 안에서 숫자를 직접 쓰지 않는다.
# LEVELS는 "추가판단"용 3단계 밴드(intensity_bonus 0~3), MIN_RATE/MIN_DELTA_PP는 룰이
# 아예 발동하는 "최초 후보" 임계값 - 스펙 5번 표의 두 컬럼 그대로.
THRESHOLDS: dict[str, Any] = {
    "F1": {"BASE": 6, "LEVELS": (5, 10, 20)},
    "F2": {"BASE": 6, "LEVELS": (5, 10, 20)},
    "F3": {"BASE": 3, "LEVELS": (20, 30, 50), "MIN_RATE": 0.10},
    "F4": {"BASE": 3, "LEVELS": (20, 40, 80), "MIN_DELTA_PP": 20},
    "F5": {"BASE": 4, "LEVELS": (5, 10, 20), "MIN_RATE": 0.20},
    "F6": {"BASE": 3, "LEVELS": (30, 50, 80), "MIN_RATE": 0.30},
    "F7": {"BASE": 3, "LEVELS": (30, 50, 80), "MIN_RATE": 0.30},
    "F8": {"BASE": 10},
    "D1": {"BASE": 4, "LEVELS": (5, 10, 20)},
    "D2": {"BASE": 5, "LEVELS": (10, 20, 30)},
    "D3": {"BASE": 10},
    "D4": {"BASE": 5},
    "D5_TYPO": {"BASE": 1},
    "D5_MINOR_CHANGE": {"BASE": 3},
    "D5_MAJOR_AMOUNT": {"BASE": 6},
    "D5_CORE_FINANCIAL": {"BASE": 9},
    "D6A": {"BASE": 4, "LEVELS": (5, 10, 20)},
    "D6B": {"BASE": 4, "LEVELS": (5, 10, 20)},
    "D6C": {"BASE": 4, "LEVELS": (5, 10, 20)},
    "D6D": {"BASE": 5, "LEVELS": (5, 10, 20)},
    "D7": {"BASE": 10},
    "D8": {"BASE": 10},
    "D9": {"BASE": 7},
}

GRADE_RANK = {"높음": 2, "중간": 1, "낮음": 0}


def _grade_from_score(score: int) -> str:
    if score >= 8:
        return "높음"
    if score >= 4:
        return "중간"
    return "낮음"


def _band(value: float, levels: tuple[float, float, float]) -> int:
    lv1, lv2, lv3 = levels
    if value >= lv3:
        return 3
    if value >= lv2:
        return 2
    if value >= lv1:
        return 1
    return 0


def _any_none(d: dict, keys: tuple[str, ...]) -> bool:
    return any(d.get(k) is None for k in keys)


def _safe_ratio(numerator: Any, denominator: Any) -> tuple[float, list[str]]:
    """분모가 결측/0 이하 -> 0 + status에 RATIO_NOT_COMPUTABLE. 그 외엔 정상 비율(%)."""
    if numerator is None or denominator is None or denominator <= 0:
        return 0.0, ["RATIO_NOT_COMPUTABLE"]
    return (numerator / denominator) * 100.0, []


def _rule_sort_key(rule_id: str) -> tuple[int, str]:
    """F가 D보다 먼저, 그다음 rule_id 사전순 - 동점 최종 tiebreak(스펙 8-6)."""
    return (0 if rule_id.startswith("F") else 1, rule_id)


def _make_event(
    event_id: str, rule_id: str, event_group_id: str | None,
    base_score: int, intensity_bonus: int, is_emergency: bool,
    direction: str, raw_change: float | None, prev_value: Any, curr_value: Any,
    reason_text: str, evidence_rcept_no: str | None, evidence_report_id: str | None,
    status: list[str],
) -> dict:
    score = max(1, min(10, base_score + intensity_bonus))
    grade = "긴급확인" if is_emergency else _grade_from_score(score)
    return {
        "event_id": event_id, "rule_id": rule_id, "domain": RULE_DOMAIN_V40[rule_id],
        "event_group_id": event_group_id,
        "grade": grade, "score": score, "is_emergency": is_emergency,
        "direction": direction, "raw_change": raw_change,
        "prev_value": prev_value, "curr_value": curr_value,
        "reason_text": reason_text,
        "evidence_rcept_no": evidence_rcept_no, "evidence_report_id": evidence_report_id,
        "status": status,
    }


# ---------------------------------------------------------------------------
# F 규칙 (재무) - F1/F2/F3는 양방향(긍정/부정 모두 탐지), F6/F7은 신설
# ---------------------------------------------------------------------------

def _evaluate_financial_rules(
    corp_code: str, review_date: str, financial: dict,
) -> tuple[list[dict], dict[str, str], bool, bool]:
    events: list[dict] = []
    status: dict[str, str] = {}
    group_id = financial.get("event_group_id")
    report_id = financial.get("financial_report_id")
    rcept_no = financial.get("rcept_no")

    def eid(rule_id: str) -> str:
        return f"{rule_id}_{corp_code}_{review_date}"

    # F8을 가장 먼저 본다 - F4를 적용제외로 돌릴지 여기서 결정해야 한다.
    f8_hit = False
    if _any_none(financial, ("prev_equity", "curr_equity")):
        status["F8"] = "NOT_EVALUATED"
    else:
        prev_equity, curr_equity = financial["prev_equity"], financial["curr_equity"]
        if prev_equity > 0 and curr_equity <= 0:
            f8_hit = True
            t = THRESHOLDS["F8"]
            events.append(_make_event(
                eid("F8"), "F8", group_id, t["BASE"], 0, True, "부정", None,
                prev_equity, curr_equity,
                f"자기자본 상태 전환(양수 -> 0 이하): 전년 동기 {prev_equity:,}원 -> 최신 {curr_equity:,}원",
                rcept_no, report_id, [],
            ))
            status["F8"] = "HIT"
        else:
            status["F8"] = "NO_HIT"

    # F1: 영업이익 부호 전환 (흑자<->적자, 양방향). 0원은 흑자/적자 어느 쪽도 아니라고
    # 보고 전환으로 치지 않는다(스펙: "0원인 경우는 구분해 별도 기록" - 별도 규칙이
    # 명시되지 않아 지금은 전환 미발생으로 처리, 추정하지 않음).
    f1_hit = False
    if _any_none(financial, ("prev_op", "curr_op")):
        status["F1"] = "NOT_EVALUATED"
    else:
        prev_op, curr_op = financial["prev_op"], financial["curr_op"]
        if (prev_op > 0 and curr_op < 0) or (prev_op < 0 and curr_op > 0):
            f1_hit = True
            t = THRESHOLDS["F1"]
            b_val, b_status = _safe_ratio(abs(curr_op - prev_op), financial.get("curr_sales"))
            direction = "부정" if prev_op > 0 else "긍정"
            label = "흑자->적자 전환" if prev_op > 0 else "적자->흑자 전환"
            events.append(_make_event(
                eid("F1"), "F1", group_id, t["BASE"], _band(b_val, t["LEVELS"]), False, direction, b_val,
                prev_op, curr_op,
                f"영업이익 {label} (전년 동기 {prev_op:,}원 -> 최신 {curr_op:,}원)",
                rcept_no, report_id, b_status,
            ))
            status["F1"] = "HIT"
        else:
            status["F1"] = "NO_HIT"

    # F2: 영업활동현금흐름 부호 전환 (양방향)
    f2_hit = False
    if _any_none(financial, ("prev_cfo", "curr_cfo")):
        status["F2"] = "NOT_EVALUATED"
    else:
        prev_cfo, curr_cfo = financial["prev_cfo"], financial["curr_cfo"]
        if (prev_cfo > 0 and curr_cfo < 0) or (prev_cfo < 0 and curr_cfo > 0):
            f2_hit = True
            t = THRESHOLDS["F2"]
            b_val, b_status = _safe_ratio(abs(curr_cfo - prev_cfo), financial.get("curr_sales"))
            direction = "부정" if prev_cfo > 0 else "긍정"
            label = "양수->음수 전환" if prev_cfo > 0 else "음수->양수 전환"
            events.append(_make_event(
                eid("F2"), "F2", group_id, t["BASE"], _band(b_val, t["LEVELS"]), False, direction, b_val,
                prev_cfo, curr_cfo,
                f"영업활동현금흐름 {label} (전년 동기 {prev_cfo:,}원 -> 최신 {curr_cfo:,}원)",
                rcept_no, report_id, b_status,
            ))
            status["F2"] = "HIT"
        else:
            status["F2"] = "NO_HIT"

    # 긴급확인 조건(스펙 7-7): F1+F2가 같은 신규 보고기간(이 호출은 financial 하나뿐이라
    # 둘 다 HIT이면 자동으로 같은 보고기간)에서 함께 발생 -> 둘 다 긴급확인으로 올림.
    if f1_hit and f2_hit:
        for ev in events:
            if ev["rule_id"] in ("F1", "F2"):
                ev["is_emergency"] = True
                ev["grade"] = "긴급확인"

    # F3: 매출 변화 (양방향 - 증가도 긍정 후보로 탐지)
    if _any_none(financial, ("prev_sales", "curr_sales")):
        status["F3"] = "NOT_EVALUATED"
    else:
        prev_sales, curr_sales = financial["prev_sales"], financial["curr_sales"]
        t = THRESHOLDS["F3"]
        if prev_sales > 0:
            rate = (curr_sales - prev_sales) / prev_sales * 100.0
            if abs(rate) >= t["MIN_RATE"] * 100:
                direction = "긍정" if rate > 0 else "부정"
                events.append(_make_event(
                    eid("F3"), "F3", group_id, t["BASE"], _band(abs(rate), t["LEVELS"]), False, direction, rate,
                    prev_sales, curr_sales,
                    f"매출액 전년 동기 대비 {rate:+.1f}% {'증가' if rate > 0 else '감소'}",
                    rcept_no, report_id, [],
                ))
                status["F3"] = "HIT"
            else:
                status["F3"] = "NO_HIT"
        else:
            status["F3"] = "NO_HIT"

    # F4 - F8이 HIT면 부채비율 비교 자체가 부적절(스펙 9번 표) -> 적용제외.
    # v3.3-core는 이 경우를 NOT_EVALUATED에 욱여넣었는데(이벤트가 없어 사유를 어디에도
    # 못 남겼음), v4.0은 rule_status에 EXCLUDED를 따로 둬서 "판정불가"(데이터 없음)와
    # "적용제외"(비교 자체가 부적절)를 명확히 구분한다.
    if f8_hit:
        status["F4"] = "EXCLUDED"
    elif _any_none(financial, ("prev_equity", "curr_equity", "prev_debt", "curr_debt")):
        status["F4"] = "NOT_EVALUATED"
    else:
        prev_equity, curr_equity = financial["prev_equity"], financial["curr_equity"]
        prev_debt, curr_debt = financial["prev_debt"], financial["curr_debt"]
        t = THRESHOLDS["F4"]
        if prev_equity > 0 and curr_equity > 0:
            delta = (curr_debt / curr_equity - prev_debt / prev_equity) * 100.0
            if delta >= t["MIN_DELTA_PP"]:
                events.append(_make_event(
                    eid("F4"), "F4", group_id, t["BASE"], _band(delta, t["LEVELS"]), False, "부정", delta,
                    round(100 * prev_debt / prev_equity, 1), round(100 * curr_debt / curr_equity, 1),
                    f"부채비율 {delta:.1f}%p 상승",
                    rcept_no, report_id, [],
                ))
                status["F4"] = "HIT"
            else:
                status["F4"] = "NO_HIT"
        else:
            status["F4"] = "NO_HIT"

    # F5: 총차입금 증가
    if _any_none(financial, ("prev_borrowings", "curr_borrowings")):
        status["F5"] = "NOT_EVALUATED"
    else:
        prev_b, curr_b = financial["prev_borrowings"], financial["curr_borrowings"]
        t = THRESHOLDS["F5"]
        if prev_b > 0 and (curr_b - prev_b) / prev_b >= t["MIN_RATE"]:
            b_val, b_status = _safe_ratio(curr_b - prev_b, financial.get("prev_assets"))
            rate = (curr_b - prev_b) / prev_b * 100.0
            events.append(_make_event(
                eid("F5"), "F5", group_id, t["BASE"], _band(b_val, t["LEVELS"]), False, "부정", rate,
                prev_b, curr_b,
                f"총차입금 {rate:.1f}% 증가 (전년 동기 {prev_b:,}원 -> 최신 {curr_b:,}원)",
                rcept_no, report_id, b_status,
            ))
            status["F5"] = "HIT"
        else:
            status["F5"] = "NO_HIT"

    # F6(신설): 흑자 유지 중(양수->양수) 영업이익 증감. 기존 v3.3의 "F7(YoY+50%)"을
    # 대체 - 이쪽이 "흑자 유지"라는 조건을 명시해서 F1과 안 겹친다.
    if _any_none(financial, ("prev_op", "curr_op")):
        status["F6"] = "NOT_EVALUATED"
    else:
        prev_op, curr_op = financial["prev_op"], financial["curr_op"]
        t = THRESHOLDS["F6"]
        if prev_op > 0 and curr_op > 0:
            rate = (curr_op - prev_op) / prev_op * 100.0
            if abs(rate) >= t["MIN_RATE"] * 100:
                direction = "긍정" if rate > 0 else "부정"
                events.append(_make_event(
                    eid("F6"), "F6", group_id, t["BASE"], _band(abs(rate), t["LEVELS"]), False, direction, rate,
                    prev_op, curr_op,
                    f"흑자 유지 중 영업이익 {rate:+.1f}% {'증가' if rate > 0 else '감소'}",
                    rcept_no, report_id, [],
                ))
                status["F6"] = "HIT"
            else:
                status["F6"] = "NO_HIT"
        else:
            status["F6"] = "NO_HIT"

    # F7(신설): 적자 유지 중(음수->음수) 영업손실 증감.
    if _any_none(financial, ("prev_op", "curr_op")):
        status["F7"] = "NOT_EVALUATED"
    else:
        prev_op, curr_op = financial["prev_op"], financial["curr_op"]
        t = THRESHOLDS["F7"]
        if prev_op < 0 and curr_op < 0:
            # prev_op<0이라 abs(prev_op)로 나눔 - rate>0이면 손실 축소(curr이 0에 더 가까움),
            # rate<0이면 손실 확대.
            rate = (curr_op - prev_op) / abs(prev_op) * 100.0
            if abs(rate) >= t["MIN_RATE"] * 100:
                direction = "긍정" if rate > 0 else "부정"
                events.append(_make_event(
                    eid("F7"), "F7", group_id, t["BASE"], _band(abs(rate), t["LEVELS"]), False, direction, rate,
                    prev_op, curr_op,
                    f"적자 유지 중 영업손실 {'축소' if rate > 0 else '확대'} ({abs(rate):.1f}%)",
                    rcept_no, report_id, [],
                ))
                status["F7"] = "HIT"
            else:
                status["F7"] = "NO_HIT"
        else:
            status["F7"] = "NO_HIT"

    return events, status, f1_hit, f2_hit


# ---------------------------------------------------------------------------
# D 규칙 (공시) - D5는 4단계 재분류, D6은 D6A~D6D로 분할, D7/D8/D9 신설(긴급확인 전용)
# ---------------------------------------------------------------------------

_D6_LABEL = {
    "D6A": "신규 공급계약·수주", "D6B": "신규 시설투자",
    "D6C": "타법인 출자·인수", "D6D": "중요 계약 해지",
}


def _evaluate_disclosure_rules(disclosures: list[dict]) -> tuple[list[dict], dict[str, str]]:
    events: list[dict] = []
    created_event = dict.fromkeys(D_RULE_IDS_V40, False)
    not_evaluated_any = dict.fromkeys(D_RULE_IDS_V40, False)

    for disc in disclosures:
        disc_type = disc.get("disc_type")
        if disc_type not in D_RULE_IDS_V40:
            continue
        rcept_no = disc.get("rcept_no")
        rcept_dt = disc.get("rcept_dt")
        group_id = disc.get("event_group_id")
        structured = disc.get("structured") or {}

        if disc_type == "D1":
            t = THRESHOLDS["D1"]
            b_val, b_status = _safe_ratio(structured.get("amount"), structured.get("total_assets"))
            events.append(_make_event(
                f"D1_{rcept_no}", "D1", group_id, t["BASE"], _band(b_val, t["LEVELS"]), False, "부정", b_val,
                None, structured.get("amount"),
                f"단기차입금 증가 결정 공시 ({rcept_dt})", rcept_no, None, b_status,
            ))
            created_event["D1"] = True

        elif disc_type == "D2":
            t = THRESHOLDS["D2"]
            b_val, b_status = _safe_ratio(structured.get("new_shares"), structured.get("old_shares"))
            events.append(_make_event(
                f"D2_{rcept_no}", "D2", group_id, t["BASE"], _band(b_val, t["LEVELS"]), False, "부정", b_val,
                None, structured.get("new_shares"),
                f"유상증자 결정 공시 ({rcept_dt})", rcept_no, None, b_status,
            ))
            created_event["D2"] = True

        elif disc_type == "D3":
            t = THRESHOLDS["D3"]
            events.append(_make_event(
                f"D3_{rcept_no}", "D3", group_id, t["BASE"], 0, True, "부정", None, None, None,
                f"감사의견 비적정(의견거절·부적정·중요한정) 공시 ({rcept_dt})", rcept_no, None, [],
            ))
            created_event["D3"] = True

        elif disc_type == "D4":
            t = THRESHOLDS["D4"]
            events.append(_make_event(
                f"D4_{rcept_no}", "D4", group_id, t["BASE"], 0, False, "중립", None, None, None,
                f"최대주주 변경 공시 ({rcept_dt})", rcept_no, None, [],
            ))
            created_event["D4"] = True

        elif disc_type == "D5":
            ctype = disc.get("correction_type")
            if ctype == "TYPO":
                t = THRESHOLDS["D5_TYPO"]
                events.append(_make_event(
                    f"D5_{rcept_no}", "D5", group_id, t["BASE"], 0, False, "중립", None, None, None,
                    f"오탈자 수준 정정공시 ({rcept_dt})", rcept_no, None, [],
                ))
                created_event["D5"] = True
            elif ctype == "MINOR_CHANGE":
                t = THRESHOLDS["D5_MINOR_CHANGE"]
                events.append(_make_event(
                    f"D5_{rcept_no}", "D5", group_id, t["BASE"], 0, False, "중립", None, None, None,
                    f"일반 조건 변경 정정공시 ({rcept_dt})", rcept_no, None, [],
                ))
                created_event["D5"] = True
            elif ctype == "MAJOR_AMOUNT":
                t = THRESHOLDS["D5_MAJOR_AMOUNT"]
                events.append(_make_event(
                    f"D5_{rcept_no}", "D5", group_id, t["BASE"], 0, False, "부정", None, None, None,
                    f"중요 금액 변경 정정공시 ({rcept_dt})", rcept_no, None, [],
                ))
                created_event["D5"] = True
            elif ctype == "CORE_FINANCIAL":
                t = THRESHOLDS["D5_CORE_FINANCIAL"]
                # 긴급확인(스펙 7-6)은 "핵심재무 정정"이라고 전부가 아니라, 그걸로 손익
                # 상태나 자기자본 상태가 실제로 달라진 경우만 - structured로 좁힌다.
                is_emergency = structured.get("changes_pl_or_equity_state") is True
                extra = " - 손익/자기자본 상태 변경 동반" if is_emergency else ""
                events.append(_make_event(
                    f"D5_{rcept_no}", "D5", group_id, t["BASE"], 0, is_emergency, "부정", None, None, None,
                    f"핵심 재무수치 정정공시 ({rcept_dt}){extra}", rcept_no, None, [],
                ))
                created_event["D5"] = True
            else:  # "UNKNOWN" 또는 None - 정정 내용을 확인 못 함
                not_evaluated_any["D5"] = True

        elif disc_type in ("D6A", "D6B", "D6C", "D6D"):
            t = THRESHOLDS[disc_type]
            direction = "부정" if disc_type == "D6D" else "긍정"
            # 분자/분모 조합 후보 - structured에 있는 것을 쓴다(계약금액/연매출 우선,
            # 없으면 투자·취득금액/총자산).
            if structured.get("amount") is not None and structured.get("annual_sales") is not None:
                b_val, b_status = _safe_ratio(structured.get("amount"), structured.get("annual_sales"))
            elif structured.get("amount") is not None and structured.get("total_assets") is not None:
                b_val, b_status = _safe_ratio(structured.get("amount"), structured.get("total_assets"))
            else:
                b_val, b_status = 0.0, ["RATIO_NOT_COMPUTABLE"]
            events.append(_make_event(
                f"{disc_type}_{rcept_no}", disc_type, group_id, t["BASE"], _band(b_val, t["LEVELS"]), False, direction, b_val,
                None, structured.get("amount"),
                f"{_D6_LABEL[disc_type]} 공시 ({rcept_dt})", rcept_no, None, b_status,
            ))
            created_event[disc_type] = True

        elif disc_type == "D7":
            t = THRESHOLDS["D7"]
            events.append(_make_event(
                f"D7_{rcept_no}", "D7", group_id, t["BASE"], 0, True, "부정", None, None, None,
                f"관리종목 지정·상장적격성 실질심사·상장폐지 관련 공시 ({rcept_dt})", rcept_no, None, [],
            ))
            created_event["D7"] = True

        elif disc_type == "D8":
            t = THRESHOLDS["D8"]
            events.append(_make_event(
                f"D8_{rcept_no}", "D8", group_id, t["BASE"], 0, True, "부정", None, None, None,
                f"매매거래정지·회생절차개시신청·부도 관련 공시 ({rcept_dt})", rcept_no, None, [],
            ))
            created_event["D8"] = True

        elif disc_type == "D9":
            t = THRESHOLDS["D9"]
            events.append(_make_event(
                f"D9_{rcept_no}", "D9", group_id, t["BASE"], 0, False, "부정", None, None, None,
                f"중대한 영업정지·핵심사업중단 공시 ({rcept_dt})", rcept_no, None, [],
            ))
            created_event["D9"] = True

    status: dict[str, str] = {}
    for rid in D_RULE_IDS_V40:
        if created_event[rid]:
            status[rid] = "HIT"
        elif not_evaluated_any[rid]:
            status[rid] = "NOT_EVALUATED"
        else:
            status[rid] = "NO_HIT"

    return events, status


# ---------------------------------------------------------------------------
# 그룹 중복 처리 + 최종 집계 (스펙 8번: 점수 합산 없이 "가장 중요한 단일 사건")
# ---------------------------------------------------------------------------

def _apply_group_dedup(events: list[dict]) -> list[dict]:
    """같은 event_group_id를 가진 이벤트들 중 하나만 "최종 후보"로 남기고(긴급확인 우선,
    그다음 점수 높은 순, 그래도 같으면 F가 D보다 먼저) 나머지는 status에
    GROUPED_DUPLICATE를 추가한다(events 리스트에서 제거하지는 않음 - 스펙 9번: "두 사유
    모두 보존")."""
    groups: dict[str, list[int]] = {}
    for i, ev in enumerate(events):
        gid = ev.get("event_group_id")
        if gid:
            groups.setdefault(gid, []).append(i)

    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        ranked = sorted(
            idxs,
            key=lambda i: (
                0 if events[i]["is_emergency"] else 1,
                -events[i]["score"],
                _rule_sort_key(events[i]["rule_id"]),
            ),
        )
        for i in ranked[1:]:
            if "GROUPED_DUPLICATE" not in events[i]["status"]:
                events[i]["status"].append("GROUPED_DUPLICATE")
    return events


def _pick_final(events: list[dict], rule_status: dict[str, str]) -> dict:
    """스펙 8번 순서 그대로: ①긴급확인 여부 ②가장 중요한 개별 사건의 등급 ③대표
    사건의 점수 ④실제 변화 규모(원시 %) ⑤추가 독립 중요사건 개수(부가정보, 등급은
    안 바꿈) ⑥rule_id(결정론적 최종 타이브레이크). "작은 사건 여러 개를 더해서 높은
    등급을 만들지 않는다"(스펙 8번 핵심 원칙) - sum() 호출이 이 함수 어디에도 없다."""
    candidates = [ev for ev in events if "GROUPED_DUPLICATE" not in ev["status"]]

    def raw_abs(ev: dict) -> float:
        return abs(ev["raw_change"]) if ev["raw_change"] is not None else 0.0

    emergency = [ev for ev in candidates if ev["is_emergency"]]
    if emergency:
        best = sorted(emergency, key=lambda ev: (-ev["score"], -raw_abs(ev), _rule_sort_key(ev["rule_id"])))[0]
        others = [ev for ev in candidates if ev is not best and GRADE_RANK.get(ev["grade"], -1) >= GRADE_RANK["중간"]]
        return {
            "grade": "긴급확인", "score": best["score"], "is_emergency": True,
            "top_event_id": best["event_id"], "reason_text": best["reason_text"],
            "additional_important_events": len(others),
        }

    graded = [ev for ev in candidates if ev["grade"] in GRADE_RANK]
    if graded:
        best = sorted(
            graded,
            key=lambda ev: (-GRADE_RANK[ev["grade"]], -ev["score"], -raw_abs(ev), _rule_sort_key(ev["rule_id"])),
        )[0]
        others = [ev for ev in graded if ev is not best and GRADE_RANK[ev["grade"]] >= GRADE_RANK["중간"]]
        return {
            "grade": best["grade"], "score": best["score"], "is_emergency": False,
            "top_event_id": best["event_id"], "reason_text": best["reason_text"],
            "additional_important_events": len(others),
        }

    # 후보 이벤트가 하나도 없음 - v3.3-core와 동일 해석(테스트로 검증됨, 스펙 9번
    # 표와도 일치): 판정불가(NOT_EVALUATED) 룰이 하나라도 있으면 "판정불가",
    # 전부 SKIPPED/NO_HIT/EXCLUDED뿐이면 "변화없음".
    not_evaluated_rules = sorted(rid for rid, s in rule_status.items() if s == "NOT_EVALUATED")
    if not_evaluated_rules:
        return {
            "grade": "판정불가", "score": None, "is_emergency": False,
            "top_event_id": None, "reason_text": f"데이터 부족으로 판정 불가 ({', '.join(not_evaluated_rules)})",
            "additional_important_events": 0,
        }
    return {
        "grade": "변화없음", "score": None, "is_emergency": False,
        "top_event_id": None, "reason_text": "마지막 검토일 이후 유의미한 변화 없음",
        "additional_important_events": 0,
    }


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def evaluate(
    corp_code: str, review_date: str, data_cutoff: str,
    financial: dict | None, disclosures: list[dict],
    rule_version: str = RULE_VERSION_DEFAULT,
) -> dict:
    rule_status: dict[str, str] = {}
    events: list[dict] = []

    if financial is None or not financial.get("financial_new_since_review", False):
        for rid in F_RULE_IDS_V40:
            rule_status[rid] = "SKIPPED_NO_NEW_FINANCIAL"
    elif not financial.get("same_comparable_periods", False):
        for rid in F_RULE_IDS_V40:
            rule_status[rid] = "NOT_EVALUATED"
    else:
        f_events, f_status, _f1_hit, _f2_hit = _evaluate_financial_rules(corp_code, review_date, financial)
        events.extend(f_events)
        rule_status.update(f_status)

    d_events, d_status = _evaluate_disclosure_rules(disclosures)
    events.extend(d_events)
    rule_status.update(d_status)

    events = _apply_group_dedup(events)
    review_priority = _pick_final(events, rule_status)

    return {
        "corp_code": corp_code,
        "rule_version": rule_version,
        "review_date": review_date,
        "data_cutoff": data_cutoff,
        "review_priority": review_priority,
        "events": events,
        "rule_status": rule_status,
    }
