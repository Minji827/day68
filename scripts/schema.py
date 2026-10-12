"""스키마 적용기. DDL -> 마이그레이션 -> 시드, 세 단계를 이 순서로만 적용한다.

디렉터리마다 역할이 다르다.

- `sql/*.sql`           = **DDL = 현재 스키마 그 자체**. CREATE TABLE/INDEX/VIEW/
  FUNCTION만 들어있고 전부 멱등이라 매 기동마다 다시 돌려도 안전하다. "지금 테이블이
  어떻게 생겼나"는 이 파일들만 읽으면 된다.
- `sql/migrations/*.sql` = **그 형태 이전에 만들어진 DB를 올리기 위한 일회성 변형**.
  meta.schema_migrations에 파일명을 기록해서 한 번만 실행한다.
- `sql/seeds/*.sql`      = **기준 데이터**(계정 매핑, 룰 카탈로그, 체크리스트).
  전부 ON CONFLICT DO UPDATE라 매번 다시 돌려도 안전하다.

순서가 왜 이래야 하는지:

1. DDL이 먼저 - 새 DB에 테이블이 생겨야 마이그레이션의 ALTER가 걸 대상이 있다
   (마이그레이션을 먼저 돌리면 빈 DB에서 "relation does not exist"로 죽는다).
2. 마이그레이션이 그다음 - CREATE TABLE IF NOT EXISTS는 **이미 있는** 테이블에
   컬럼을 추가해주지 않는다. 구 DB를 현재 형태로 맞추는 건 ALTER뿐이다.
3. 시드가 마지막 - 시드 INSERT가 참조하는 컬럼(예: rule_catalog.base_score)은
   구 DB에선 2단계가 끝나야 존재한다. 예전처럼 DDL과 시드가 한 파일에 섞여 있으면
   구 DB 업그레이드가 여기서 깨진다(실측으로 확인하고 분리함).

예전에는 ALTER 묶음이 CREATE TABLE 바로 밑에 누적돼 셋이 전부 섞여 있었다. 그래서
(a) 현재 형태를 파일만 읽어서는 알 수 없었고, (b) CREATE와 ALTER가 따로 놀다가
mart.rate_context처럼 CREATE만 사라져도 기존 DB에서는 안 터져서 발견이 늦었다
(새 DB에서만 "relation does not exist"로 실패).

컬럼을 새로 추가할 때는 양쪽 모두에 써야 한다 - sql/*.sql의 CREATE TABLE(새 DB가
처음부터 그 형태로 생기도록)과 새 sql/migrations/NNN_*.sql(이미 있는 DB가 따라오도록).

Usage:
    python scripts/init_db.py          # CLI
    schema.apply_all(conn)             # 코드에서
"""
from __future__ import annotations

from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
MIGRATIONS_DIR = SQL_DIR / "migrations"
SEEDS_DIR = SQL_DIR / "seeds"

_TRACKING_DDL = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def ddl_files() -> list[Path]:
    """현재 형태 DDL. migrations/·seeds/ 하위는 glob('*.sql')에 안 잡힌다."""
    return sorted(SQL_DIR.glob("*.sql"))


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.is_dir() else []


def seed_files() -> list[Path]:
    return sorted(SEEDS_DIR.glob("*.sql")) if SEEDS_DIR.is_dir() else []


def apply_all(conn, log=print) -> dict:
    """DDL -> 아직 안 돌린 마이그레이션 -> 시드 순으로 적용한다(순서 근거는 모듈 docstring).

    전체를 한 트랜잭션으로 묶는다 - 중간에 실패하면 반쯤 적용된 스키마가 남지 않는다.
    """
    applied: list[str] = []
    with conn:
        with conn.cursor() as cur:
            for f in ddl_files():
                cur.execute(f.read_text(encoding="utf-8"))

            cur.execute(_TRACKING_DDL)
            cur.execute("SELECT filename FROM meta.schema_migrations")
            done = {r[0] for r in cur.fetchall()}

            for f in migration_files():
                if f.name in done:
                    continue
                log(f"  마이그레이션 적용: {f.name}")
                cur.execute(f.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO meta.schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING",
                    (f.name,),
                )
                applied.append(f.name)

            for f in seed_files():
                cur.execute(f.read_text(encoding="utf-8"))

    return {
        "ddl_files": len(ddl_files()),
        "seed_files": len(seed_files()),
        "migrations_applied": applied,
    }
