"""PostgreSQL이 연결 가능해질 때까지 재시도 (run_demo.bat에서 사용)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db


def main() -> int:
    for i in range(30):
        try:
            conn = db.get_conn()
            conn.close()
            print("DB 연결 성공")
            return 0
        except Exception as e:
            print(f"  대기 중... ({i + 1}/30) {type(e).__name__}")
            time.sleep(2)
    print("DB 연결 실패 (60초 초과) - WSL/Docker 상태를 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
