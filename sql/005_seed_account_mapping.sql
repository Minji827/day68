-- FR-01 요구 재무항목(매출액/영업이익/부채총계/영업활동현금흐름/차입금) + F4·D1 규칙용
-- (자본총계/단기차입금) 표준화 매핑.
-- "차입금"은 단일 계정이 아니라 장기/단기로 쪼개져 있고, 계정명 자체도 회사마다 다르게
-- 표기됨 (삼성전자 실測: '영업이익'/'영업활동현금흐름', 한화솔루션 실測:
-- '영업이익(손실)'/'영업활동으로 인한 현금흐름'). 새 기업 추가 시 raw 계정명을 먼저
-- 확인하고 여기에 변형을 추가할 것 — 매핑에 없는 계정명은 조용히 누락되므로 주의.
-- 단기차입금은 두 표준계정에 동시 기여: BORROWINGS(장단기 합산, F5용)와
-- ST_BORROWINGS(단기만 단독, D1용).

INSERT INTO core.account_mapping (account_nm_raw, account_std_code, account_std_name, agg_method) VALUES
    ('매출액',                   'REVENUE',        '매출액',          'direct'),
    ('영업이익',                 'OP_INCOME',      '영업이익',         'direct'),
    ('영업이익(손실)',            'OP_INCOME',      '영업이익',         'direct'),
    ('부채총계',                 'TOTAL_LIAB',     '부채총계',         'direct'),
    ('자본총계',                 'TOTAL_EQUITY',   '자본총계',         'direct'),
    ('영업활동현금흐름',          'OCF',            '영업활동현금흐름',  'direct'),
    ('영업활동으로 인한 현금흐름', 'OCF',            '영업활동현금흐름',  'direct'),
    ('장기차입금',               'BORROWINGS',     '차입금',          'sum'),
    ('단기차입금',               'BORROWINGS',     '차입금',          'sum'),
    ('단기차입금',               'ST_BORROWINGS',  '단기차입금',       'direct')
ON CONFLICT (account_nm_raw, account_std_code) DO NOTHING;
