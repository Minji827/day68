-- CORE: 중복 제거 + 기간/단위/계정명 표준화 (PRD 9번). 핵심 Entity·PK·FK 기준.

CREATE TABLE IF NOT EXISTS core.companies (
    corp_code  text PRIMARY KEY,
    corp_name  text NOT NULL,
    stock_code text,
    corp_cls   text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 공시(접수번호, FK 기업코드). 정정공시는 원공시 접수번호로 자기참조.
CREATE TABLE IF NOT EXISTS core.disclosures (
    rcept_no         text PRIMARY KEY,
    corp_code        text NOT NULL REFERENCES core.companies (corp_code),
    report_nm        text NOT NULL,
    report_nm_clean  text NOT NULL,          -- "[기재정정]" 등 접두어 제거
    is_correction    boolean NOT NULL DEFAULT false,
    orig_rcept_no    text REFERENCES core.disclosures (rcept_no),
    match_method     text,                   -- 'exact_title' | 'fuzzy_title' | 'unmatched'
    rcept_dt         date NOT NULL,
    flr_nm           text,
    rm               text,
    fetched_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_core_disclosures_corp_dt ON core.disclosures (corp_code, rcept_dt);
CREATE INDEX IF NOT EXISTS ix_core_disclosures_clean_title ON core.disclosures (corp_code, report_nm_clean);

-- 계정명 표준화 매핑. 예: 장기차입금/단기차입금 -> 차입금(합산).
-- raw 계정명 하나가 여러 표준계정에 동시에 기여할 수 있음
-- (예: 단기차입금 -> 차입금(합산 대상) 이면서 동시에 단기차입금(단독, D1 규칙용)).
CREATE TABLE IF NOT EXISTS core.account_mapping (
    account_nm_raw   text NOT NULL,
    account_std_code text NOT NULL,
    account_std_name text NOT NULL,
    agg_method       text NOT NULL DEFAULT 'sum',  -- 같은 std_code로 매핑된 raw 항목들을 합산할지(sum) 단일값(direct)인지
    PRIMARY KEY (account_nm_raw, account_std_code)
);

-- 재무계정값(기업코드·보고기간·계정)
CREATE TABLE IF NOT EXISTS core.financial_accounts (
    corp_code        text NOT NULL REFERENCES core.companies (corp_code),
    bsns_year        text NOT NULL,
    reprt_code       text NOT NULL,
    fs_div           text NOT NULL,
    account_std_code text NOT NULL,
    account_std_name text NOT NULL,
    thstrm_amount    numeric,
    frmtrm_amount    numeric,
    unit             text NOT NULL DEFAULT 'KRW',
    PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div, account_std_code)
);

-- 분기/반기 보고서의 (포괄)손익계산서(IS) 항목은 DART가 thstrm_amount를 이미 "[3개월]"
-- (그 분기만의 값)로 주고, 누적치는 별도 필드(thstrm_add_amount)로 따로 준다 - 공식
-- 개발가이드(opendart.fss.or.kr)에 명시됨. scripts/quarterly.py가 분기별 순수값을
-- 계산할 때 이 필드들이 필요해서 추가한다 (전에는 안 읽고 있었음 - IS 항목에 뺄셈을
-- 잘못 적용해서 음수 매출 같은 오류가 났던 원인).
ALTER TABLE core.financial_accounts ADD COLUMN IF NOT EXISTS thstrm_add_amount numeric;
ALTER TABLE core.financial_accounts ADD COLUMN IF NOT EXISTS frmtrm_q_amount numeric;
ALTER TABLE core.financial_accounts ADD COLUMN IF NOT EXISTS frmtrm_add_amount numeric;

-- 금리관측(지표코드·관측일)
CREATE TABLE IF NOT EXISTS core.rate_observations (
    stat_code text NOT NULL,
    item_code text NOT NULL,
    obs_date  date NOT NULL,
    value     numeric NOT NULL,
    unit      text,
    PRIMARY KEY (stat_code, item_code, obs_date)
);

-- 공시 원문 섹션 (해설서 근거 추출용)
CREATE TABLE IF NOT EXISTS core.disclosure_sections (
    rcept_no      text NOT NULL REFERENCES core.disclosures (rcept_no),
    section_no    integer NOT NULL,
    section_title text,
    section_text  text NOT NULL,
    PRIMARY KEY (rcept_no, section_no)
);
