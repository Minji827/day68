-- 규칙표: rule_id, 조건 설명, 기본점수, 근거번호. PRD "근거 규칙"과 그대로 연결.
--
-- v4.0 전면 개정: "점수를 합산해서 등급을 매기지 않는다 — 가장 중요한 단일 사건으로
-- 기업 등급을 정한다"는 팀 스펙(v4.0)에 맞춰, 기존 risk_score/positive_score 가중치
-- 합산 모델(weight/direction 컬럼, COMBO_* 가산점 row)을 전면 교체했다. 룰별
-- base_score(1~10, "같은 등급 안에서의 순서 정렬용 보조 척도")와 domain(분류 메타데이터)
-- 로 바뀌었고, 긴급확인 여부는 is_emergency_rule(등장 자체가 항상 긴급확인인 룰만
-- true — F8/D3/D7/D8)로 표시한다. F1+F2 동시발생·D5 핵심재무정정의 조건부 긴급확인은
-- 카탈로그 플래그로 표현 못 해서 run_rules.py의 집계 단계에서 직접 처리한다.
--
-- base_score/domain/밴드 숫자는 src/rules/rule_engine_v40.py의 THRESHOLDS/RULE_DOMAIN_V40
-- 값과 그대로 맞췄다 (pure-function 엔진과 SQL 엔진 간 숫자 불일치 방지). 근거 문헌이
-- 뒷받침하는 값이 아니라 팀 설정값(is_team_assumption=true)인 것도 동일하게 유지.
--
-- v4.1 개정: 실제 기업 데이터를 돌려보니 등급이 거의 다 "중간"에 몰려 변별력이 낮다는
-- 팀 리뷰 피드백 반영 — F1/F2/F4/F5/D2/D6D/D9/D5(MAJOR_AMOUNT)의 base_score를 상향하고,
-- D10(채무보증·담보제공)을 신설했다. 등급 경계(8/4)는 그대로 — 룰별 기본점수만 조정.

CREATE TABLE IF NOT EXISTS core.rule_catalog (
    rule_id             text PRIMARY KEY,
    rule_type           text NOT NULL CHECK (rule_type IN ('financial', 'disclosure', 'context')),
    description         text NOT NULL,
    evidence_refs       text,
    is_team_assumption  boolean NOT NULL DEFAULT false,
    notes               text
);

-- v4.0: 합산 모델 전용이었던 direction/weight는 제거하고 base_score/domain/
-- is_emergency_rule로 교체. rule_type은 'combo'를 더 이상 안 써서 원래 3종으로 되돌림
-- (기존 COMBO_* row는 새 CHECK를 걸기 전에 먼저 지워야 제약 위반이 안 난다).
DELETE FROM core.rule_catalog WHERE rule_type = 'combo' OR rule_id LIKE 'COMBO_%';
-- v3.3 전용 rule_id(D6 - v4.0에서 D6A~D6D로 분할됨)도 같은 이유로 정리.
DELETE FROM core.rule_catalog WHERE rule_id = 'D6';
ALTER TABLE core.rule_catalog ADD COLUMN IF NOT EXISTS base_score integer;
ALTER TABLE core.rule_catalog ADD COLUMN IF NOT EXISTS domain text;
ALTER TABLE core.rule_catalog ADD COLUMN IF NOT EXISTS is_emergency_rule boolean NOT NULL DEFAULT false;
ALTER TABLE core.rule_catalog DROP COLUMN IF EXISTS weight;
ALTER TABLE core.rule_catalog DROP COLUMN IF EXISTS direction;
ALTER TABLE core.rule_catalog DROP CONSTRAINT IF EXISTS rule_catalog_rule_type_check;
ALTER TABLE core.rule_catalog ADD CONSTRAINT rule_catalog_rule_type_check
    CHECK (rule_type IN ('financial', 'disclosure', 'context'));

INSERT INTO core.rule_catalog (rule_id, rule_type, description, base_score, domain, is_emergency_rule, evidence_refs, is_team_assumption, notes) VALUES
    ('F1', 'financial', '영업이익 부호 전환(흑자<->적자, 양방향). 매출 대비 전환 규모가
        크면(5/10/20%) 가산점. F1+F2 동시발생 시 둘 다 긴급확인으로 승격(집계 단계에서
        처리).', 7, '손익', false, '(1)(2)',  true,
        'v3.3까지의 "흑자->적자만" 단방향에서 양방향으로 확장(기존 F6 적자->흑자 전환을
         흡수). base_score/밴드는 src/rules/rule_engine_v40.py THRESHOLDS["F1"]과 동일.
         v4.1: 6->7 상향 — 부호 전환은 거래소 손익구조 변경 공시 기준(30%)보다 큰 사건이라
         전환폭 밴드1(매출 대비 5%)만 붙어도 "높음"으로 올라가야 한다는 팀 리뷰 반영.'),
    ('F2', 'financial', '영업활동현금흐름 부호 전환(양방향). F1과 동일한 매출 대비 전환강도
        가산점.', 7, '현금', false, '(2)(3)(4)', true,
        'F1과 동일 로직, 양방향으로 확장. v4.1: F1과 동일 사유로 6->7 상향.'),
    ('F3', 'financial', '매출액 YoY ±10% 이상 변화(양방향 — 증가도 긍정 후보로 탐지)', 3, '손익', false, NULL, true,
        '방향·기준값 모두 직접 근거 없음(팀 설정값). v3.3의 "감소만"에서 양방향으로 확장.'),
    ('F4', 'financial', '부채비율(부채총계/자본총계) +20%p 이상. 자기자본이 음수로 전환된
        경우(F8 발생)는 비교 자체가 부적절해 적용제외.', 4, '레버리지', false, '(1)(2)', true,
        '레버리지 변화를 신호로 쓰는 방향은 근거 있음. 기준값은 팀 설정.
         v4.1: 3->4 상향 — +20%p 상승이 "낮음"으로 분류되는 건 과소평가라는 팀 리뷰 반영.'),
    ('F5', 'financial', '총차입금 +20% 이상', 5, '레버리지', false, '(2)(6)', true,
        '방향은 근거 있음. 기준값은 팀 설정.
         v4.1: 4->5 상향 — 차입 100% 증가 시 7점까지 가도록, 자금조달 압박 신호로서의
         비중을 높였다(팀 리뷰).'),
    ('F6', 'financial', '흑자 유지 중(양수->양수) 영업이익 ±30% 이상 증감', 3, '손익', false, NULL, true,
        '신설 — v3.3의 "F6=적자->흑자 전환"은 새 F1(양방향)에 흡수됨. "흑자 유지"
         조건이 명시적이라 F1과 안 겹침.'),
    ('F7', 'financial', '적자 유지 중(음수->음수) 영업손실 ±30% 이상 증감(확대/축소)', 3, '손익', false, NULL, true,
        '신설 — v3.3의 "F7=영업이익 YoY+50%"는 폐기(F6의 대칭 룰로 대체). "적자 유지"
         조건이 명시적이라 F1과 안 겹침.'),
    ('F8', 'financial', '자기자본 상태 전환(양수 -> 0 이하)', 10, '자본', true, NULL, true,
        '신설(v3.3엔 없던 룰). 긴급확인 7개 조건 중 하나 — 등장 자체로 긴급확인.
         F4(부채비율)는 F8 발생 시 적용제외.'),
    ('D1', 'disclosure', '단기차입금 직전 대비 증가', 4, '레버리지', false, '(6)', false,
        '단기차입 비중이 2008년 금융위기 이후 최고라는 보도 기반.'),
    ('D2', 'disclosure', '자금조달 결정 공시(유상증자·전환사채·신주인수권부사채 발행결정)', 6, '자본', false, '(5)(7)', false,
        '현금흐름 약한 기업의 유상증자에 시장이 부정적 반응, 공시일 18% 하락 사례.
         v4.1: 5->6 상향(벼랑 끝 자금조달 공시 유형 — 투자성 공시 D6A~C보다 상위로),
         유상증자 외 전환사채·신주인수권부사채 발행결정도 같은 자금조달 압박 신호로 보고
         조건 확대(팀 리뷰).'),
    ('D3', 'disclosure', '감사의견 비적정(의견거절·부적정·중요한정) 공시', 10, '감사', true, '(8)', false,
        '공시 제목만으로는 의견 유형이 거의 드러나지 않음 — 제목 키워드 매칭은
         best-effort이며 놓칠 수 있음(TODO: 공시원문 파싱). 긴급확인 7개 조건 중 하나.'),
    ('D4', 'disclosure', '최대주주 변경 공시', 5, '지배구조', false, '(9)', false,
        '공시 1건당 이벤트 1개 — 등급 산정은 "가장 중요한 단일 사건" 방식이라 같은 기간
         여러 건이어도 더 이상 합산되지 않음(v3.3과의 차이).'),
    ('D5', 'disclosure', '정정공시 — 고정점수 아님. 정정 내용을 섹션 원문 키워드로
        TYPO(1)/MINOR_CHANGE(3)/MAJOR_AMOUNT(7)/CORE_FINANCIAL(9, 손익·자기자본 상태
        변경 동반 시 긴급확인) 4단계로 분류해 run_rules.classify_d5_severity가 계산한다.
        여기 base_score(1)는 섹션을 못 찾았을 때만 쓰는 fallback(TYPO와 동일 취급).', 1, '정정', false, '(10)', false,
        '금감원이 재무제표 정정을 "투자판단에 영향을 줄 수 있는 중요정보"라고 설명.
         v3.3의 1~7점 단일 스케일에서 4단계 유형 분류로 개편(v4.0 스펙).
         v4.1: MAJOR_AMOUNT 6->7 상향(핵심 보고서 정정은 "중간" 상위로). 이 row의
         fallback base_score는 실제로는 안 쓰이는 값이라(classify_d5가 섹션 없으면
         TYPO(1) 반환) 3->1로 정정해 실제 동작과 일치시킴(팀 리뷰 — 과거 "3"은 혼동 소지).'),
    ('D6A', 'disclosure', '신규 공급계약·수주 공시', 4, '사업', false, NULL, true,
        'v3.3의 단일 D6을 4분할. 제목 키워드 매칭(팀 설정값).'),
    ('D6B', 'disclosure', '신규 시설투자 공시', 4, '사업', false, NULL, true, '위와 동일.'),
    ('D6C', 'disclosure', '타법인 출자·인수 공시', 4, '사업', false, NULL, true, '위와 동일.'),
    ('D6D', 'disclosure', '중요 계약 해지 공시', 6, '사업', false, NULL, true,
        '신설(v3.3엔 없던 유형) — D6A~C와 달리 부정적 신호.
         v4.1: 5->6 상향(부정 사업 변화를 긍정 D6A~C(4점)보다 상위로, 팀 리뷰).'),
    ('D7', 'disclosure', '관리종목 지정·상장적격성 실질심사·상장폐지 관련 공시', 10, '상장', true, NULL, true,
        '신설 — 긴급확인 7개 조건 중 하나. 스펙엔 조건만 있고 룰 ID가 없어 새로 번호를
         매김(팀 설정값).'),
    ('D8', 'disclosure', '매매거래정지·회생절차개시신청·부도 관련 공시', 10, '상장', true, NULL, true,
        '신설 — 긴급확인 7개 조건 중 하나. D7과 같은 사유로 번호를 새로 매김.'),
    ('D9', 'disclosure', '중대한 영업정지·핵심사업중단 공시', 8, '사업', false, NULL, true,
        '신설. 긴급확인 고정은 아님 — src/rules/rule_engine_v40.py의 실제 구현과 동일하게
         맞춤(등장 자체가 자동 긴급확인은 아니라는 판단, 팀 설정값).
         v4.1: 7->8 상향(핵심사업 중단은 단독으로도 "높음"이어야 한다는 팀 리뷰).'),
    ('D10', 'disclosure', '채무보증·담보제공 결정 공시', 6, '레버리지', false, NULL, true,
        '신설(v4.1). D1/D2와 같은 자금조달 압박 신호 유형 — 거래소 의무공시 항목. 구조화된
         보증금액/자기자본 비율 데이터가 없어 밴드 없는 단일 점수(팀 설정값).'),
    ('CTX1', 'context', '마지막 검토 이후 기준금리 변경 시 차입 의존도 높은 기업을 금리 영향
        재확인 대상으로 표시(점수·등급 미반영)', 0, NULL, false, '(6)(11)', false,
        '차입비용 상승이 이자보상배율을 악화시킨다는 한국은행 관계자 설명.
         v4.1: 검토일당 1행(숫자만 표시)에서 검토일+기업 단위로 개편 — 차입 의존도가
         높거나 최근 차입 관련 공시(F5/D1/D10)가 있는 기업에 직접 노출시켜 "그래서
         어떤 기업을 봐야 하는지"가 드러나게 함(팀 리뷰). 여전히 대표사건 선출에서는
         제외(run_rules.aggregate_company_priority), 점수·등급에는 영향 없음.')
ON CONFLICT (rule_id) DO UPDATE SET
    rule_type = EXCLUDED.rule_type,
    description = EXCLUDED.description,
    base_score = EXCLUDED.base_score,
    domain = EXCLUDED.domain,
    is_emergency_rule = EXCLUDED.is_emergency_rule,
    evidence_refs = EXCLUDED.evidence_refs,
    is_team_assumption = EXCLUDED.is_team_assumption,
    notes = EXCLUDED.notes;

ALTER TABLE core.rule_catalog ALTER COLUMN base_score SET NOT NULL;
