# Rule Engine v4.0 (현재 기본값)

순수 함수 `src/rules/rule_engine_v40.py::evaluate()`. `from src.rules import evaluate`로
가져오면 이게 기본으로 잡힌다. DB·API 접근 없음, LLM 없음 — 코드가 전부 판정.

v3.3-core와의 근본적 차이: **점수를 합산해서 등급을 매기지 않는다.** 기업의 재검토
등급은 "가장 중요한 단일 사건"으로 정해지고, 긴급확인 사건이 하나라도 있으면 점수와
무관하게 그게 등급을 결정한다.

```python
from src.rules import evaluate  # == rule_engine_v40.evaluate

result = evaluate(
    corp_code="00126380", review_date="2026-10-08", data_cutoff="2026-10-08",
    financial={
        "prev_op": 100_000, "curr_op": -50_000, "curr_sales": 1_000_000,
        "prev_cfo": None, "curr_cfo": None, "prev_sales": None,
        "prev_debt": None, "curr_debt": None, "prev_equity": None, "curr_equity": None,
        "prev_assets": None, "curr_assets": None, "prev_borrowings": None, "curr_borrowings": None,
        "financial_new_since_review": True, "same_comparable_periods": True,
        "financial_report_id": "FR1", "rcept_no": "20260101000001",
    },
    disclosures=[
        {"rcept_no": "D1", "rcept_dt": "20260102", "disc_type": "D3", "report_nm": "...",
         "event_group_id": None, "correction_type": None, "structured": None},
    ],
)
print(result["review_priority"])
# {"grade": "긴급확인"|"높음"|"중간"|"낮음"|"변화없음"|"판정불가", "score": 1~10|None,
#  "is_emergency": bool, "top_event_id": ..., "reason_text": ..., "additional_important_events": int}
```

`financial`/`disclosures` 키는 모듈 docstring 참고. v4.0에서 바뀐 점:
- F1(영업이익)/F2(CFO)/F3(매출)이 양방향(긍정·부정 둘 다 탐지)으로 바뀜
- F6(흑자 유지 중 증감)·F7(적자 유지 중 증감) 신설 — v3.3의 "F7(YoY+50%)"는 대체됨
- D5가 4단계(오탈자/일반조건변경/중요금액변경/핵심재무수치정정), D6이 D6A~D6D로 분할
- D7(관리종목등)·D8(거래정지등)·D9(영업정지등) 신설 — 전부 긴급확인 전용
- rule_status에 `EXCLUDED`(적용제외) 추가 — F8 발생 시 F4가 여기로 감

실행: `pytest -q` (v4.0 16개 + v3.3 14개 = 30개 전부 통과).

## 스펙에 공식이 없어 직접 정한 부분 (팀 설정값)
`src/rules/rule_engine_v40.py` 모듈 docstring 및 `THRESHOLDS` 주석에 근거와 함께 적어둠:
1. 1~10 점수 공식(`base_score + intensity_bonus`)과 룰별 base_score 배정값
2. D2/D6A~D6D의 밴드 숫자(스펙에 명시 없음, D1과 같은 패턴으로 통일)
3. D7/D8/D9의 룰 ID(스펙엔 조건만 있고 번호가 없어 새로 매김)
4. F6/F7의 2·3차 밴드(50%/80% — 스펙은 1차 임계값 30%만 명시)

## 이전 버전
`src/rules/rule_engine_v33.py` (v3.3-core) — `from src.rules import evaluate_v33`로 계속
접근 가능. P1/P2/P3/NONE/NOT_EVALUATED 등급 체계, A+B+C+D 점수 합산 모델. 테스트는
`tests/test_rule_engine_v33.py`.

## SQL 엔진(실제 웹앱)에도 반영 완료
실제 웹앱이 쓰는 `scripts/run_rules.py` + `sql/006_rule_catalog.sql`(+ `sql/004_mart.sql`,
`scripts/webapp.py`, `scripts/webapp_static/index.html`, `scripts/export_powerbi.py`)도
같은 v4.0 로직(점수 합산 없이 대표사건 1개 선출)으로 전면 교체했다. 두 엔진의 base_score/
밴드 숫자는 `rule_engine_v40.py`의 THRESHOLDS와 맞춰 일치시켰다.

SQL 엔진은 아래 지점에서 `src/rules`보다 단순화돼 있다(해당 함수/스키마 주석에도
명시):
- "판정불가"(NOT_EVALUATED)/"적용제외"(EXCLUDED) 상태값 테이블 없음 — F8 발생 시 F4는
  이벤트를 안 만드는 것으로 암묵 처리.
- event_group_id 기반 중복이벤트 묶기 미반영(disclosures 적재 단계에 연결 컬럼 없음).
- D1/D2/D6A~D6D의 강도 밴드는 구조화된 금액 데이터가 적재돼 있지 않아 바이너리(밴드
  없음) — 기존 엔진과 동일 수준.
- D5 CORE_FINANCIAL의 "손익/자기자본 상태 변경" 긴급확인 조건은 키워드 근사치
  (HIGH_KEYWORDS + CORE_REPORT_KEYWORDS 동시매칭)로 대체.
