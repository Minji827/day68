"""Load data/raw/* (produced by collect.py) into the raw.* tables."""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import psycopg2.extras

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def load_corp_code(cur) -> None:
    path = DATA_DIR / "corpCode.xml"
    if not path.exists():
        print("corpCode.xml 없음 (collect.py를 먼저 실행하세요) - skip")
        return
    root = ET.parse(path).getroot()
    rows = [
        (
            n.findtext("corp_code"),
            (n.findtext("corp_name") or "").strip(),
            (n.findtext("stock_code") or "").strip() or None,
            n.findtext("modify_date"),
        )
        for n in root.findall("list")
    ]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO raw.opendart_corp_code (corp_code, corp_name, stock_code, modify_date)
        VALUES %s
        ON CONFLICT (corp_code) DO UPDATE SET
            corp_name = EXCLUDED.corp_name,
            stock_code = EXCLUDED.stock_code,
            modify_date = EXCLUDED.modify_date
        """,
        rows,
    )
    print(f"raw.opendart_corp_code: {len(rows)}건 upsert")


def load_disclosure_calls(cur, corp_code: str | None = None) -> None:
    pattern = re.compile(r"^(\d+)_(\d{8})_(\d{8})\.json$")
    n = 0
    for f in (DATA_DIR / "disclosures").glob("*.json"):
        m = pattern.match(f.name)
        if not m:
            continue
        file_corp_code, bgn_de, end_de = m.groups()
        if corp_code and file_corp_code != corp_code:
            continue
        response = json.loads(f.read_text(encoding="utf-8"))
        cur.execute(
            """
            INSERT INTO raw.opendart_disclosure_calls (corp_code, bgn_de, end_de, response)
            VALUES (%s, %s, %s, %s)
            """,
            (file_corp_code, bgn_de, end_de, json.dumps(response, ensure_ascii=False)),
        )
        n += 1
    print(f"raw.opendart_disclosure_calls: {n}건 insert")


def load_financial_calls(cur, corp_code: str | None = None) -> None:
    pattern = re.compile(r"^(\d+)_(\d{4})_(\d+)_(CFS|OFS)\.json$")
    n = 0
    for f in (DATA_DIR / "financials").glob("*.json"):
        m = pattern.match(f.name)
        if not m:
            continue
        file_corp_code, bsns_year, reprt_code, fs_div = m.groups()
        if corp_code and file_corp_code != corp_code:
            continue
        response = json.loads(f.read_text(encoding="utf-8"))
        cur.execute(
            """
            INSERT INTO raw.opendart_financial_calls
                (corp_code, bsns_year, reprt_code, fs_div, response)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (file_corp_code, bsns_year, reprt_code, fs_div, json.dumps(response, ensure_ascii=False)),
        )
        n += 1
    print(f"raw.opendart_financial_calls: {n}건 insert")


def load_document_files(cur) -> None:
    n = 0
    for f in (DATA_DIR / "documents").glob("*.zip"):
        rcept_no = f.stem
        cur.execute(
            """
            INSERT INTO raw.opendart_document_files (rcept_no, file_path, byte_size)
            VALUES (%s, %s, %s)
            ON CONFLICT (rcept_no) DO UPDATE SET
                file_path = EXCLUDED.file_path, byte_size = EXCLUDED.byte_size
            """,
            (rcept_no, str(f.relative_to(DATA_DIR.parent.parent)), f.stat().st_size),
        )
        n += 1
    print(f"raw.opendart_document_files: {n}건 upsert")


def load_ecos_calls(cur) -> None:
    pattern = re.compile(r"^base_rate_([A-Z])_(\d{8})_(\d{8})\.json$")
    n = 0
    for f in (DATA_DIR / "ecos").glob("*.json"):
        m = pattern.match(f.name)
        if not m:
            continue
        cycle, start_date, end_date = m.groups()
        response = json.loads(f.read_text(encoding="utf-8"))
        cur.execute(
            """
            INSERT INTO raw.ecos_rate_calls (stat_code, item_code, cycle, start_date, end_date, response)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            ("722Y001", "0101000", cycle, start_date, end_date, json.dumps(response, ensure_ascii=False)),
        )
        n += 1
    print(f"raw.ecos_rate_calls: {n}건 insert")


def main() -> None:
    conn = db.get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                load_corp_code(cur)
                load_disclosure_calls(cur)
                load_financial_calls(cur)
                load_document_files(cur)
                load_ecos_calls(cur)
        print("OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
