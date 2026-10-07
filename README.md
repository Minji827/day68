# DeltaWatch AI — 기업 변화 탐지 Agent

마지막 검토일 이후 달라진 공시·재무지표·금리환경만 탐지해 재검토 우선순위와
신구 대비 해설서를 제공하는 AI Agent. (KIC AI 에이전트 공모전 제출작)

데이터 흐름: OpenDART + ECOS → PostgreSQL(RAW → CORE → MART) → SQL 변화 탐지
→ OpenAI 해설서 생성 → Power BI 시각화 (+ n8n 이메일 발송)

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env   # OPENDART_API_KEY / ECOS_API_KEY / OPENAI_API_KEY / DATABASE_URL 채우기
```

`.env`는 git에 커밋되지 않습니다 (`.gitignore` 처리).

### PostgreSQL 실행

Docker Desktop이 없다면 WSL2에 docker가 깔려 있는지 확인하고 (`wsl -d Ubuntu -- docker --version`),
WSL 안에서 compose로 띄운다. **WSL 유틸리티 VM은 기본적으로 유휴 시 자동 종료되므로
`%USERPROFILE%\.wslconfig`에 `vmIdleTimeout=-1`을 넣어 끄지 않는 게 중요** (안 하면
localhost:5432 연결이 몇 분마다 끊김 — 실측 확인된 이슈).

```bash
wsl -d Ubuntu -- bash -c "cd /mnt/c/Users/redsu/day68 && docker compose up -d"
```

WSL2는 localhost 포트포워딩을 지원해서 Windows 쪽 Python(`DATABASE_URL`)에서 바로
`localhost:5432`로 붙는다. Docker Desktop이 정상 설치되어 있다면 그냥
`docker compose up -d`만 Windows에서 실행하면 된다.

## 현재까지 구현

**수집 (OpenDART / ECOS)**
- `scripts/opendart_client.py` — corp_code 조회, 공시목록, 재무제표, 공시원문 다운로드
- `scripts/ecos_client.py` — 기준금리 등 통계 조회
- `scripts/test_apis.py` — 두 API 실제 연동 테스트 (PRD 11번 리스크 항목 확인용)
- `scripts/collect.py` — 회사명으로 공시/재무/원문/기준금리를 받아 `data/raw/`에 원본 저장
  (`data/raw/`는 git에 올리지 않음)

**DB (RAW → CORE → MART)**
- `sql/001~004_*.sql` — 스키마 DDL. `sql/005_seed_account_mapping.sql` — 계정명 표준화 시드.
  `sql/006_rule_catalog.sql` — 변화 탐지 규칙표(rule_id/가중치/근거). `sql/007_views.sql` —
  `core.latest_financials` 뷰 (기업×표준계정별 최신 사업연도 1건, CFS 우선)
- `scripts/init_db.py` — `sql/*.sql`을 순서대로 적용
- `scripts/load_raw.py` — `data/raw/*` → `raw.*` 테이블 적재
- `scripts/load_core.py` — `raw.*` → `core.*` 변환 (중복 제거, 정정공시 연결, 계정 표준화)
- `scripts/run_rules.py` — **변화 탐지 SQL 룰엔진.** F1~F5(재무) + D1~D5(공시 가산) +
  CTX1(기준금리 맥락, 무점수) 실행 → `mart.change_events` / `mart.company_priority` /
  `mart.kpi_daily` / `mart.rate_context` 채움

```bash
.venv/Scripts/python scripts/collect.py --corp 삼성전자 --bgn-de 20250101 --end-de 20261007
.venv/Scripts/python scripts/init_db.py
.venv/Scripts/python scripts/load_raw.py
.venv/Scripts/python scripts/load_core.py
.venv/Scripts/python scripts/run_rules.py --review-date 2026-01-01
```

한화솔루션 실데이터로 전체 파이프라인(수집→RAW→CORE→규칙엔진) 검증 완료:
F2(영업활동현금흐름 흑자→적자, 6,385억→-6,550억) + F5(차입금 +22.3%, 2개 규칙 모두
20% 기준값 초과) + D2(유상증자 결정 2건) + D5(정정공시 6건, 2건 원공시 연결/4건 "정정
전 공시 없음") → 합산 13점(가중치 3+2+1×8) → `priority_level='high'` 정상 산출.

### 변화 탐지 규칙 (`core.rule_catalog`)

| rule_id | 유형 | 조건 | 가중치 | 비고 |
|---|---|---|---|---|
| F1 | 재무 | 영업이익 흑자→적자 전환 | 3(높음) | |
| F2 | 재무 | 영업활동현금흐름 흑자→적자 전환 | 3(높음) | |
| F3 | 재무 | 매출 YoY ≤ -10% | 2(중간) | **방향·기준값 모두 근거 없음, 팀 설정값** |
| F4 | 재무 | 부채비율(부채총계/자본총계) +20%p 이상 | 2(중간) | 방향은 근거 있음, 20%p는 팀 설정 |
| F5 | 재무 | 차입금(장단기 합산) +20% 이상 | 2(중간) | 방향은 근거 있음, 20%는 팀 설정 |
| D1 | 공시 | 단기차입금 직전 대비 증가 | 1 | |
| D2 | 공시 | 유상증자 결정 공시 | 1 | |
| D3 | 공시 | 감사의견 비적정 | 1 | **공시 제목 키워드 매칭 — best-effort, 원문 파싱 전까지 놓칠 수 있음(TODO)** |
| D4 | 공시 | 최대주주 변경 | 1 | 공시 1건당 +1 적재 → 같은 기간 2건 이상이면 합산 점수가 자연히 누적(강화) |
| D5 | 공시 | 정정공시 | 1 | |
| CTX1 | 맥락 | 기준금리 상승 + 차입금 증가 | 0 | 점수 미반영, `mart.rate_context`에 표시만 |

점수 합산 ≥5 `high`, ≥2 `mid`, 그 외 `low` (`scripts/run_rules.py`의
`HIGH_THRESHOLD`/`MID_THRESHOLD`). **이 임계값도 근거 문헌이 뒷받침하는 값이 아니라
팀 설정값 — 샘플 기업 여러 개로 돌려보고 조정할 것.**

## 실측으로 확인된 리스크 (PRD 11번 제약·위험 대응)

- **정정공시 ↔ 원공시 연결**: `list.json`에 원공시 접수번호 필드가 없음. `report_nm`에서
  `[기재정정]` 등 접두어를 제거한 뒤 같은 기업의 더 이른 공시 중 제목이 일치하는 것을 찾는
  방식(`core.disclosures.orig_rcept_no`)으로 처리. 한화솔루션 실데이터에서 6건 중 2건 매칭,
  4건은 수집 기간 밖이라 "정정 전 공시 없음"(`match_method='unmatched'`)으로 처리됨 —
  정상 동작.
- **계정명이 회사마다 다름**: "차입금" 단일 계정이 없고 `장기차입금`/`단기차입금`으로
  쪼개져 있어 `core.account_mapping`에서 합산(`agg_method='sum'`). 더 나아가 **계정명 자체도
  회사별로 다름** — 삼성전자는 `영업이익`/`영업활동현금흐름`, 한화솔루션은
  `영업이익(손실)`/`영업활동으로 인한 현금흐름`을 씀. 매핑에 없는 계정명은 조용히
  누락되므로, 새 기업을 추가할 때마다 raw 계정명을 먼저 확인해 `sql/005_*.sql`에
  변형을 추가해야 함.
- **ECOS 기준금리 통계코드**: `722Y001` / 항목 `0101000`, 일별(`D`) 주기 정상 조회됨.
- **공시원문 다운로드**: `document.xml` 정상 동작 (zip 바이너리로 수신).

## 다음 단계

1. ~~PostgreSQL RAW → CORE → MART 스키마~~ 완료
2. ~~변화 탐지 SQL 룰엔진 (재무 규칙 5개 + 공시 가산 규칙)~~ 완료
3. OpenAI 해설서 생성 (원문 근거 필수 필드) → `mart.explanation_sentences`
4. Power BI 대시보드 연결 (`mart` 스키마를 PostgreSQL 커넥터로 직결, flat 테이블이라
   추가 변환 없이 붙을 수 있음)
5. n8n: `mart.company_priority` / `mart.change_events` 조회 → 변화 기업 주간 이메일 발송

## 남은 판단 필요 사항 (team decision)

- F3(매출 YoY 규칙)은 방향·기준값 모두 근거가 없음 — 보조 지표로 낮추거나 MVP에서
  빼는 걸 권장 (PRD 조사 결과 그대로).
- D3(감사의견 비적정)은 현재 공시 제목 키워드 매칭뿐이라 정확도가 낮음. 해설서 생성
  단계에서 공시원문을 파싱하게 되면 그 로직을 D3 판정에도 재사용할 것.
- 점수 구간(5점=높음/2점=중간)과 F3~F5 기준값(10%/20%p/20%)은 여러 기업 샘플로
  돌려보고 조정 필요 — `core.rule_catalog.is_team_assumption=true`인 행 참고.
