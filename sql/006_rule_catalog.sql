-- 규칙표: rule_id, 조건 설명, 가중치, 근거번호. PRD "근거 규칙"과 그대로 연결.
-- 점수 방식: 높음=3, 중간=2, 공시 가산=1. 합산 5점 이상 높음 / 2점 이상 중간 / 그 외 낮음.
-- 이 구간·기준값은 근거 문헌이 뒷받침하는 값이 아니라 팀 설정값 (is_team_assumption=true).
-- 샘플 기업으로 돌려보고 조정할 것.
-- direction: 'negative'(악화 신호)/'positive'(개선 신호). "재검토 우선순위"는 위험기업
-- 판정이 아니라 "다시 볼 기업" 선별이므로, 급격한 개선(F6/F7/D6)도 점수에 똑같이 더한다
-- (양쪽 다 +가중치 — 음수로 상쇄하지 않음). 화면에서는 direction으로 색/아이콘만 구분.

CREATE TABLE IF NOT EXISTS core.rule_catalog (
    rule_id             text PRIMARY KEY,
    rule_type           text NOT NULL CHECK (rule_type IN ('financial', 'disclosure', 'context')),
    description         text NOT NULL,
    weight              integer NOT NULL,
    evidence_refs       text,
    is_team_assumption  boolean NOT NULL DEFAULT false,
    notes               text
);

-- 멘토 피드백 ⑤: Rule이 전부 악화 신호뿐이라 "위험기업 판정"처럼 보임 — 제품 목적은
-- "다시 볼 기업"이므로 긍정적 변화도 재검토 대상이어야 한다. direction으로 구분해서
-- 화면에서 ▲개선/▼악화를 다르게 표시할 수 있게 한다.
ALTER TABLE core.rule_catalog ADD COLUMN IF NOT EXISTS direction text NOT NULL DEFAULT 'negative'
    CHECK (direction IN ('positive', 'negative'));

INSERT INTO core.rule_catalog (rule_id, rule_type, description, weight, evidence_refs, is_team_assumption, notes, direction) VALUES
    ('F1', 'financial',  '영업이익 직전 >0 → 최신 ≤0 (흑자→적자 전환)', 3, '(1)(2)', false,
        'Ohlson/Piotroski 변수는 순이익 기준. 영업이익 사용은 팀 선택.', 'negative'),
    ('F2', 'financial',  '영업활동현금흐름 직전 >0 → 최신 <0', 3, '(2)(3)(4)', false, NULL, 'negative'),
    ('F3', 'financial',  '매출 YoY ≤ -10%', 2, NULL, true,
        '방향·기준값 모두 직접 근거 없음. 팀 설정값 — 보조 지표로 낮추거나 MVP 제외도 고려.', 'negative'),
    ('F4', 'financial',  '부채비율(부채총계/자본총계) +20%p 이상', 2, '(1)(2)', true,
        '레버리지 변화를 신호로 쓰는 방향은 근거 있음. 20%p 기준값은 팀 설정.', 'negative'),
    ('F5', 'financial',  '차입금(장기+단기 합산) +20% 이상', 2, '(2)(6)', true,
        '방향은 근거 있음. 20% 기준값은 팀 설정. 단기차입 비중 상승은 기사로 확인.', 'negative'),
    ('F6', 'financial',  '영업이익 직전 ≤0 → 최신 >0 (적자→흑자 전환)', 3, NULL, true,
        'F1의 반대 방향. 실적 급개선도 재검토 대상이라는 멘토 피드백 반영 — 전환 자체를
         신호로 쓰는 것은 F1/F2와 같은 논리 구조이나, "개선"이라는 방향에 대한 별도
         문헌 근거는 없음 (팀 설정값).', 'positive'),
    ('F7', 'financial',  '영업이익 YoY +50% 이상 급증', 2, NULL, true,
        'F3(매출 급감)과 대칭 구조. 기준값(50%)은 팀 설정값, 샘플 기업으로 조정 필요.', 'positive'),
    ('D1', 'disclosure', '단기차입금 직전 대비 증가', 1, '(6)', false,
        '단기차입 비중이 2008년 금융위기 이후 최고라는 보도 기반.', 'negative'),
    ('D2', 'disclosure', '유상증자 결정 공시', 1, '(5)(7)', false,
        '현금흐름 약한 기업의 유상증자에 시장이 부정적 반응, 공시일 18% 하락 사례.', 'negative'),
    ('D3', 'disclosure', '감사의견 비적정 (부적정·의견거절 등)', 1, '(8)', false,
        '공시 제목만으로는 의견 유형이 거의 드러나지 않음 — 현재 제목 키워드 매칭은 best-effort이며 놓칠 수 있음. 정확히 하려면 공시원문 파싱 필요(TODO).', 'negative'),
    ('D4', 'disclosure', '최대주주 변경 (같은 회계기간 2회 이상이면 강화)', 1, '(9)', false,
        '공시 1건당 +1로 적재되므로 같은 기간 2건 이상이면 합산 점수가 자연히 2점 이상으로 누적됨(강화).', 'negative'),
    ('D5', 'disclosure', '정정공시', 1, '(10)', false,
        '금감원이 재무제표 정정을 "투자판단에 영향을 줄 수 있는 중요정보"라고 설명. 재무제표
         유형 정정이 같은 리뷰에서 F-rule과 겹치면 이중계산 방지 로직(dedupe_financial_corrections)이
         weight를 0으로 내림 — 멘토 피드백 ①.', 'negative'),
    ('D6', 'disclosure', '대규모 투자·조달·수주 공시', 1, NULL, true,
        '시설투자/타법인 출자/수주처럼 규모가 큰 적극적 의사결정도 "다시 볼" 이유가
         된다는 멘토 피드백 반영. 제목 키워드 매칭 — 방향·기준 모두 팀 설정값.', 'positive'),
    ('CTX1', 'context', '기준금리 상승 + 차입금 증가 (가산 없음, 맥락 표시 전용)', 0, '(6)(11)', false,
        '차입비용 상승이 이자보상배율을 악화시킨다는 한국은행 관계자 설명. mart.rate_context에 표시만, 점수에는 반영 안 함.', 'negative')
ON CONFLICT (rule_id) DO UPDATE SET
    description = EXCLUDED.description,
    weight = EXCLUDED.weight,
    evidence_refs = EXCLUDED.evidence_refs,
    is_team_assumption = EXCLUDED.is_team_assumption,
    notes = EXCLUDED.notes,
    direction = EXCLUDED.direction;
