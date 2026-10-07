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

### 가장 쉬운 방법: `run_demo.bat`

Docker Desktop(이미 설치되어 있음) 기동 → 스키마 적용 → 샘플 기업 수집 → RAW/CORE
적재 → 규칙 실행 → 결과 출력까지 한 번에 돈다.

```
run_demo.bat [종목코드|회사명] [검토일]
```

인자 없이 실행하면 `005930`(삼성전자) / `2026-01-01`로 기본 실행된다. 6자리 숫자를 주면
종목코드로, 아니면 회사명으로 처리한다 (`run_demo.bat 009830 2026-01-01`처럼).

**기업 지정은 종목코드(`--stock-code`)가 기본값**이다. 회사명(`--corp`)은 모호할 수 있어서
(예: `삼성`은 30개 상장사에 매칭됨) `collect.py`/`compare_disclosure.py`가 후보 여럿이면
추측하지 않고 전부 보여준 뒤 멈춘다 — 종목코드로 다시 지정해야 진행된다.

### PostgreSQL 포트 메모

이 PC에는 **다른 랩 실습용 Postgres(`finance-postgres`, `card_master`/`customer`/`merchant`
등 전혀 다른 스키마)가 이미 Docker Desktop에서 5432 포트를 쓰고 있음** — 우리 프로젝트는
절대 그 컨테이너를 건드리지 않고 **5433** 포트로 분리했다 (`docker-compose.yml`,
`DATABASE_URL`). `docker ps`로 `day68-postgres-1`과 `finance-postgres`가 둘 다 떠 있는 게
정상이다.

수동으로 띄우려면:
```bash
docker compose up -d
```

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
- `scripts/run_demo.py` / `run_demo.bat` — 위 전체 과정을 한 번에 실행하는 원클릭 러너
- `scripts/wait_for_db.py`, `scripts/show_results.py` — 데모 러너 보조 스크립트
- `scripts/compare_disclosure.py` — **정정 전후 비교 (AI 없이, 결정론적).** 원공시·정정공시
  원문을 섹션 단위로 받아 단어 단위 diff(`[-삭제-]{+추가+}`)를 출력. FR-01 AC("정정공시는
  원공시와 연결되어 정정 전·후를 확인할 수 있다")를 실제로 눈에 보이게 하는 기능 —
  `mart.change_events`가 "정정이 있었다"는 점수만 주고 **무엇이 바뀌었는지는 안 보여주던
  공백**을 메움.
  ```bash
  .venv/Scripts/python scripts/compare_disclosure.py --stock-code 005930
  .venv/Scripts/python scripts/compare_disclosure.py --rcept-no 20260814003672
  ```

**해설서 생성 (OpenAI, FR-05) — 실데이터로 검증 완료**
- `scripts/dart_xml.py` — 공시원문(document.xml) 섹션 파서. **실측으로 두 가지 포맷을
  확인**: 표준 DART XML(ATOC 마커로 섹션 구분)과, 일부 정정/자율공시는 깨진 HTML(XFORM
  템플릿)로 내려옴 — strict XML 파싱이 실패해서 HTML fallback(제목+본문 통짜 1~2섹션)을
  추가함. PRD 11번이 예견한 리스크가 실제로 발생한 케이스.
- `scripts/explain.py` — 정정공시(+원공시)를 OpenAI에 넘겨 신구 대비 해설 문장 생성,
  근거 발췌가 실제 원문의 부분 문자열인지 검증 후 통과한 것만 `mart.explanation_sentences`에
  저장 (FR-05 AC: 근거 없는 문장 제외). `collect.py`/`compare_disclosure.py`와 동일하게
  `--stock-code`로 기업 하나만 지정 가능.
  **실제 OpenAI 호출로 검증됨**: 생성된 문장 중 원문과 토씨가 안 맞는 건 자동으로
  REJECTED 처리됨 (예: "2026.06.02 공시 수량이 81에서 34로" — 다른 섹션 수치를 섞어
  쓴 문장을 실제로 걸러냄). 통과한 문장은 전부 원문 발췌와 정확히 일치.

```bash
.venv/Scripts/python scripts/compare_disclosure.py --stock-code 005930 --review-date 2026-01-01
.venv/Scripts/python scripts/explain.py --stock-code 005930 --review-date 2026-01-01
```

```bash
.venv/Scripts/python scripts/collect.py --stock-code 005930 --bgn-de 20250101 --end-de 20261007
.venv/Scripts/python scripts/init_db.py
.venv/Scripts/python scripts/load_raw.py
.venv/Scripts/python scripts/load_core.py
.venv/Scripts/python scripts/run_rules.py --review-date 2026-01-01
```

삼성전자·한화솔루션 실데이터로 전체 파이프라인(수집→RAW→CORE→규칙엔진) `run_demo.bat`
한방으로 검증 완료:
- 한화솔루션: F2(영업활동현금흐름 흑자→적자, 6,385억→-6,550억) + F5(차입금 +22.3%) +
  D2(유상증자 결정 2건) + D5(정정공시 6건, 2건 원공시 연결/4건 "정정 전 공시 없음")
  → 13점 → `HIGH`
- 삼성전자: F5(차입금 +40.6%) + D1(단기차입금 증가) + D5(정정공시 10건, 전부 원공시 연결)
  → 12점 → `HIGH`

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
  방식(`core.disclosures.orig_rcept_no`)으로 처리.
  **제목만으로 매칭하면 실제로 틀린다** — "임원ㆍ주요주주특정증권등소유상황보고서"처럼
  여러 임원이 매일 각자 내는 공통 양식은 제목이 전부 같아서, 박태훈의 정정공시 6건이
  여명구·손영수 등 **다른 사람의 원공시**에 잘못 연결되는 걸 `scripts/compare_disclosure.py`로
  실제 원문을 diff 떠보고서야 발견했다 (숫자만 보면 그럴듯해서 안 터짐 — 조용히 틀린 값이
  나오는 부류의 버그). 수정: 같은 기업+제목에 더해 **제출인(`flr_nm`)까지 일치**해야
  매칭(`exact_title_filer`), 그래도 못 찾으면 제목 후보가 정확히 1개일 때만
  매칭(`exact_title_unique`), 그 외엔 추측하지 않고 "정정 전 공시 없음"(`unmatched`)으로
  둔다. 삼성전자 실데이터 기준 15건 중 7건(제출인 일치)+1건(제목 유일)=8건 매칭,
  7건은 안전하게 미매칭 처리.
- **계정명이 회사마다 다름**: "차입금" 단일 계정이 없고 `장기차입금`/`단기차입금`으로
  쪼개져 있어 `core.account_mapping`에서 합산(`agg_method='sum'`). 더 나아가 **계정명 자체도
  회사별로 다름** — 삼성전자는 `영업이익`/`영업활동현금흐름`, 한화솔루션은
  `영업이익(손실)`/`영업활동으로 인한 현금흐름`을 씀. 매핑에 없는 계정명은 조용히
  누락되므로, 새 기업을 추가할 때마다 raw 계정명을 먼저 확인해 `sql/005_*.sql`에
  변형을 추가해야 함.
- **ECOS 기준금리 통계코드**: `722Y001` / 항목 `0101000`, 일별(`D`) 주기 정상 조회됨.
- **공시원문 다운로드**: `document.xml` 정상 동작 (zip 바이너리로 수신).
- **공시원문 형식이 섞여 있음**: 대부분은 ATOC 마커가 붙은 표준 DART XML이라 섹션이 깔끔히
  나뉘지만, 일부 정정/자율공시(예: 자율공시 정정신고)는 **깨진 HTML(XFORM 템플릿)**로
  내려와서 strict XML 파서가 터짐. `scripts/dart_xml.py`에 HTML fallback 추가로 해결.

## 다음 단계

1. ~~PostgreSQL RAW → CORE → MART 스키마~~ 완료
2. ~~변화 탐지 SQL 룰엔진 (재무 규칙 5개 + 공시 가산 규칙)~~ 완료
3. ~~OpenAI 해설서 생성~~ 완료, 실제 호출로 근거 검증까지 확인
4. ~~Power BI 대시보드 연결~~ 완료 (`mart` 스키마 PostgreSQL 커넥터 직결, `localhost:5433`).
   동일 레이아웃의 스냅샷 HTML 버전도 있음 (팀 공유/발표용): 대시보드 아티팩트 참고
5. ~~n8n 워크플로우~~ 완료 (`n8n/deltawatch_weekly_alert.json`, `docker compose`로 기동,
   `localhost:5680`). Postgres/SMTP 자격증명은 팀원이 직접 입력 필요 (보안상 코드에 안 넣음)

## 남은 판단 필요 사항 (team decision)

- F3(매출 YoY 규칙)은 방향·기준값 모두 근거가 없음 — 보조 지표로 낮추거나 MVP에서
  빼는 걸 권장 (PRD 조사 결과 그대로).
- D3(감사의견 비적정)은 현재 공시 제목 키워드 매칭뿐이라 정확도가 낮음. 해설서 생성
  단계에서 공시원문을 파싱하게 되면 그 로직을 D3 판정에도 재사용할 것.
- 점수 구간(5점=높음/2점=중간)과 F3~F5 기준값(10%/20%p/20%)은 여러 기업 샘플로
  돌려보고 조정 필요 — `core.rule_catalog.is_team_assumption=true`인 행 참고.
