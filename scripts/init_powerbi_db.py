"""powerbi_db 최초 셋업: DB 자체 생성(없으면) -> sql/powerbi/*.sql 적용 -> dim_date 시딩.
deltawatch와 물리적으로 분리된 별도 데이터베이스라 CREATE DATABASE부터 필요하다 —
이건 일반 DDL과 달리 트랜잭션 안에서 못 돌리므로 autocommit 커넥션으로 따로 처리.

Usage:
    .venv/Scripts/python scripts/init_powerbi_db.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "powerbi"
MONTH_NAMES = ["1월", "2월", "3월", "4월", "5월", "6월", "7월", "8월", "9월", "10월", "11월", "12월"]
DAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]


def ensure_database_exists() -> None:
    target = urlparse(db.POWERBI_DATABASE_URL)
    db_name = target.path.lstrip("/")
    # powerbi_db 자체를 만들려면 이미 존재하는 다른 DB(운영 deltawatch)에 붙어서
    # CREATE DATABASE를 실행해야 한다 - 자기 자신에는 연결 못 함.
    admin_url = db.DATABASE_URL
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True  # CREATE DATABASE는 트랜잭션 블록 안에서 실행 불가
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone():
                print(f"DB '{db_name}' 이미 존재함")
                return
            cur.execute(f'CREATE DATABASE "{db_name}"')
            print(f"DB '{db_name}' 생성 완료")
    finally:
        conn.close()


def apply_schema() -> None:
    conn = db.get_powerbi_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for f in sorted(SQL_DIR.glob("*.sql")):
                    print(f"Applying {f.name} ...")
                    cur.execute(f.read_text(encoding="utf-8"))
    finally:
        conn.close()


def seed_dim_date(start: date, end: date) -> None:
    """날짜 스팬을 넉넉하게(기본 2024-01-01 ~ 2030-12-31) 한 번에 채워둔다 - 캘린더
    테이블이라 수집된 데이터 범위와 무관하게 고정 생성, 나중에 다시 안 건드림."""
    rows = []
    d = start
    while d <= end:
        iso_year, iso_week, iso_weekday = d.isocalendar()
        rows.append(
            (d, d.year, (d.month - 1) // 3 + 1, d.month, d.day, iso_week, DAY_NAMES[d.weekday()], MONTH_NAMES[d.month - 1])
        )
        d += timedelta(days=1)

    conn = db.get_powerbi_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO dim_date (date, year, quarter, month, day, week, day_name, month_name)
                    VALUES %s
                    ON CONFLICT (date) DO NOTHING
                    """,
                    rows,
                )
        print(f"dim_date: {len(rows)}건 시딩 ({start} ~ {end})")
    finally:
        conn.close()


def main() -> None:
    ensure_database_exists()
    apply_schema()
    seed_dim_date(date(2024, 1, 1), date(2030, 12, 31))
    print("OK")


if __name__ == "__main__":
    main()
