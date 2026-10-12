-- [마이그레이션 001] v3.3 -> v4.0/v4.1 스키마 변형
--
-- 이 파일은 "v4.1 이전에 만들어진 DB"를 현재 형태로 올리기 위한 일회성 변형이다.
-- 원래 sql/003_core.sql / 004_mart.sql / 006_rule_catalog.sql 본문에 CREATE TABLE
-- 밑으로 쭉 누적돼 있던 ALTER 묶음을 그대로 옮겨왔다 - 거기 섞여 있으면 (a) 현재
-- 스키마가 뭔지 파일만 읽어서는 알 수 없고, (b) CREATE와 ALTER가 따로 놀다가
-- mart.rate_context처럼 CREATE만 사라지는 사고가 난다(실제로 8209a9e에서 발생).
--
-- 새로 만든 DB에서는 sql/*.sql이 이미 최종 형태로 테이블을 만들기 때문에 아래 구문은
-- 전부 no-op이다(전 구문이 IF EXISTS / IF NOT EXISTS 가드). scripts/schema.py가
-- meta.schema_migrations에 기록해서 두 번 돌리지 않는다.
--
-- 새 컬럼을 추가할 때: sql/*.sql의 CREATE TABLE(= 현재 형태, 단일 진실)과
-- sql/migrations/NNN_*.sql(= 기존 DB 올리는 용도) 양쪽에 써야 한다.

-- ---------------------------------------------------------------------------
-- core.financial_accounts: 분기/반기 보고서 손익계산서 전용 필드
-- 분기/반기 보고서의 (포괄)손익계산서(IS) 항목은 DART가 thstrm_amount를 이미 "[3개월]"
-- (그 분기만의 값)로 주고, 누적치는 thstrm_add_amount로 따로 준다 - 공식 개발가이드에
-- 명시됨. scripts/quarterly.py가 분기별 순수값을 계산할 때 필요하다(전에는 안 읽고
-- 있어서 IS 항목에 뺄셈을 잘못 적용해 음수 매출 같은 오류가 났다).
-- ---------------------------------------------------------------------------
ALTER TABLE core.financial_accounts ADD COLUMN IF NOT EXISTS thstrm_add_amount numeric;
ALTER TABLE core.financial_accounts ADD COLUMN IF NOT EXISTS frmtrm_q_amount numeric;
ALTER TABLE core.financial_accounts ADD COLUMN IF NOT EXISTS frmtrm_add_amount numeric;

-- ---------------------------------------------------------------------------
-- core.rule_catalog: 합산 모델(weight/direction) -> 단일사건 모델(base_score/domain)
-- rule_type='combo'를 더 이상 안 써서 3종으로 되돌린다 - 기존 COMBO_* row는 새 CHECK를
-- 걸기 전에 먼저 지워야 제약 위반이 안 난다. v3.3 전용 D6(v4.0에서 D6A~D6D로 분할)도 정리.
-- ---------------------------------------------------------------------------
DELETE FROM core.rule_catalog WHERE rule_type = 'combo' OR rule_id LIKE 'COMBO_%';
DELETE FROM core.rule_catalog WHERE rule_id = 'D6';
ALTER TABLE core.rule_catalog ADD COLUMN IF NOT EXISTS base_score integer;
ALTER TABLE core.rule_catalog ADD COLUMN IF NOT EXISTS domain text;
ALTER TABLE core.rule_catalog ADD COLUMN IF NOT EXISTS is_emergency_rule boolean NOT NULL DEFAULT false;
ALTER TABLE core.rule_catalog DROP COLUMN IF EXISTS weight;
ALTER TABLE core.rule_catalog DROP COLUMN IF EXISTS direction;
ALTER TABLE core.rule_catalog DROP CONSTRAINT IF EXISTS rule_catalog_rule_type_check;
ALTER TABLE core.rule_catalog ADD CONSTRAINT rule_catalog_rule_type_check
    CHECK (rule_type IN ('financial', 'disclosure', 'context'));

-- ---------------------------------------------------------------------------
-- mart.change_events: weight 합산 제거, 이벤트가 자기 grade/score/direction을 직접 보유
-- v4.0: 같은 rule_id라도 이벤트마다 direction이 다를 수 있어(양방향 룰) rule_catalog
-- 에서 join으로 등급을 끌어올 수 없게 됐다.
-- v4.1: CTX1(거시 맥락)은 점수·등급에 안 들어가는 '참고' 전용 grade가 필요.
--       rule_version으로 어느 룰 버전 산출물인지 추적.
--       compare_basis/basis_label로 YoY·정정·신규이벤트의 비교 기준을 구분.
-- ---------------------------------------------------------------------------
ALTER TABLE mart.change_events DROP CONSTRAINT IF EXISTS change_events_change_type_check;
ALTER TABLE mart.change_events ADD CONSTRAINT change_events_change_type_check
    CHECK (change_type IN ('financial', 'disclosure', 'context'));
ALTER TABLE mart.change_events DROP COLUMN IF EXISTS weight;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS grade text;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS score integer;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS is_emergency boolean NOT NULL DEFAULT false;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS direction text
    CHECK (direction IN ('positive', 'negative', 'neutral'));
ALTER TABLE mart.change_events DROP CONSTRAINT IF EXISTS change_events_grade_check;
ALTER TABLE mart.change_events ADD CONSTRAINT change_events_grade_check
    CHECK (grade IN ('긴급확인', '높음', '중간', '낮음', '참고'));
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS rule_version text NOT NULL DEFAULT '4.1';
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS compare_basis text
    CHECK (compare_basis IN ('YOY', 'CORRECTION', 'NEW_EVENT', 'MACRO'));
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS basis_label text;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS event_group_id text;
ALTER TABLE mart.change_events ADD COLUMN IF NOT EXISTS is_grouped_duplicate boolean NOT NULL DEFAULT false;

-- ---------------------------------------------------------------------------
-- mart.company_priority: 합산 점수 컬럼 제거, 대표 사건 1건을 그대로 기업 등급으로
-- v4.1: 2순위 사유(reason_text_2), 비교 기준별 건수, 금리 노출 컬럼 추가.
-- ---------------------------------------------------------------------------
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
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS reason_text_2 text;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS yoy_event_count integer NOT NULL DEFAULT 0;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS correction_event_count integer NOT NULL DEFAULT 0;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS new_event_count integer NOT NULL DEFAULT 0;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS rate_exposure boolean NOT NULL DEFAULT false;
ALTER TABLE mart.company_priority ADD COLUMN IF NOT EXISTS rate_context_text text;

-- ---------------------------------------------------------------------------
-- mart.explanation_sentences: AI 해설 3단 구조(변화/사유/확인) 구분
-- ---------------------------------------------------------------------------
ALTER TABLE mart.explanation_sentences ADD COLUMN IF NOT EXISTS sentence_type text
    NOT NULL DEFAULT '변화' CHECK (sentence_type IN ('변화', '사유', '확인'));

-- ---------------------------------------------------------------------------
-- mart.kpi_daily: 금리 변경 여부 / 노출 기업 수
-- ---------------------------------------------------------------------------
ALTER TABLE mart.kpi_daily ADD COLUMN IF NOT EXISTS rate_changed boolean NOT NULL DEFAULT false;
ALTER TABLE mart.kpi_daily ADD COLUMN IF NOT EXISTS rate_exposed_companies integer NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- mart.rate_context: 입도(grain)를 review_date -> (review_date, corp_code)로
-- 기존엔 "검토일당 1행"으로 기준금리 숫자만 있어서 어느 기업과 관련 있는지 연결이
-- 없었다. 금리가 안 바뀐 회차는 corp_code='__ALL__'(sentinel) 한 행만 남긴다.
-- PK를 바꾸는 breaking change - n8n/PowerBI 등 이 테이블을 직접 조회하는 쪽에 공지 필요.
-- ---------------------------------------------------------------------------
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS corp_code text NOT NULL DEFAULT '__ALL__';
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS rate_at_review numeric;
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS rate_now numeric;
ALTER TABLE mart.rate_context ADD COLUMN IF NOT EXISTS exposure_reason text;
ALTER TABLE mart.rate_context DROP COLUMN IF EXISTS base_rate;
ALTER TABLE mart.rate_context DROP COLUMN IF EXISTS prior_base_rate;
-- PK 교체는 "이미 (review_date, corp_code)인 경우"엔 건드리지 않는다 - 새로 만든 DB는
-- 처음부터 그 형태라 DROP/ADD를 반복할 이유가 없다(기동마다 인덱스 재생성 방지).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'mart' AND c.relname = 'rate_context' AND i.indisprimary
          AND i.indnatts = 2
    ) THEN
        ALTER TABLE mart.rate_context DROP CONSTRAINT IF EXISTS rate_context_pkey;
        ALTER TABLE mart.rate_context ADD PRIMARY KEY (review_date, corp_code);
    END IF;
END $$;
