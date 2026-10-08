-- MART: 기업 × 검토 기준일 단위로 변화 이벤트·중요도 집계 (PRD 9번).
-- Power BI는 이 스키마를 PostgreSQL 커넥터로 직접 조회, n8n도 동일 테이블을 쿼리해서
-- 주간 이메일을 발송한다 (별도 API 서버 불필요).

-- 변화이벤트(기업코드·검토기준일·변화유형·규칙ID)
CREATE TABLE IF NOT EXISTS mart.change_events (
    event_id         bigserial PRIMARY KEY,
    corp_code        text NOT NULL,
    corp_name        text NOT NULL,
    review_date      date NOT NULL,          -- 사용자가 선택한 "마지막 검토일"
    change_type      text NOT NULL CHECK (change_type IN ('financial', 'disclosure')),
    rule_id          text NOT NULL,            -- core.rule_catalog.rule_id
    rcept_no         text,
    account_std_name text,
    old_value        numeric,
    new_value        numeric,
    change_rate      numeric,
    description      text,
    detected_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mart_change_events_review ON mart.change_events (review_date, corp_code);
CREATE INDEX IF NOT EXISTS ix_mart_change_events_rule ON mart.change_events (review_date, rule_id);

-- v4.0 전면 개정: "점수를 합산해서 등급을 매기지 않는다 — 가장 중요한 단일 사건으로
-- 기업 등급을 정한다"는 팀 스펙에 맞춰, 합산 모델 전용이던 weight 컬럼과 change_type=
-- 'combo'(룰 조합 가산점) 개념을 제거했다. 대신 이벤트 하나하나가 자기 grade/score/
-- is_emergency/direction을 직접 들고 있다(양방향 룰이라 같은 rule_id도 이벤트마다
-- direction이 다를 수 있어 더 이상 rule_catalog에서 join 못 함).
ALTER TABLE mart.change_events DROP CONSTRAINT IF EXISTS change_events_change_type_check;
ALTER TABLE mart.change_events ADD CONSTRAINT change_events_change_type_check
    CHECK (change_type IN ('financial', 'disclosure'));
ALTER TABLE mart.change_events DROP COLUMN IF EXISTS weight;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS grade text;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS score integer;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS is_emergency boolean NOT NULL DEFAULT false;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS direction text
    CHECK (direction IN ('positive', 'negative', 'neutral'));
ALTER TABLE mart.change_events DROP CONSTRAINT IF EXISTS change_events_grade_check;
ALTER TABLE mart.change_events ADD CONSTRAINT change_events_grade_check
    CHECK (grade IN ('긴급확인', '높음', '중간', '낮음'));

-- 기업별 재검토 우선순위 요약 (대시보드 상단 랭킹용)
CREATE TABLE IF NOT EXISTS mart.company_priority (
    corp_code               text NOT NULL,
    corp_name                text NOT NULL,
    review_date              date NOT NULL,
    priority_level           text NOT NULL CHECK (priority_level IN ('high', 'mid', 'low', 'none')),
    priority_score           numeric NOT NULL DEFAULT 0,
    change_count             integer NOT NULL DEFAULT 0,
    financial_rule_count     integer NOT NULL DEFAULT 0,  -- F1-F8 매칭 수
    disclosure_rule_count    integer NOT NULL DEFAULT 0,  -- D1-D9 매칭 수 (정정공시 D5 포함)
    PRIMARY KEY (corp_code, review_date)
);
CREATE INDEX IF NOT EXISTS ix_mart_company_priority_review ON mart.company_priority (review_date, priority_level);

-- v4.0 전면 개정: risk_score/positive_score/raw_risk_score/forced_high/forced_high_reason
-- (합산 모델 전용) 제거. 대표 사건 하나를 선출해 그 사건의 grade/score/is_emergency를
-- 그대로 기업 등급으로 쓴다 — priority_level은 'emergency'/'high'/'mid'/'low'/'none'
-- 5종(판정불가·적용제외는 SQL 엔진엔 미반영 — README_rule_engine.md 참고).
ALTER TABLE mart.company_priority DROP COLUMN IF EXISTS risk_score;
ALTER TABLE mart.company_priority DROP COLUMN IF EXISTS positive_score;
ALTER TABLE mart.company_priority DROP COLUMN IF EXISTS raw_risk_score;
ALTER TABLE mart.company_priority DROP COLUMN IF EXISTS forced_high;
ALTER TABLE mart.company_priority DROP COLUMN IF EXISTS forced_high_reason;
ALTER TABLE mart.company_priority DROP CONSTRAINT IF EXISTS company_priority_priority_level_check;
ALTER TABLE mart.company_priority ADD CONSTRAINT company_priority_priority_level_check
    CHECK (priority_level IN ('emergency', 'high', 'mid', 'low', 'none'));
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS is_emergency boolean NOT NULL DEFAULT false;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS top_rule_id text;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS top_event_id bigint;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS additional_important_events integer NOT NULL DEFAULT 0;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS reason_text text;

-- 해설문장(접수번호·문장번호), 근거 원문 섹션 필수
CREATE TABLE IF NOT EXISTS mart.explanation_sentences (
    rcept_no          text NOT NULL,
    sentence_no        integer NOT NULL,
    sentence_text       text NOT NULL,
    evidence_rcept_no   text NOT NULL,
    evidence_section    text NOT NULL,
    evidence_excerpt    text NOT NULL,
    PRIMARY KEY (rcept_no, sentence_no)
);

-- KPI 타일 (FR-04: 변화 발생 기업 수 / 중요도 높은 기업 수 / 신규·정정 공시 수)
CREATE TABLE IF NOT EXISTS mart.kpi_daily (
    review_date             date PRIMARY KEY,
    companies_changed       integer NOT NULL DEFAULT 0,
    companies_high_priority integer NOT NULL DEFAULT 0,
    new_disclosures         integer NOT NULL DEFAULT 0,
    corrections              integer NOT NULL DEFAULT 0
);

-- ECOS 기준금리 맥락 (차입금 증가 기업의 이자 부담 확인용 보조 맥락)
CREATE TABLE IF NOT EXISTS mart.rate_context (
    review_date       date PRIMARY KEY,
    base_rate         numeric NOT NULL,
    prior_base_rate   numeric,
    rate_direction    text CHECK (rate_direction IN ('up', 'down', 'flat'))
);
