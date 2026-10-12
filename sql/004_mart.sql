-- MART: 기업 × 검토 기준일 단위로 변화 이벤트·중요도 집계 (PRD 9번).
-- Power BI는 이 스키마를 PostgreSQL 커넥터로 직접 조회, n8n도 동일 테이블을 쿼리해서
-- 주간 이메일을 발송한다 (별도 API 서버 불필요).
--
-- 이 파일은 "현재 스키마" 하나만 기술한다 - 예전에는 CREATE TABLE 밑으로 v4.0/v4.1
-- 개정 ALTER가 44줄 쌓여 있어서 (a) 현재 형태를 파일만 읽어서는 알 수 없었고
-- (b) CREATE와 ALTER가 따로 놀다 mart.rate_context의 CREATE만 사라지는 사고가 났다.
-- 기존 DB를 올리는 일회성 변형은 sql/migrations/001_v40_v41_reshape.sql로 옮겼다.

-- 변화이벤트(기업코드·검토기준일·변화유형·규칙ID)
--
-- v4.0 설계 원칙: "점수를 합산해서 등급을 매기지 않는다 - 가장 중요한 단일 사건으로
-- 기업 등급을 정한다." 그래서 이벤트 하나하나가 자기 grade/score/is_emergency/
-- direction을 직접 들고 있다(양방향 룰이라 같은 rule_id도 이벤트마다 direction이
-- 달라서 rule_catalog에서 join으로 끌어올 수 없다).
--
-- compare_basis: F1~F8·D1(재무 데이터 기반)=YOY, D2~D4/D6A~D10=NEW_EVENT,
--   D5=CORRECTION, CTX1=MACRO. "이전->최신"이라는 같은 표현이 실제로는 서로 다른
--   시간축을 가리킨다는 게 화면에서 안 드러난다는 팀 리뷰 반영(v4.1 2단계).
-- event_group_id: 같은 사건을 서로 다른 룰이 동시에 잡은 경우 묶는 키. 현재는 D5만
--   자기 rcept_no를 쓴다 - 재무제표가 어느 공시에서 나왔는지 연결하는 컬럼이 DB에
--   없어 F-rule과 D5를 못 묶는다. 그 연결이 생기면 바로 쓸 수 있게 인프라만 둔 상태.
-- rule_version: 점수표가 또 바뀔 때 과거 행과 구분하기 위한 추적용. run_rules.py가
--   매번 새로 INSERT하므로 DEFAULT만으로 신규 행에 자동 기록된다 - 버전이 바뀌면
--   이 DEFAULT도 같이 올릴 것.
CREATE TABLE IF NOT EXISTS mart.change_events (
    event_id             bigserial PRIMARY KEY,
    corp_code            text NOT NULL,
    corp_name            text NOT NULL,
    review_date          date NOT NULL,          -- 사용자가 선택한 "마지막 검토일"
    change_type          text NOT NULL CHECK (change_type IN ('financial', 'disclosure', 'context')),
    rule_id              text NOT NULL,          -- core.rule_catalog.rule_id
    grade                text CHECK (grade IN ('긴급확인', '높음', '중간', '낮음', '참고')),
    score                integer,
    is_emergency         boolean NOT NULL DEFAULT false,
    direction            text CHECK (direction IN ('positive', 'negative', 'neutral')),
    rcept_no             text,
    account_std_name     text,
    old_value            numeric,
    new_value            numeric,
    change_rate          numeric,
    description          text,
    compare_basis        text CHECK (compare_basis IN ('YOY', 'CORRECTION', 'NEW_EVENT', 'MACRO')),
    basis_label          text,
    event_group_id       text,
    is_grouped_duplicate boolean NOT NULL DEFAULT false,
    rule_version         text NOT NULL DEFAULT '4.1',
    detected_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mart_change_events_review ON mart.change_events (review_date, corp_code);
CREATE INDEX IF NOT EXISTS ix_mart_change_events_rule ON mart.change_events (review_date, rule_id);
-- 기업 삭제(DELETE ... WHERE corp_code = ?)가 전체 스캔을 타지 않게.
CREATE INDEX IF NOT EXISTS ix_mart_change_events_corp ON mart.change_events (corp_code);

-- 기업별 재검토 우선순위 요약 (대시보드 상단 랭킹용)
--
-- v4.0: 합산 점수(risk_score/positive_score/forced_high)를 버리고 대표 사건 하나를
-- 선출해 그 사건의 grade/score/is_emergency를 그대로 기업 등급으로 쓴다. 집계 로직은
-- run_rules.aggregate_company_priority 참고. "판정불가"/"적용제외"는 SQL 엔진에
-- 미반영 - README_rule_engine.md 참고.
CREATE TABLE IF NOT EXISTS mart.company_priority (
    corp_code                   text NOT NULL,
    corp_name                   text NOT NULL,
    review_date                 date NOT NULL,
    priority_level              text NOT NULL
        CHECK (priority_level IN ('emergency', 'high', 'mid', 'low', 'none')),
    priority_score              numeric NOT NULL DEFAULT 0,
    is_emergency                boolean NOT NULL DEFAULT false,
    change_count                integer NOT NULL DEFAULT 0,
    financial_rule_count        integer NOT NULL DEFAULT 0,  -- F1-F8 매칭 수
    disclosure_rule_count       integer NOT NULL DEFAULT 0,  -- D1-D10 매칭 수 (정정공시 D5 포함)
    -- 대표 사건과 그 다음으로 중요한 사건
    top_rule_id                 text,
    top_event_id                bigint,
    additional_important_events integer NOT NULL DEFAULT 0,
    reason_text                 text,
    reason_text_2               text,
    -- 비교 기준별 건수 (YoY / 정정 / 신규 이벤트)
    yoy_event_count             integer NOT NULL DEFAULT 0,
    correction_event_count      integer NOT NULL DEFAULT 0,
    new_event_count             integer NOT NULL DEFAULT 0,
    -- "금리 변경 + 차입 의존" 노출 대상인지, 그 사유 (mart.rate_context 조인 결과)
    rate_exposure               boolean NOT NULL DEFAULT false,
    rate_context_text           text,
    rule_version                text NOT NULL DEFAULT '4.1',
    PRIMARY KEY (corp_code, review_date)
);
CREATE INDEX IF NOT EXISTS ix_mart_company_priority_review ON mart.company_priority (review_date, priority_level);

-- 해설문장(접수번호·문장번호), 근거 원문 섹션 필수
--
-- sentence_type: ①변화(원문에서 추출, LLM+근거검증) / ②사유(rule_catalog.description
-- 그대로, 결정론적) / ③확인(core.rule_checklist에서만, 결정론적)을 한 문장으로 합치지
-- 않고 구분해서 저장한다 - 화면에서 세 구역으로 나눠 보여주기 위함(팀 리뷰). ②③은
-- evidence_*를 "원문 발췌"가 아니라 그 출처(rule_id/체크리스트 항목 자체)로 채운다 -
-- 체크리스트/카탈로그 존재 자체가 근거이기 때문.
CREATE TABLE IF NOT EXISTS mart.explanation_sentences (
    rcept_no          text NOT NULL,
    sentence_no       integer NOT NULL,
    sentence_type     text NOT NULL DEFAULT '변화' CHECK (sentence_type IN ('변화', '사유', '확인')),
    sentence_text     text NOT NULL,
    evidence_rcept_no text NOT NULL,
    evidence_section  text NOT NULL,
    evidence_excerpt  text NOT NULL,
    PRIMARY KEY (rcept_no, sentence_no)
);

-- KPI 타일 (FR-04: 변화 발생 기업 수 / 중요도 높은 기업 수 / 신규·정정 공시 수)
CREATE TABLE IF NOT EXISTS mart.kpi_daily (
    review_date             date PRIMARY KEY,
    companies_changed       integer NOT NULL DEFAULT 0,
    companies_high_priority integer NOT NULL DEFAULT 0,
    new_disclosures         integer NOT NULL DEFAULT 0,
    corrections             integer NOT NULL DEFAULT 0,
    rate_changed            boolean NOT NULL DEFAULT false,
    rate_exposed_companies  integer NOT NULL DEFAULT 0
);

-- ECOS 기준금리 맥락 (차입금 증가 기업의 이자 부담 확인용 보조 맥락)
--
-- 입도는 "검토일 + 기업". 금리가 변경됐고 그 기업이 차입 의존도가 높거나 최근 차입
-- 관련 공시가 있으면 그 기업에 직접 노출시킨다 - 예전처럼 "검토일당 1행"으로 금리
-- 숫자만 두면 어느 기업과 관련 있는지 연결이 없어 "그래서 뭐?"가 없다는 팀 리뷰.
-- 금리가 안 바뀐 회차는 기업별로 행을 만들 대상이 없으므로 corp_code='__ALL__'
-- (sentinel - 특정 기업 아님) 한 행만 "변경 없음" 마커로 남긴다.
CREATE TABLE IF NOT EXISTS mart.rate_context (
    review_date     date NOT NULL,
    corp_code       text NOT NULL DEFAULT '__ALL__',
    rate_at_review  numeric,                      -- 검토일 시점 기준금리
    rate_now        numeric,                      -- 적재된 최신 관측치
    rate_direction  text CHECK (rate_direction IN ('up', 'down', 'flat')),
    exposure_reason text,
    PRIMARY KEY (review_date, corp_code)
);
