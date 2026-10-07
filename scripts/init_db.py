"""Apply sql/*.sql in order against DATABASE_URL."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def main() -> None:
    files = sorted(SQL_DIR.glob("*.sql"))
    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for f in files:
                    print(f"Applying {f.name} ...")
                    cur.execute(f.read_text(encoding="utf-8"))
        print("OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
