"""Apply sql/*.sql (현재 스키마) + sql/migrations/*.sql (일회성 변형) to DATABASE_URL.

적용 규칙은 scripts/schema.py 참고.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import schema


def main() -> None:
    conn = db.get_conn()
    try:
        result = schema.apply_all(conn)
        print(f"DDL {result['ddl_files']}개 파일 적용")
        if result["migrations_applied"]:
            print(f"마이그레이션 {len(result['migrations_applied'])}건 적용")
        else:
            print("새로 적용할 마이그레이션 없음")
        print(f"시드 {result['seed_files']}개 파일 적용")
        print("OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
