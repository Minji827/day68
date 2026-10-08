-- powerbi_db: deltawatch(raw/core/mart)와 물리적으로 분리된 별도 DB.
-- Power BI 팀에게 전달할 dim_*/fact_* 스타 스키마만 담는다. deltawatch 쪽 운영
-- 스키마가 바뀌어도 scripts/export_powerbi.py의 동기화 로직만 고치면 되고, 여기
-- 테이블 구조 자체는 영향받지 않는다 (의도적 분리 — 민감정보/운영 변경으로부터 격리).
--
-- 동기화 방식: 실시간 스트리밍 복제가 아니라 "이벤트 트리거" 방식 — webapp.py의
-- /api/analyze가 끝날 때마다 scripts/export_powerbi.py가 그 기업의 최신 상태를
-- 이 DB에 upsert한다. 기업 삭제 시에도 같은 스크립트가 여기서 대응 행을 지운다
-- (두 DB가 물리적으로 분리돼 있어 완전한 원자성은 없음 — deltawatch 삭제가 성공한
-- 직후 powerbi_db 삭제를 시도하는 구조라 드물게 드리프트 가능, 해커톤 규모라 수용).

CREATE TABLE IF NOT EXISTS dim_company (
    corp_code     text PRIMARY KEY,
    stock_code    text,
    company_name  text NOT NULL,
    market        text,   -- TODO: OpenDART company.json(기업개황) 연동 전까지 비어있음
    industry      text    -- TODO: 위와 동일
);

CREATE TABLE IF NOT EXISTS dim_date (
    date       date PRIMARY KEY,
    year       integer NOT NULL,
    quarter    integer NOT NULL,
    month      integer NOT NULL,
    day        integer NOT NULL,
    week       integer NOT NULL,
    day_name   text NOT NULL,
    month_name text NOT NULL
);

-- rule_group: 즉시상향(그 자체로 강제 HIGH)/정정/제도사건/재무변동/조합가산.
-- "조합가산"은 팀 스샷 작성 시점엔 없던 카테고리 — 이번 점수체계 개정(COMBO_* 8개
-- 룰조합 가산점)에서 새로 생겨서 5번째로 추가. 기존 4개 중 하나로 억지로 끼워맞추지
-- 않음 (F1+F2 동시발생 같은 조합 신호를 "제도사건"이나 "재무변동" 어느 쪽에도 넣기
-- 애매해서).
CREATE TABLE IF NOT EXISTS dim_rule (
    rule_id        text PRIMARY KEY,
    rule_group     text NOT NULL CHECK (rule_group IN ('즉시상향', '정정', '제도사건', '재무변동', '조합가산')),
    rule_name      text NOT NULL,
    condition_text text NOT NULL,
    score          integer NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_account (
    account_id   text PRIMARY KEY,
    account_name text NOT NULL,
    unit         text NOT NULL DEFAULT 'KRW'
);

CREATE TABLE IF NOT EXISTS fact_disclosure (
    rcept_no         text PRIMARY KEY,
    corp_code        text NOT NULL REFERENCES dim_company (corp_code) ON DELETE CASCADE,
    rcept_date       date NOT NULL,
    report_name      text NOT NULL,
    disclosure_type  text NOT NULL CHECK (disclosure_type IN ('신규', '정정')),
    orig_rcept_no    text,
    url              text NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fact_disclosure_corp ON fact_disclosure (corp_code);

-- rcept_no는 best-effort 연결(재무제표 API는 공시 목록과 별도라 접수번호가 원래 없음) —
-- 같은 사업연도의 사업/반기/분기보고서로 제목 매칭 성공한 경우만 채워짐, 실패하면 NULL.
CREATE TABLE IF NOT EXISTS fact_financial_change (
    corp_code      text NOT NULL REFERENCES dim_company (corp_code) ON DELETE CASCADE,
    report_period  text NOT NULL,  -- bsns_year
    rcept_no       text REFERENCES fact_disclosure (rcept_no),
    account_id     text NOT NULL REFERENCES dim_account (account_id),
    fs_div         text NOT NULL,  -- 연결/별도
    prev_value     numeric,
    curr_value     numeric,
    change_pct     numeric,        -- 양쪽 다 양수일 때만 채움
    change_label   text,           -- 적자확대/적자축소/흑자전환/적자전환 (부호가 걸치거나 0 이하 포함 시)
    PRIMARY KEY (corp_code, report_period, account_id, fs_div)
);

CREATE TABLE IF NOT EXISTS fact_rule_hit (
    corp_code   text NOT NULL REFERENCES dim_company (corp_code) ON DELETE CASCADE,
    review_date date NOT NULL,
    rule_id     text NOT NULL REFERENCES dim_rule (rule_id),
    rcept_no    text,
    event_date  date,
    prev_value  numeric,
    curr_value  numeric,
    score       integer NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fact_rule_hit_corp_review ON fact_rule_hit (corp_code, review_date);

CREATE TABLE IF NOT EXISTS fact_priority (
    corp_code         text NOT NULL REFERENCES dim_company (corp_code) ON DELETE CASCADE,
    review_date       date NOT NULL,
    total_score       integer NOT NULL,
    score_correction  integer NOT NULL,  -- D5
    score_event       integer NOT NULL,  -- D1,D2,D4,D6
    score_financial   integer NOT NULL,  -- F1-F7
    override_flag     boolean NOT NULL,
    grade             text NOT NULL,
    rank              integer,
    PRIMARY KEY (corp_code, review_date)
);

CREATE TABLE IF NOT EXISTS fact_disclosure_diff (
    rcept_no       text NOT NULL REFERENCES fact_disclosure (rcept_no) ON DELETE CASCADE,
    orig_rcept_no  text NOT NULL,
    item_seq       integer NOT NULL,
    item_name      text NOT NULL,
    before_text    text,
    after_text     text,
    PRIMARY KEY (rcept_no, item_seq)
);

CREATE TABLE IF NOT EXISTS fact_explanation (
    rcept_no            text NOT NULL REFERENCES fact_disclosure (rcept_no) ON DELETE CASCADE,
    sentence_seq        integer NOT NULL,
    sentence            text NOT NULL,
    evidence_rcept_no   text NOT NULL,
    evidence_location   text NOT NULL,
    evidence_excerpt    text NOT NULL,
    PRIMARY KEY (rcept_no, sentence_seq)
);

CREATE TABLE IF NOT EXISTS fact_base_rate (
    date       date PRIMARY KEY,
    rate       numeric NOT NULL,
    prev_rate  numeric,
    direction  text CHECK (direction IN ('인상', '동결', '인하'))
);
