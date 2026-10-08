from __future__ import annotations

import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
POWERBI_DATABASE_URL = os.environ.get("POWERBI_DATABASE_URL", "")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set (check .env)")
    return psycopg2.connect(DATABASE_URL)


def get_powerbi_conn():
    """deltawatch(운영)와 물리적으로 분리된 powerbi_db 커넥션. Power BI 전달용
    dim_*/fact_* 테이블만 들어있다 — scripts/export_powerbi.py 참고."""
    if not POWERBI_DATABASE_URL:
        raise RuntimeError("POWERBI_DATABASE_URL not set (check .env)")
    return psycopg2.connect(POWERBI_DATABASE_URL)
