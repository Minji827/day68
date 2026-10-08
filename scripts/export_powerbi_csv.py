"""powerbi_db의 dim_*/fact_* 11개 테이블을 UTF-8(BOM) CSV로 내보내고 zip으로 묶는다.
팀 전달 규칙(날짜 YYYY-MM-DD, 결측은 빈칸, 숫자는 숫자만, UTF-8 BOM)을 그대로 반영.

코드 컬럼(rcept_no/corp_code/stock_code) 관련 주의: CSV 자체는 타입 정보가 없어서
Power BI Query Editor에서 이 컬럼들을 "텍스트"로 지정해야 앞자리 0이 안 사라진다.
Excel에 직접 더블클릭으로 열면 자동으로 숫자로 추측해서 0이 사라지거나 지수 표기로
보일 수 있는데, 이건 Excel이 파일을 "표시"할 때 추측하는 동작이라 파일 내용 자체는
멀쩡하다. (=\"00607496\" 같은 Excel 수식 트릭은 일부러 안 씀 - 그건 Excel에서 열 때만
통하고 Power BI Power Query는 CSV 안의 수식을 평가하지 않아서, 오히려 Power BI 쪽
값에 ="..." 글자가 그대로 섞여 들어가 버린다. Power BI가 최종 소비자라 이쪽을 깨뜨리지
않는 쪽을 택함.)

Usage:
    python scripts\export_powerbi_csv.py
"""
from __future__ import annotations

import csv
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db

TABLES = [
    "dim_company", "dim_date", "dim_rule", "dim_account",
    "fact_disclosure", "fact_financial_change", "fact_rule_hit",
    "fact_priority", "fact_disclosure_diff", "fact_explanation", "fact_base_rate",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "powerbi_export"
ZIP_PATH = OUT_DIR.parent / "powerbi_export.zip"


def _format_value(v) -> str:
    if v is None:
        return ""  # 결측은 빈칸 (0으로 채우지 않음, PRD 결측 비추정 원칙)
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, date):
        return v.isoformat()  # YYYY-MM-DD
    if isinstance(v, Decimal):
        return format(v, "f")  # 지수표기 없이 숫자 그대로, 천단위 구분자 없이
    return str(v)


def export_table(cur, table: str, out_dir: Path) -> int:
    cur.execute(f"SELECT * FROM {table} ORDER BY 1")
    cols = [c.name for c in cur.description]
    rows = cur.fetchall()
    path = out_dir / f"{table}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for row in rows:
            writer.writerow([_format_value(v) for v in row])
    return len(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = db.get_powerbi_conn()
    try:
        with conn.cursor() as cur:
            for table in TABLES:
                n = export_table(cur, table, OUT_DIR)
                print(f"{table}: {n}행 -> {OUT_DIR / (table + '.csv')}")
    finally:
        conn.close()

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in TABLES:
            zf.write(OUT_DIR / f"{table}.csv", arcname=f"{table}.csv")
    print(f"\nzip: {ZIP_PATH} ({ZIP_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
