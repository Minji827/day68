-- 기업×표준계정별로 "가장 최신 사업연도" 값 1건만 뽑는 뷰 (CFS 우선, 없으면 OFS).
-- F1~F5 규칙이 전부 이 뷰 위에서 동작한다.
CREATE OR REPLACE VIEW core.latest_financials AS
SELECT DISTINCT ON (corp_code, account_std_code)
    corp_code, account_std_code, account_std_name, bsns_year, reprt_code, fs_div,
    thstrm_amount, frmtrm_amount
FROM core.financial_accounts
WHERE reprt_code = '11011'
ORDER BY corp_code, account_std_code, bsns_year DESC, (fs_div = 'CFS') DESC;
