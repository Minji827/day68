-- FR-01 요구 재무항목(매출액/영업이익/부채총계/영업활동현금흐름/차입금) + F4·D1 규칙용
-- (자본총계/단기차입금) 표준화 매핑.
-- "차입금"은 단일 계정이 아니라 장기/단기로 쪼개져 있고, 계정명 자체도 회사마다 다르게
-- 표기됨 (삼성전자 실測: '영업이익'/'영업활동현금흐름', 한화솔루션 실測:
-- '영업이익(손실)'/'영업활동으로 인한 현금흐름'). 새 기업 추가 시 raw 계정명을 먼저
-- 확인하고 여기에 변형을 추가할 것 — 매핑에 없는 계정명은 조용히 누락되므로 주의.
-- 단기차입금은 두 표준계정에 동시 기여: BORROWINGS(장단기 합산, F5용)와
-- ST_BORROWINGS(단기만 단독, D1용).

-- v4.2-1: 당기순이익(NET_INCOME) 추가 — 분기 차트에 순이익률(당기순이익/매출액)을
-- 보여주기 위해 필요(팀 리뷰: "영업이익률뿐 아니라 순이익률도 같이 보는 지표").
-- 사업보고서는 '당기순이익(손실)', 분기/반기보고서는 '분기순이익(손실)'/'반기순이익
-- (손실)'로 계정명이 다르게 나온다(실제 수집 데이터로 확인) — 전부 같은 표준계정으로.
-- v4.3-2: 영업수익(REVENUE) 추가 — NAVER·이스트에이드·SK스퀘어처럼 지주사/플랫폼
-- 기업은 매출을 "매출액"이 아니라 "영업수익"으로 공시해서, 이 계정명이 없으면
-- REVENUE가 통째로 비어 F3(매출 YoY)가 평가 자체를 못 한다(실측: NAVER도 REVENUE 0건
-- 이었음). 다만 SK스퀘어는 같은 보고서에 "매출액"과 "영업수익"을 같은 금액으로 둘 다
-- 공시해서, 이 둘을 합산하면 매출이 2배로 뻥튀기된다(scripts/load_core.py의
-- EXCLUSIVE_STD_CODE_PRIORITY가 "매출액" 우선으로 하나만 골라 처리 — 합산 안 함).
INSERT INTO core.account_mapping (account_nm_raw, account_std_code, account_std_name, agg_method) VALUES
    ('매출액',                   'REVENUE',        '매출액',          'direct'),
    ('영업수익',                 'REVENUE',        '매출액',          'direct'),
    ('영업이익',                 'OP_INCOME',      '영업이익',         'direct'),
    ('영업이익(손실)',            'OP_INCOME',      '영업이익',         'direct'),
    ('부채총계',                 'TOTAL_LIAB',     '부채총계',         'direct'),
    ('자본총계',                 'TOTAL_EQUITY',   '자본총계',         'direct'),
    ('영업활동현금흐름',          'OCF',            '영업활동현금흐름',  'direct'),
    ('영업활동으로 인한 현금흐름', 'OCF',            '영업활동현금흐름',  'direct'),
    ('장기차입금',               'BORROWINGS',     '차입금',          'sum'),
    ('단기차입금',               'BORROWINGS',     '차입금',          'sum'),
    ('단기차입금',               'ST_BORROWINGS',  '단기차입금',       'direct'),
    ('당기순이익',               'NET_INCOME',     '당기순이익',       'direct'),
    ('당기순이익(손실)',          'NET_INCOME',     '당기순이익',       'direct'),
    ('분기순이익',               'NET_INCOME',     '당기순이익',       'direct'),
    ('분기순이익(손실)',          'NET_INCOME',     '당기순이익',       'direct'),
    ('반기순이익',               'NET_INCOME',     '당기순이익',       'direct'),
    ('반기순이익(손실)',          'NET_INCOME',     '당기순이익',       'direct')
ON CONFLICT (account_nm_raw, account_std_code) DO NOTHING;
