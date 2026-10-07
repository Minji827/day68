"""mart 스키마 결과를 보기 좋게 출력 (run_demo.bat 마지막 단계)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import psycopg2.extras


def main() -> None:
    conn = db.get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("\n=== 기업 재검토 우선순위 (mart.company_priority) ===")
    cur.execute("SELECT * FROM mart.company_priority ORDER BY priority_score DESC")
    rows = cur.fetchall()
    if not rows:
        print("(없음 - run_rules.py가 아직 안 돌았거나 변화가 없습니다)")
    for r in rows:
        print(
            f"  {r['corp_name']} ({r['corp_code']}) | {r['review_date']} | "
            f"{r['priority_level'].upper()} {r['priority_score']}점 | "
            f"재무규칙 {r['financial_rule_count']}건 / 공시규칙 {r['disclosure_rule_count']}건"
        )

    print("\n=== 변화 이벤트 상세 (mart.change_events) ===")
    cur.execute(
        "SELECT rule_id, corp_name, weight, description FROM mart.change_events "
        "ORDER BY corp_name, weight DESC, rule_id"
    )
    for r in cur.fetchall():
        print(f"  [{r['rule_id']}] (+{r['weight']}) {r['corp_name']}: {r['description']}")

    print("\n=== KPI (mart.kpi_daily) ===")
    cur.execute("SELECT * FROM mart.kpi_daily ORDER BY review_date DESC")
    for r in cur.fetchall():
        print(
            f"  {r['review_date']} | 변화기업 {r['companies_changed']} | "
            f"높음 {r['companies_high_priority']} | 신규공시 {r['new_disclosures']} | "
            f"정정공시 {r['corrections']}"
        )

    print("\n=== 기준금리 맥락 (mart.rate_context) ===")
    cur.execute("SELECT * FROM mart.rate_context ORDER BY review_date DESC")
    for r in cur.fetchall():
        print(f"  {r['review_date']} | {r['prior_base_rate']} -> {r['base_rate']} ({r['rate_direction']})")

    conn.close()


if __name__ == "__main__":
    main()
