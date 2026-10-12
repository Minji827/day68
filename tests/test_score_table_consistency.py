"""점수표가 세 군데에 손으로 복사돼 있는 걸 테스트로 묶어둔다.

같은 숫자가 사는 곳:
  1. src/rules/rule_engine_v40.py  THRESHOLDS      (순수함수 엔진 - 기존 테스트 대상)
  2. sql/seeds/002_rule_catalog.sql base_score     (DB 시드)
  3. scripts/run_rules.py          D5_BASE / 등급 경계 (실제 운영 엔진)

(1)과 (2)는 주석으로 "맞춰 놨다"고만 적혀 있었고, (3)은 테스트가 아예 없었다.
tests/test_rule_engine_v33.py와 test_rule_engine_v40.py는 (1)만 검증하는데 화면에
나오는 숫자를 만드는 건 (3)이다 - 둘이 갈라지면 테스트는 전부 통과하면서 결과만
틀린다. 여기서 세 쪽이 같은 숫자인지 직접 대조한다.

DB도 네트워크도 안 쓴다 - SQL 시드 파일을 텍스트로 파싱한다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pytest

from src.rules.rule_engine_v40 import THRESHOLDS
from src.rules.rule_engine_v40 import _grade_from_score as pure_grade

SEED_SQL = ROOT / "sql" / "seeds" / "002_rule_catalog.sql"


def _parse_catalog_seed() -> dict[str, dict]:
    """시드 INSERT에서 rule_id -> {base_score, is_emergency_rule}를 뽑는다.

    각 행은 아래 순서다(컬럼 목록은 sql/seeds/002_rule_catalog.sql 참고):
        (rule_id, rule_type, description, base_score, domain, is_emergency_rule, ...)

    description 안에 쉼표·괄호·줄바꿈이 섞여 있어 단순 split이 안 된다. 또 뒤쪽
    is_team_assumption도 true/false라서 "행 어딘가의 true"를 찾으면 안 된다 -
    description 리터럴의 끝을 정확히 찾아 **자리로** 읽는다.
    """
    sql = SEED_SQL.read_text(encoding="utf-8")
    insert = sql[sql.index("INSERT INTO core.rule_catalog"):]

    out: dict[str, dict] = {}
    for m in re.finditer(r"^\s*\('([A-Z0-9]+)',\s*'(?:financial|disclosure|context)',\s*",
                         insert, re.MULTILINE):
        rule_id = m.group(1)
        rest = insert[m.end():]
        # description 리터럴을 건너뛴다. ''(SQL 이스케이프된 따옴표)는 종료가 아니다.
        assert rest[0] == "'", f"{rule_id}: description이 따옴표로 시작하지 않음"
        i = 1
        while i < len(rest):
            if rest[i] == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        after = rest[i + 1:]
        # , base_score , domain , is_emergency_rule
        f = re.match(r"\s*,\s*(\d+)\s*,\s*(?:'[^']*'|NULL)\s*,\s*(true|false)\s*,", after)
        assert f, f"{rule_id}: base_score/domain/is_emergency_rule를 못 읽음 -> {after[:60]!r}"
        out[rule_id] = {"base_score": int(f.group(1)), "is_emergency_rule": f.group(2) == "true"}
    return out


SEED = _parse_catalog_seed()
CATALOG = {k: v["base_score"] for k, v in SEED.items()}


def test_seed_parsed_every_rule():
    """파서가 조용히 0건을 반환하면 아래 테스트가 전부 공허하게 통과한다."""
    assert len(CATALOG) >= 20, f"시드에서 룰을 {len(CATALOG)}개밖에 못 읽음"
    for rule_id in ("F1", "F8", "D1", "D5", "D10", "CTX1"):
        assert rule_id in CATALOG, f"{rule_id}를 시드에서 못 찾음"


@pytest.mark.parametrize("rule_id", sorted(k for k in THRESHOLDS if not k.startswith("D5_")))
def test_threshold_base_matches_catalog(rule_id):
    """순수함수 엔진의 BASE == DB 시드의 base_score."""
    assert rule_id in CATALOG, f"{rule_id}가 rule_catalog 시드에 없다"
    assert THRESHOLDS[rule_id]["BASE"] == CATALOG[rule_id], (
        f"{rule_id}: THRESHOLDS={THRESHOLDS[rule_id]['BASE']} vs "
        f"시드 base_score={CATALOG[rule_id]}"
    )


def test_d5_base_scores_match_pure_engine():
    """run_rules.D5_BASE == THRESHOLDS["D5_*"]["BASE"].

    D5는 rule_catalog의 단일 base_score를 안 쓰고 4단계 유형별 점수를 쓴다
    (classify_d5). 그 표가 run_rules.py에 따로 하드코딩돼 있다.
    """
    import run_rules

    for kind, score in run_rules.D5_BASE.items():
        key = f"D5_{kind}"
        assert key in THRESHOLDS, f"THRESHOLDS에 {key}가 없다"
        assert THRESHOLDS[key]["BASE"] == score, (
            f"{key}: THRESHOLDS={THRESHOLDS[key]['BASE']} vs run_rules.D5_BASE={score}"
        )
    assert set(run_rules.D5_BASE) == {"TYPO", "MINOR_CHANGE", "MAJOR_AMOUNT", "CORE_FINANCIAL"}


@pytest.mark.parametrize("score", range(0, 12))
def test_grade_boundaries_match_across_engines(score):
    """등급 경계(8/4)가 순수함수 엔진·운영 엔진·SQL CASE 식에서 모두 같은지."""
    import run_rules

    expected = pure_grade(score)
    assert run_rules._grade_from_score(score) == expected, (
        f"score={score}: run_rules={run_rules._grade_from_score(score)} vs 순수엔진={expected}"
    )

    # SQL로 내려가는 CASE 식도 같은 경계인지 - 문자열을 파이썬에서 재현해 비교한다
    # (DB 없이 검증하려면 이 방법뿐이고, 숫자가 바뀌면 여기서 걸린다).
    case_sql = run_rules._GRADE_CASE_SQL.format(score=str(score))
    m = re.match(
        r"CASE WHEN (\d+) >= (\d+) THEN '(\S+)' WHEN \1 >= (\d+) THEN '(\S+)' ELSE '(\S+)' END",
        case_sql,
    )
    assert m, f"_GRADE_CASE_SQL 모양이 바뀌었다: {case_sql}"
    hi_cut, hi, mid_cut, mid, low = int(m.group(2)), m.group(3), int(m.group(4)), m.group(5), m.group(6)
    sql_grade = hi if score >= hi_cut else mid if score >= mid_cut else low
    assert sql_grade == expected, f"score={score}: SQL CASE={sql_grade} vs 순수엔진={expected}"


def test_emergency_rules_match_catalog():
    """"등장 자체가 항상 긴급확인"인 룰 집합이 시드와 순수엔진에서 같은지.

    시드의 is_emergency_rule=true인 룰은 BASE가 10(최고점)이어야 한다 - 등급 경계상
    8 이상이면 '높음'이지만, 긴급확인 룰을 9점 이하로 두면 같은 '높음' 안에서 일반
    룰에 밀릴 수 있다. D9(8점)는 긴급확인 룰이 아니므로 제외 대상이 맞다.
    """
    emergency = {k for k, v in SEED.items() if v["is_emergency_rule"]}
    assert emergency == {"F8", "D3", "D7", "D8"}, f"긴급확인 룰 집합이 바뀌었다: {sorted(emergency)}"
    for rule_id in emergency:
        assert THRESHOLDS[rule_id]["BASE"] == 10, (
            f"{rule_id}는 긴급확인 룰인데 BASE={THRESHOLDS[rule_id]['BASE']} (10이어야 함)"
        )
