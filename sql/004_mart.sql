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
    rule_id          text NOT NULL,            -- core.rule_catalog.rule_id (F1-F5 / D1-D5)
    weight           integer NOT NULL,         -- core.rule_catalog.weight 스냅샷 (3=high,2=mid,1=가산)
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

-- 기업별 재검토 우선순위 요약 (대시보드 상단 랭킹용)
CREATE TABLE IF NOT EXISTS mart.company_priority (
    corp_code               text NOT NULL,
    corp_name                text NOT NULL,
    review_date              date NOT NULL,
    priority_level           text NOT NULL CHECK (priority_level IN ('high', 'mid', 'low', 'none')),
    priority_score           numeric NOT NULL DEFAULT 0,
    change_count             integer NOT NULL DEFAULT 0,
    financial_rule_count     integer NOT NULL DEFAULT 0,  -- F1-F5 매칭 수
    disclosure_rule_count    integer NOT NULL DEFAULT 0,  -- D1-D5 매칭 수 (정정공시 D5 포함)
    PRIMARY KEY (corp_code, review_date)
);
CREATE INDEX IF NOT EXISTS ix_mart_company_priority_review ON mart.company_priority (review_date, priority_level);

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
