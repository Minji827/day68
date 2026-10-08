"""DeltaWatch Rule Engine v3.3-core.

순수 함수 엔진 - DB/API 접근 없음. 정규화된 dict를 받아 dict를 반환한다.
모든 판정·점수·등급은 이 코드가 계산하며(LLM 호출 없음), 데이터가 없으면 추정하지
않고 NOT_EVALUATED로 표시한다. 같은 입력이면 항상 같은 출력이다(random/datetime.now()
미사용, 날짜는 전부 인자로 받음).

이 스펙을 구현하며 두 군데는 글자 그대로는 모순/미정의라 아래처럼 해석해서 구현했다
(각주 참고): (1) 최종 등급이 "이벤트 없음"일 때 NONE과 NOT_EVALUATED을 가르는 기준,
(2) 같은 event_group_id 안에서 A가 동점일 때의 2차 타이브레이크.

Usage:
    from src.rules.rule_engine_v33 import evaluate
    result = evaluate(
        corp_code="00126380", review_date="2026-10-08", data_cutoff="2026-10-08",
        financial={"prev_op": 1000, "curr_op": -500, "curr_sales": 10000,
                   "financial_new_since_review": True, "same_comparable_periods": True,
                   "financial_report_id": "FR1", "rcept_no": "20260101000001"},
        disclosures=[],
    )
    print(result["review_priority"]["p"])  # 'P2' 같은 값
"""
from __future__ import annotations

from typing import Any

from .domain_map import D_RULE_IDS, F_RULE_IDS, RULE_DOMAIN

RULE_VERSION_DEFAULT = "3.3-core"

# 임계값·구간·P 경계는 전부 여기 하나로 모은다 - 아래 로직 안에서 숫자를 직접 쓰지 않는다.
THRESHOLDS: dict[str, Any] = {
    "F1": {"A": 3, "B_LEVELS": (5, 10, 20), "BASE_U": "U2"},
    "F2": {"A": 3, "B_LEVELS": (5, 10, 20), "BASE_U": "U2"},
    "F3": {"A": 2, "B_LEVELS": (10, 20, 30), "BASE_U": "U2", "MIN_SALES_RATIO": 0.90},
    "F4": {"A": 2, "B_LEVELS": (20, 30, 50), "BASE_U": "U2", "MIN_DELTA_PP": 20},
    "F5": {"A": 2, "B_LEVELS": (5, 10, 20), "BASE_U": "U2", "MIN_GROWTH_RATE": 0.20},
    "F6": {"A": 3, "B_LEVELS": (5, 10, 20), "BASE_U": "U2"},
    "F7": {"A": 2, "B_LEVELS": (2, 5, 10), "BASE_U": "U1", "MIN_GROWTH_RATE": 0.50},
    "F8": {"A": 3, "BASE_U": "U3"},
    "D1": {"A": 2, "B_LEVELS": (5, 10, 20), "BASE_U": "U2"},
    "D2": {"A": 2, "B_LEVELS": (5, 10, 20), "BASE_U": "U2"},
    "D3": {"A": 3, "BASE_U": "U3"},
    "D4": {"A": 2, "BASE_U": "U2"},
    "D6": {"A": 2, "B_LEVELS": (5, 10, 20), "BASE_U": "U2"},
    "D5_TYPO": {"A": 0, "BASE_U": "U0"},
    "D5_MATERIAL_FINANCIAL": {"A": 3, "BASE_U": "U3"},
}
F1_U3_B_THRESHOLD = 20  # F1의 B산식 값이 이 이상이면 해당 F1 이벤트는 강제 U3

U_ORDER = {"U0": 0, "U1": 1, "U2": 2, "U3": 3}
P_ORDER = {"P1": 0, "P2": 1, "P3": 2}  # 작을수록 급함 - 최종 후보 정렬용 (NONE/NOT_EVALUATED는 후보 아님)


def compute_c(*_args: Any, **_kwargs: Any) -> int:
    """TODO(v3.4): 정성적 보정 가산(c). 아직 설계 전 - v3.3-core는 0 고정."""
    return 0


def compute_d(*_args: Any, **_kwargs: Any) -> int:
    """TODO(v3.4): 업종/상황별 가중치(d). 아직 설계 전 - v3.3-core는 0 고정."""
    return 0


def _p_from_u_m(u: str, m: float) -> str:
    """P 결정표. U0은 항상 NONE - 후보에서 제외되므로(_pick_final 참고) 사실상
    도달 경로가 없지만 이벤트 자체의 p 필드 계산에는 그대로 쓰인다."""
    if u == "U3":
        return "P1"
    if u == "U2":
        return "P1" if m >= 7 else ("P2" if m >= 1 else "P3")
    if u == "U1":
        return "P2" if m >= 7 else "P3"
    return "NONE"  # U0


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
    """B 분모가 결측/0 이하 -> B=0 + status에 B_NOT_COMPUTABLE. 그 외엔 정상 비율(%)."""
    if numerator is None or denominator is None or denominator <= 0:
        return 0.0, ["B_NOT_COMPUTABLE"]
    return (numerator / denominator) * 100.0, []


def _rule_sort_key(rule_id: str) -> tuple[int, str]:
    """F가 D보다 먼저, 그다음 rule_id 사전순 - 스펙 ⑦의 동점 tiebreak."""
    return (0 if rule_id.startswith("F") else 1, rule_id)


def _make_event(
    event_id: str, rule_id: str, event_group_id: str | None,
    a: int, b: int, c: int, d: int, u: str,
    reason_text: str, evidence_rcept_no: str | None, evidence_report_id: str | None,
    status: list[str],
) -> dict:
    m = a + b + c + d
    return {
        "event_id": event_id, "rule_id": rule_id, "domain": RULE_DOMAIN[rule_id],
        "event_group_id": event_group_id,
        "a": a, "b": b, "c": c, "d": d, "m": m, "u": u, "p": _p_from_u_m(u, m),
        "reason_text": reason_text,
        "evidence_rcept_no": evidence_rcept_no, "evidence_report_id": evidence_report_id,
        "status": status,
    }


# ---------------------------------------------------------------------------
# F 규칙 (재무)
# ---------------------------------------------------------------------------

def _evaluate_financial_rules(
    corp_code: str, review_date: str, financial: dict, c: int, d: int,
) -> tuple[list[dict], dict[str, str]]:
    events: list[dict] = []
    status: dict[str, str] = {}
    group_id = financial.get("event_group_id")
    report_id = financial.get("financial_report_id")
    rcept_no = financial.get("rcept_no")

    def eid(rule_id: str) -> str:
        return f"{rule_id}_{corp_code}_{review_date}"

    # F8을 가장 먼저 본다 - F4를 NOT_EVALUATED로 강제할지 여기서 결정해야 한다.
    f8_hit = False
    if _any_none(financial, ("prev_equity", "curr_equity")):
        status["F8"] = "NOT_EVALUATED"
    else:
        prev_equity, curr_equity = financial["prev_equity"], financial["curr_equity"]
        if prev_equity > 0 and curr_equity <= 0:
            f8_hit = True
            t = THRESHOLDS["F8"]
            events.append(_make_event(
                eid("F8"), "F8", group_id, t["A"], 0, c, d, t["BASE_U"],
                f"자본잠식 발생 (전년 동기 {prev_equity:,}원 -> 최신 {curr_equity:,}원)",
                rcept_no, report_id, [],
            ))
            status["F8"] = "HIT"
        else:
            status["F8"] = "NO_HIT"

    # F1
    f1_hit = False
    if _any_none(financial, ("prev_op", "curr_op")):
        status["F1"] = "NOT_EVALUATED"
    else:
        prev_op, curr_op = financial["prev_op"], financial["curr_op"]
        if prev_op > 0 and curr_op < 0:
            f1_hit = True
            t = THRESHOLDS["F1"]
            b_val, b_status = _safe_ratio(abs(curr_op - prev_op), financial.get("curr_sales"))
            u = "U3" if b_val >= F1_U3_B_THRESHOLD else t["BASE_U"]
            events.append(_make_event(
                eid("F1"), "F1", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, u,
                f"영업이익 흑자->적자 전환 (전년 동기 {prev_op:,}원 -> 최신 {curr_op:,}원)",
                rcept_no, report_id, b_status,
            ))
            status["F1"] = "HIT"
        else:
            status["F1"] = "NO_HIT"

    # F2
    f2_hit = False
    if _any_none(financial, ("prev_cfo", "curr_cfo")):
        status["F2"] = "NOT_EVALUATED"
    else:
        prev_cfo, curr_cfo = financial["prev_cfo"], financial["curr_cfo"]
        if prev_cfo > 0 and curr_cfo < 0:
            f2_hit = True
            t = THRESHOLDS["F2"]
            b_val, b_status = _safe_ratio(abs(curr_cfo - prev_cfo), financial.get("curr_sales"))
            events.append(_make_event(
                eid("F2"), "F2", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, t["BASE_U"],
                f"영업활동현금흐름 흑자->적자 전환 (전년 동기 {prev_cfo:,}원 -> 최신 {curr_cfo:,}원)",
                rcept_no, report_id, b_status,
            ))
            status["F2"] = "HIT"
        else:
            status["F2"] = "NO_HIT"

    # U3 강제조건: F1+F2가 같은 financial_report_id(이 호출 전체가 financial 하나뿐이라
    # 둘 다 HIT이면 자동으로 같은 보고서)에서 함께 HIT -> 둘 다 U3로 올린다.
    if f1_hit and f2_hit:
        for ev in events:
            if ev["rule_id"] in ("F1", "F2"):
                ev["u"] = "U3"
                ev["p"] = _p_from_u_m("U3", ev["m"])

    # F3
    if _any_none(financial, ("prev_sales", "curr_sales")):
        status["F3"] = "NOT_EVALUATED"
    else:
        prev_sales, curr_sales = financial["prev_sales"], financial["curr_sales"]
        t = THRESHOLDS["F3"]
        if prev_sales > 0 and curr_sales / prev_sales <= t["MIN_SALES_RATIO"]:
            rate = (prev_sales - curr_sales) / prev_sales * 100.0
            events.append(_make_event(
                eid("F3"), "F3", group_id, t["A"], _band(rate, t["B_LEVELS"]), c, d, t["BASE_U"],
                f"매출액 전년 동기 대비 {rate:.1f}% 감소",
                rcept_no, report_id, [],
            ))
            status["F3"] = "HIT"
        else:
            status["F3"] = "NO_HIT"

    # F4 - F8이 HIT면 그 자체로 NOT_EVALUATED (F8_PRIORITY). 스펙이 이 경우의 "status"를
    # 이벤트 status 리스트에 넣으라고 하는데, NOT_EVALUATED는 애초에 이벤트를 안 만들므로
    # 넣을 곳이 없다 - 코드 주석으로만 사유를 남긴다(반환 스키마는 rule_status 그대로 유지).
    if f8_hit:
        status["F4"] = "NOT_EVALUATED"  # F8_PRIORITY
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
                    eid("F4"), "F4", group_id, t["A"], _band(delta, t["B_LEVELS"]), c, d, t["BASE_U"],
                    f"부채비율 {delta:.1f}%p 상승",
                    rcept_no, report_id, [],
                ))
                status["F4"] = "HIT"
            else:
                status["F4"] = "NO_HIT"
        else:
            status["F4"] = "NO_HIT"

    # F5
    if _any_none(financial, ("prev_borrowings", "curr_borrowings")):
        status["F5"] = "NOT_EVALUATED"
    else:
        prev_b, curr_b = financial["prev_borrowings"], financial["curr_borrowings"]
        t = THRESHOLDS["F5"]
        if prev_b > 0 and (curr_b - prev_b) / prev_b >= t["MIN_GROWTH_RATE"]:
            b_val, b_status = _safe_ratio(curr_b - prev_b, financial.get("prev_assets"))
            rate = (curr_b - prev_b) / prev_b * 100.0
            events.append(_make_event(
                eid("F5"), "F5", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, t["BASE_U"],
                f"차입금 {rate:.1f}% 증가 (전년 동기 {prev_b:,}원 -> 최신 {curr_b:,}원)",
                rcept_no, report_id, b_status,
            ))
            status["F5"] = "HIT"
        else:
            status["F5"] = "NO_HIT"

    # F6
    if _any_none(financial, ("prev_op", "curr_op")):
        status["F6"] = "NOT_EVALUATED"
    else:
        prev_op, curr_op = financial["prev_op"], financial["curr_op"]
        if prev_op < 0 and curr_op > 0:
            t = THRESHOLDS["F6"]
            b_val, b_status = _safe_ratio(abs(curr_op - prev_op), financial.get("curr_sales"))
            events.append(_make_event(
                eid("F6"), "F6", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, t["BASE_U"],
                f"영업이익 적자->흑자 전환 (전년 동기 {prev_op:,}원 -> 최신 {curr_op:,}원)",
                rcept_no, report_id, b_status,
            ))
            status["F6"] = "HIT"
        else:
            status["F6"] = "NO_HIT"

    # F7
    if _any_none(financial, ("prev_op", "curr_op")):
        status["F7"] = "NOT_EVALUATED"
    else:
        prev_op, curr_op = financial["prev_op"], financial["curr_op"]
        t = THRESHOLDS["F7"]
        if prev_op > 0 and curr_op > 0 and (curr_op - prev_op) / prev_op >= t["MIN_GROWTH_RATE"]:
            curr_sales, prev_sales = financial.get("curr_sales"), financial.get("prev_sales")
            if curr_sales is None or prev_sales is None or curr_sales <= 0 or prev_sales <= 0:
                b_val, b_status = 0.0, ["B_NOT_COMPUTABLE"]
            else:
                b_val, b_status = (curr_op / curr_sales - prev_op / prev_sales) * 100.0, []
            rate = (curr_op - prev_op) / prev_op * 100.0
            events.append(_make_event(
                eid("F7"), "F7", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, t["BASE_U"],
                f"영업이익 전년 동기 대비 {rate:.1f}% 증가",
                rcept_no, report_id, b_status,
            ))
            status["F7"] = "HIT"
        else:
            status["F7"] = "NO_HIT"

    return events, status


# ---------------------------------------------------------------------------
# D 규칙 (공시)
# ---------------------------------------------------------------------------

def _evaluate_disclosure_rules(
    disclosures: list[dict], f1_hit: bool, f2_hit: bool, c: int, d: int,
) -> tuple[list[dict], dict[str, str]]:
    events: list[dict] = []
    created_event = dict.fromkeys(D_RULE_IDS, False)
    not_evaluated_any = dict.fromkeys(D_RULE_IDS, False)

    for disc in disclosures:
        disc_type = disc.get("disc_type")
        if disc_type not in D_RULE_IDS:
            continue
        rcept_no = disc.get("rcept_no")
        rcept_dt = disc.get("rcept_dt")
        group_id = disc.get("event_group_id")
        structured = disc.get("structured") or {}

        if disc_type == "D1":
            t = THRESHOLDS["D1"]
            b_val, b_status = _safe_ratio(structured.get("amount"), structured.get("total_assets"))
            events.append(_make_event(
                f"D1_{rcept_no}", "D1", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, t["BASE_U"],
                f"단기차입금 증가 공시 ({rcept_dt})", rcept_no, None, b_status,
            ))
            created_event["D1"] = True

        elif disc_type == "D2":
            t = THRESHOLDS["D2"]
            b_val, b_status = _safe_ratio(structured.get("new_shares"), structured.get("old_shares"))
            u = t["BASE_U"]
            if structured.get("purpose_debt_repayment") is True and (f1_hit or f2_hit):
                u = "U3"
            events.append(_make_event(
                f"D2_{rcept_no}", "D2", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, u,
                f"유상증자 결정 공시 ({rcept_dt})", rcept_no, None, b_status,
            ))
            created_event["D2"] = True

        elif disc_type == "D3":
            t = THRESHOLDS["D3"]
            events.append(_make_event(
                f"D3_{rcept_no}", "D3", group_id, t["A"], 0, c, d, t["BASE_U"],
                f"감사의견 비적정 공시 ({rcept_dt})", rcept_no, None, [],
            ))
            created_event["D3"] = True

        elif disc_type == "D4":
            t = THRESHOLDS["D4"]
            events.append(_make_event(
                f"D4_{rcept_no}", "D4", group_id, t["A"], 0, c, d, t["BASE_U"],
                f"최대주주 변경 공시 ({rcept_dt})", rcept_no, None, [],
            ))
            created_event["D4"] = True

        elif disc_type == "D5":
            correction_type = disc.get("correction_type")
            if correction_type == "TYPO":
                t = THRESHOLDS["D5_TYPO"]
                events.append(_make_event(
                    f"D5_{rcept_no}", "D5", group_id, t["A"], 0, c, d, t["BASE_U"],
                    f"단순 정정공시 ({rcept_dt})", rcept_no, None, [],
                ))
                created_event["D5"] = True
            elif correction_type == "MATERIAL_FINANCIAL":
                t = THRESHOLDS["D5_MATERIAL_FINANCIAL"]
                events.append(_make_event(
                    f"D5_{rcept_no}", "D5", group_id, t["A"], 0, c, d, t["BASE_U"],
                    f"중요 재무 정정공시 ({rcept_dt})", rcept_no, None, [],
                ))
                created_event["D5"] = True
            else:  # "UNKNOWN" 또는 None
                not_evaluated_any["D5"] = True

        elif disc_type == "D6":
            t = THRESHOLDS["D6"]
            if structured.get("contract_amount") is not None and structured.get("annual_sales") is not None:
                b_val, b_status = _safe_ratio(structured.get("contract_amount"), structured.get("annual_sales"))
            elif structured.get("amount") is not None and structured.get("total_assets") is not None:
                b_val, b_status = _safe_ratio(structured.get("amount"), structured.get("total_assets"))
            else:
                b_val, b_status = 0.0, ["B_NOT_COMPUTABLE"]
            events.append(_make_event(
                f"D6_{rcept_no}", "D6", group_id, t["A"], _band(b_val, t["B_LEVELS"]), c, d, t["BASE_U"],
                f"대규모 투자·계약 공시 ({rcept_dt})", rcept_no, None, b_status,
            ))
            created_event["D6"] = True

    status: dict[str, str] = {}
    for rid in D_RULE_IDS:
        if created_event[rid]:
            status[rid] = "HIT"
        elif not_evaluated_any[rid]:
            status[rid] = "NOT_EVALUATED"
        else:
            status[rid] = "NO_HIT"

    return events, status


# ---------------------------------------------------------------------------
# 그룹 중복 처리 + 최종 집계
# ---------------------------------------------------------------------------

def _apply_group_dedup(events: list[dict]) -> list[dict]:
    """같은 event_group_id를 가진 이벤트들 중 A가 가장 높은 하나만 "최종 후보"로 남기고
    나머지는 status에 GROUPED_DUPLICATE를 추가한다(events 리스트에서 제거하지는 않음).

    A가 동점일 때의 2차 타이브레이크는 스펙에 명시가 없어 직접 정함: U가 더 급한 쪽
    (U3>U2>U1>U0)을 우선하고, 그래도 같으면 ⑦과 동일하게 F가 D보다 먼저. 근거: 테스트
    케이스 12(D5 MATERIAL_FINANCIAL(A=3,U3) + F1(A=3,U2) 동일 그룹 -> 기업 P1)가 A만으로는
    풀리지 않는 동점이라, U가 더 급한 D5가 대표로 남아야 결과가 P1이 된다."""
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
            key=lambda i: (-events[i]["a"], -U_ORDER[events[i]["u"]], _rule_sort_key(events[i]["rule_id"])),
        )
        for i in ranked[1:]:
            if "GROUPED_DUPLICATE" not in events[i]["status"]:
                events[i]["status"].append("GROUPED_DUPLICATE")
    return events


def _pick_final(events: list[dict], rule_status: dict[str, str]) -> dict:
    """스펙 ⑦. "이벤트 중 P가 가장 높은 것(P1>P2>P3)"이라는 문구가 P1/P2/P3만 언급하므로,
    u=U0이라 p="NONE"인 이벤트(예: D5 TYPO)는 후보에서 제외한다 - 그래야 테스트 케이스 8
    (D5 TYPO 단독 -> 이벤트는 존재하지만 기업은 NONE)이 성립한다.

    후보가 하나도 없을 때 NONE과 NOT_EVALUATED를 가르는 기준도 스펙 문구("이벤트 0개 ->
    NONE. 평가된 이벤트가 없고 NOT_EVALUATED만 있으면 -> NOT_EVALUATED")가 글자 그대로는
    "14개 룰 전부 NOT_EVALUATED여야 한다"로 읽히는데, 그러면 테스트 케이스 9(D5 UNKNOWN
    단독, financial=None이라 F1~F8은 SKIPPED지 NOT_EVALUATED가 아님 -> 기업 NOT_EVALUATED)가
    성립할 수 없다. 그래서 "판정 불가로 끝난 룰이 하나라도 있으면(SKIPPED/NO_HIT가 섞여
    있어도) NONE 대신 NOT_EVALUATED로 알린다"로 해석해 구현했다."""
    candidates = [
        ev for ev in events
        if ev["p"] in ("P1", "P2", "P3") and "GROUPED_DUPLICATE" not in ev["status"]
    ]
    if candidates:
        best = sorted(candidates, key=lambda ev: (P_ORDER[ev["p"]], -ev["m"], _rule_sort_key(ev["rule_id"])))[0]
        return {
            "p": best["p"], "m": best["m"], "u": best["u"],
            "top_event_id": best["event_id"], "reason_text": best["reason_text"],
        }

    not_evaluated_rules = sorted(rid for rid, s in rule_status.items() if s == "NOT_EVALUATED")
    if not_evaluated_rules:
        return {
            "p": "NOT_EVALUATED", "m": None, "u": None, "top_event_id": None,
            "reason_text": f"데이터 부족으로 판정 불가 ({', '.join(not_evaluated_rules)})",
        }
    return {
        "p": "NONE", "m": None, "u": None, "top_event_id": None,
        "reason_text": "마지막 검토일 이후 유의미한 변화 없음",
    }


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def evaluate(
    corp_code: str, review_date: str, data_cutoff: str,
    financial: dict | None, disclosures: list[dict],
    rule_version: str = RULE_VERSION_DEFAULT,
) -> dict:
    c, d = compute_c(), compute_d()
    rule_status: dict[str, str] = {}
    events: list[dict] = []
    f1_hit = f2_hit = False

    if financial is None or not financial.get("financial_new_since_review", False):
        for rid in F_RULE_IDS:
            rule_status[rid] = "SKIPPED_NO_NEW_FINANCIAL"
    elif not financial.get("same_comparable_periods", False):
        for rid in F_RULE_IDS:
            rule_status[rid] = "NOT_EVALUATED"
    else:
        f_events, f_status = _evaluate_financial_rules(corp_code, review_date, financial, c, d)
        events.extend(f_events)
        rule_status.update(f_status)
        f1_hit = f_status.get("F1") == "HIT"
        f2_hit = f_status.get("F2") == "HIT"

    d_events, d_status = _evaluate_disclosure_rules(disclosures, f1_hit, f2_hit, c, d)
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
