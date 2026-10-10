-- 기업×표준계정별로 "가장 최신 사업연도" 값 1건만 뽑는 뷰 (CFS 우선, 없으면 OFS).
-- F1~F5 규칙이 전부 이 뷰 위에서 동작한다.
CREATE OR REPLACE VIEW core.latest_financials AS
SELECT DISTINCT ON (corp_code, account_std_code)
    corp_code, account_std_code, account_std_name, bsns_year, reprt_code, fs_div,
    thstrm_amount, frmtrm_amount
FROM core.financial_accounts
WHERE reprt_code = '11011'
ORDER BY corp_code, account_std_code, bsns_year DESC, (fs_div = 'CFS') DESC;

-- v4.3-1: 기업별 "직전 검토일" - D-rule(신규/정정 공시)의 "검토 기준일 이후 신규"
-- 판정이 이번 실행 자체의 review_date(보통 오늘)와 비교하던 버그를 고친다. 오늘 날짜로
-- 비교하면 "오늘보다 미래인 공시"는 있을 수 없어 구조적으로 항상 0건이 된다(실측: 이스트
-- 에이드의 관리종목지정우려·거래정지 공시가 전부 과거 날짜라 D7/D8이 못 잡음).
-- 뷰가 아니라 함수인 이유: "이번 실행보다 이전" 검토일만 돌려줘야 하는데, 단순히
-- max(review_date)만 보는 뷰는 테스트용으로 과거 날짜를 다시 돌릴 때(이 프로젝트의
-- 실제 운영 방식 - 라이브 날짜 외에 2026-08-26/09-01 등으로도 엔진을 재검증함) 그보다
-- "미래"에 이미 있는 리뷰 행까지 집어서 틀린 값을 줄 수 있다. p_review_date 미만으로
-- 명시적으로 제한해야 날짜를 비순차적으로 재실행해도 항상 맞는다(새 컬럼/테이블 없이
-- 기존 이력에서 유도 - 롤백 시 DROP FUNCTION만으로 원복).
CREATE OR REPLACE FUNCTION core.last_review_before(p_corp_code text, p_review_date date)
RETURNS date AS $$
    SELECT max(review_date) FROM mart.company_priority
    WHERE corp_code = p_corp_code AND review_date < p_review_date
$$ LANGUAGE sql STABLE;
