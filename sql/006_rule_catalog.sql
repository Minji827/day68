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

-- base_score는 1~10 "같은 등급 안에서의 순서 정렬용 보조 척도", is_emergency_rule은
-- "등장 자체가 항상 긴급확인"인 룰(F8/D3/D7/D8)만 true. F1+F2 동시발생·D5 핵심재무
-- 정정의 조건부 긴급확인은 카탈로그 플래그로 표현이 안 돼서 run_rules.py 집계 단계가
-- 직접 처리한다.
--
-- v3.3 합산 모델 전용이던 weight/direction 컬럼과 rule_type='combo'를 떼어내는
-- 일회성 변형은 sql/migrations/001_v40_v41_reshape.sql에 있다.
CREATE TABLE IF NOT EXISTS core.rule_catalog (
    rule_id             text PRIMARY KEY,
    rule_type           text NOT NULL CHECK (rule_type IN ('financial', 'disclosure', 'context')),
    description         text NOT NULL,
    base_score          integer,
    domain              text,
    is_emergency_rule   boolean NOT NULL DEFAULT false,
    evidence_refs       text,
    is_team_assumption  boolean NOT NULL DEFAULT false,
    notes               text
);
