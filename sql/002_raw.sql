-- RAW: API 응답 원본을 그대로 저장 (PRD 9번). 호출 단위로 적재하고, 변환은 CORE에서 수행.

CREATE TABLE IF NOT EXISTS raw.opendart_corp_code (
    corp_code   text PRIMARY KEY,
    corp_name   text NOT NULL,
    stock_code  text,
    modify_date text,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.opendart_disclosure_calls (
    call_id      bigserial PRIMARY KEY,
    corp_code    text NOT NULL,
    bgn_de       text NOT NULL,
    end_de       text NOT NULL,
    response     jsonb NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_disclosure_calls_corp ON raw.opendart_disclosure_calls (corp_code);

CREATE TABLE IF NOT EXISTS raw.opendart_financial_calls (
    call_id      bigserial PRIMARY KEY,
    corp_code    text NOT NULL,
    bsns_year    text NOT NULL,
    reprt_code   text NOT NULL,
    fs_div       text NOT NULL,
    response     jsonb NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_financial_calls_corp ON raw.opendart_financial_calls (corp_code, bsns_year);

CREATE TABLE IF NOT EXISTS raw.opendart_document_files (
    rcept_no     text PRIMARY KEY,
    file_path    text NOT NULL,
    byte_size    integer,
    fetched_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.ecos_rate_calls (
    call_id      bigserial PRIMARY KEY,
    stat_code    text NOT NULL,
    item_code    text NOT NULL,
    cycle        text NOT NULL,
    start_date   text NOT NULL,
    end_date     text NOT NULL,
    response     jsonb NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_ecos_calls_stat ON raw.ecos_rate_calls (stat_code, item_code);
