-- v4.1 5단계: AI 해설 3단 구조(변화/사유/확인) 중 "③추가 확인사항"을 LLM이 즉석에서
-- 지어내지 않고, 룰별로 미리 정해둔 체크리스트에서만 고르게 한다 — "점수·등급·판정은
-- 코드가 계산하고 LLM은 계산하지 않는다"는 원칙을 AI 해설에도 동일하게 적용(팀 리뷰).
-- scripts/explain.py가 읽는다. 전 룰(F1~F8, D1~D10, CTX1) 2~3개씩.

CREATE TABLE IF NOT EXISTS core.rule_checklist (
    rule_id      text NOT NULL,
    seq          integer NOT NULL,
    check_item   text NOT NULL,
    PRIMARY KEY (rule_id, seq)
);
