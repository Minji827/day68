"""원클릭 데모 러너: Postgres 기동 대기 -> 스키마 적용 -> 수집 -> 적재 -> 규칙 실행 -> 결과 출력.
run_demo.bat에서 호출. 각 단계는 별도 프로세스로 실행해서 (subprocess 리스트 인자 사용,
쉘 이스케이핑/인코딩 문제 없음) 실패하면 즉시 중단한다.

Usage:
    python scripts/run_demo.py [corp_name] [review_date]
    (기본값: 삼성전자, 2026-01-01)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def run(step: str, args: list[str]) -> None:
    print(f"\n--- {step} ---")
    result = subprocess.run([PY, *args], cwd=ROOT)
    if result.returncode != 0:
        print(f"\n[실패] {step} (exit {result.returncode})")
        sys.exit(result.returncode)


def start_postgres() -> None:
    print("--- PostgreSQL 기동 (Docker Desktop, localhost:5433) ---")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT)


def main() -> None:
    corp = sys.argv[1] if len(sys.argv) > 1 else "삼성전자"
    review_date = sys.argv[2] if len(sys.argv) > 2 else "2026-01-01"

    start_postgres()
    run("DB 연결 대기", ["scripts/wait_for_db.py"])
    run("스키마 적용", ["scripts/init_db.py"])
    run(f"데이터 수집: {corp}", ["scripts/collect.py", "--corp", corp, "--bgn-de", "20250101", "--end-de", "20261007"])
    run("RAW -> CORE: raw 적재", ["scripts/load_raw.py"])
    run("RAW -> CORE: core 변환", ["scripts/load_core.py"])
    run("변화 탐지 규칙 실행", ["scripts/run_rules.py", "--review-date", review_date])
    run("결과 출력", ["scripts/show_results.py"])

    print("\n=== 완료 ===")


if __name__ == "__main__":
    main()
