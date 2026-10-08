-- 분기별 재무 추이 차트용. DART 분기보고서(1분기/반기/3분기/사업보고서)는 전부 그 시점까지의
-- "누적치"라서 (반기=1+2분기 누적, 3분기보고서=1~3분기 누적) 순수 그 분기만의 값은
-- scripts/quarterly.py가 누적치를 빼서 직접 계산해 이 테이블에 저장한다 (core.financial_accounts는
-- DART가 준 원래 누적치 그대로 두고 건드리지 않음 - 이 테이블이 파생 결과).
-- 올해 분기값(curr_amount)과 전년 동분기값(prior_amount)을 같이 저장 — DART 분기보고서 응답
-- 자체가 thstrm(올해 누적)/frmtrm(작년 동기간 누적) 쌍으로 오기 때문에 한 번의 수집으로 두 해를
-- 같이 계산할 수 있다.
CREATE TABLE IF NOT EXISTS core.quarterly_financials (
    corp_code         text NOT NULL REFERENCES core.companies (corp_code),
    bsns_year         text NOT NULL,              -- 올해 사업연도
    quarter           integer NOT NULL CHECK (quarter IN (1, 2, 3, 4)),
    account_std_code  text NOT NULL,
    account_std_name  text NOT NULL,
    curr_amount       numeric,                    -- 올해 해당 분기만의 순수값 (결측이면 NULL - 추정 안 함)
    prior_amount      numeric,                    -- 작년 같은 분기만의 순수값
    fs_div            text NOT NULL,              -- 실제로 사용된 연결/별도 (CFS 우선, 없으면 OFS)
    PRIMARY KEY (corp_code, bsns_year, quarter, account_std_code)
);
CREATE INDEX IF NOT EXISTS ix_core_quarterly_financials_corp ON core.quarterly_financials (corp_code, bsns_year);
