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
    CHECK (change_type IN ('financial', 'disclosure', 'context'));
ALTER TABLE mart.change_events DROP COLUMN IF EXISTS weight;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS grade text;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS score integer;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS is_emergency boolean NOT NULL DEFAULT false;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS direction text
    CHECK (direction IN ('positive', 'negative', 'neutral'));
-- v4.1 3단계: CTX1(ECOS 거시 맥락)은 점수·등급에 반영 안 되는 "참고" 전용 이벤트라
-- 긴급확인/높음/중간/낮음 어디에도 안 들어가는 별도 grade('참고')가 필요.
ALTER TABLE mart.change_events DROP CONSTRAINT IF EXISTS change_events_grade_check;
ALTER TABLE mart.change_events ADD CONSTRAINT change_events_grade_check
    CHECK (grade IN ('긴급확인', '높음', '중간', '낮음', '참고'));

-- v4.1: 어느 룰 버전으로 계산된 행인지 추적(점수표가 또 바뀔 때 과거 행과 구분 가능하게).
-- run_rules.py가 매번 새로 INSERT하므로 DEFAULT만으로 모든 신규 행에 자동 기록됨 -
-- 버전이 바뀌면 이 DEFAULT 값도 같이 올릴 것.
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS rule_version text NOT NULL DEFAULT '4.1';

-- v4.1 2단계: 재무(F-rule)·공시(D-rule)가 전부 "이전->최신"으로만 보여서 YoY/정정전후/
-- 신규이벤트가 서로 다른 비교 기준이라는 게 화면에서 안 드러난다는 팀 리뷰 반영.
-- compare_basis: F1~F8·D1(재무 데이터 기반)=YOY, D2~D4/D6A~D10=NEW_EVENT, D5=CORRECTION.
-- event_group_id: 같은 사건을 서로 다른 룰이 동시에 잡은 경우 묶는 키(현재는 D5만 자기
-- rcept_no를 씀 - F-rule과 D5를 연결할 재무제표<->공시 매핑이 DB에 없어 실질적으로는
-- 그룹이 거의 안 생김, 나중에 그 연결이 생기면 바로 쓸 수 있게 인프라만 둠).
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS compare_basis text
    CHECK (compare_basis IN ('YOY', 'CORRECTION', 'NEW_EVENT', 'MACRO'));
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS basis_label text;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS event_group_id text;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS is_grouped_duplicate boolean NOT NULL DEFAULT false;

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
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS rule_version text NOT NULL DEFAULT '4.1';

-- v4.1 2단계: 대표 사건(reason_text) 외에 "그다음으로 중요한 사건"도 보여주고, YoY/정정/
-- 신규 이벤트가 각각 몇 건인지 구분해서 보여주기 위한 컬럼. 집계 로직은
-- run_rules.aggregate_company_priority 참고.
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS reason_text_2 text;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS yoy_event_count integer NOT NULL DEFAULT 0;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS correction_event_count integer NOT NULL DEFAULT 0;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS new_event_count integer NOT NULL DEFAULT 0;

-- v4.1 3단계: 이 기업이 "금리 변경 + 차입 의존" 노출 대상인지, 그 사유 설명.
-- run_rules.aggregate_company_priority가 mart.rate_context를 조인해서 채운다.
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS rate_exposure boolean NOT NULL DEFAULT false;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS rate_context_text text;

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

-- v4.1 5단계: 변화(①원문에서 추출, LLM+근거검증) / 사유(②rule_catalog.description 그대로,
-- 결정론적) / 확인(③core.rule_checklist에서만, 결정론적)을 한 문장으로 합치지 않고
-- 구분해서 저장 - 화면에서 세 구역으로 나눠 보여주기 위함(팀 리뷰). ②③은 evidence_*를
-- "원문 발췌"가 아니라 그 출처(rule_id/체크리스트 항목 자체)로 채운다 - 체크리스트/
-- 카탈로그 존재 자체가 근거이기 때문.
ALTER TABLE mart.explanation_sentences ADD COLUMN IF NOT EXISTS sentence_type text
    NOT NULL DEFAULT '변화' CHECK (sentence_type IN ('변화', '사유', '확인'));

-- KPI 타일 (FR-04: 변화 발생 기업 수 / 중요도 높은 기업 수 / 신규·정정 공시 수)
CREATE TABLE IF NOT EXISTS mart.kpi_daily (
    review_date             date PRIMARY KEY,
    companies_changed       integer NOT NULL DEFAULT 0,
    companies_high_priority integer NOT NULL DEFAULT 0,
    new_disclosures         integer NOT NULL DEFAULT 0,
    corrections              integer NOT NULL DEFAULT 0
);
ALTER TABLE mart.kpi_daily ADD COLUMN IF NOT EXISTS rate_changed boolean NOT NULL DEFAULT false;
ALTER TABLE mart.kpi_daily ADD COLUMN IF NOT EXISTS rate_exposed_companies integer NOT NULL DEFAULT 0;

-- ECOS 기준금리 맥락 (차입금 증가 기업의 이자 부담 확인용 보조 맥락)
--
-- v4.1 3단계 전면 개정: 기존엔 "검토일당 1행"으로 기준금리 숫자만 보여줬는데, 어느
-- 기업이랑 관련 있는지 연결이 없어 "그래서 뭐?"가 없다는 팀 리뷰 반영 — "검토일+기업"
-- 단위로 바꿔서, 금리가 변경됐고 그 기업이 차입 의존도가 높거나 최근 차입 관련 공시가
-- 있으면 그 기업에 직접 노출시킨다. **기존 PK(review_date 단독)를 바꾸는 breaking
-- change — n8n/PowerBI 등 이 테이블을 직접 조회하는 다른 팀원에게 공지 필요(사용자 확인).**
-- 금리가 안 바뀐 회차는 기업별로 행을 만들 대상이 없으므로, corp_code='__ALL__'
-- (sentinel - 특정 기업 아님) 한 행만 "변경 없음" 마커로 남긴다.
ALTER TABLE mart.rate_context DROP CONSTRAINT IF EXISTS rate_context_pkey;
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS corp_code text NOT NULL DEFAULT '__ALL__';
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS rate_at_review numeric;
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS rate_now numeric;
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS exposure_reason text;
-- 구 컬럼명(base_rate/prior_base_rate)은 더 이상 안 씀 - rate_now/rate_at_review로 대체.
ALTER TABLE mart.rate_context DROP COLUMN IF EXISTS base_rate;
ALTER TABLE mart.rate_context DROP COLUMN IF EXISTS prior_base_rate;
ALTER TABLE mart.rate_context ADD PRIMARY KEY (review_date, corp_code);
