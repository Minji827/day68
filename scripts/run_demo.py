"""원클릭 데모 러너: Postgres 기동 대기 -> 스키마 적용 -> 수집 -> 적재 -> 규칙 실행 -> 결과 출력.
run_demo.bat에서 호출. 각 단계는 별도 프로세스로 실행해서 (subprocess 리스트 인자 사용,
쉘 이스케이핑/인코딩 문제 없음) 실패하면 즉시 중단한다.

기업 지정은 종목코드가 기본값이다 (모호함 없음 - 회사명 부분일치는 동명/계열사가 많아
엉뚱한 기업을 고를 수 있어서 collect.py가 후보 여럿이면 추측 없이 멈춘다).

Usage:
    python scripts/run_demo.py [종목코드|회사명] [review_date]
    (기본값: 005930(삼성전자), 검토 기준일은 실시간(오늘). 6자리 숫자면 종목코드로, 아니면 회사명으로 처리)
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
load_dotenv(ROOT / ".env")


def run(step: str, args: list[str], allow_fail: bool = False) -> None:
    print(f"\n--- {step} ---")
    result = subprocess.run([PY, *args], cwd=ROOT)
    if result.returncode != 0:
        print(f"\n[실패] {step} (exit {result.returncode})")
        if not allow_fail:
            sys.exit(result.returncode)


def start_postgres() -> None:
    print("--- PostgreSQL 기동 (Docker Desktop, localhost:5433) ---")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "005930"
    review_date = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
    is_stock_code = target.isdigit() and len(target) == 6
    corp_flag = ["--stock-code", target] if is_stock_code else ["--corp", target]

    start_postgres()
    run("DB 연결 대기", ["scripts/wait_for_db.py"])
    run("스키마 적용", ["scripts/init_db.py"])
    run(
        f"데이터 수집: {target}",
        ["scripts/collect.py", *corp_flag, "--bgn-de", "20250101", "--end-de", "20261007"],
    )
    run("RAW -> CORE: raw 적재", ["scripts/load_raw.py"])
    run("RAW -> CORE: core 변환", ["scripts/load_core.py"])
    run("변화 탐지 규칙 실행", ["scripts/run_rules.py", "--review-date", review_date])
    run("결과 출력", ["scripts/show_results.py"])
    run("정정 전후 비교 (AI 없이, 원문 diff)", ["scripts/compare_disclosure.py", *corp_flag, "--review-date", review_date])

    if os.environ.get("OPENAI_API_KEY"):
        run(
            "AI 해설서 생성 (OpenAI, 근거 검증 포함)",
            ["scripts/explain.py", *corp_flag, "--review-date", review_date, "--limit-per-company", "3"],
            allow_fail=True,
        )
    else:
        print("\n--- AI 해설서 생성 건너뜀 (.env에 OPENAI_API_KEY 없음) ---")

    print("\n=== 완료 ===")


if __name__ == "__main__":
    main()
