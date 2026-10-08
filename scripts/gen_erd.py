"""DeltaWatch AI DB 스키마 ERD를 이미지로 그려서 저장한다 (raw/core/mart 3-column 레이아웃)."""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib.font_manager import FontProperties

# ---- 팔레트 (dataviz 스킬 참조 팔레트와 동일 계열) ----
BG = "#f9f9f7"
INK = "#0b0b0b"
SUB_INK = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"

SCHEMA_COLOR = {
    "raw": {"header": "#898781", "header_text": "#ffffff", "body": "#fcfcfb", "border": "#c3c2b7"},
    "core": {"header": "#2a78d6", "header_text": "#ffffff", "body": "#eef5fc", "border": "#2a78d6"},
    "mart": {"header": "#1baf7a", "header_text": "#ffffff", "body": "#eaf8f2", "border": "#1baf7a"},
}

KFONT = "Malgun Gothic"
plt.rcParams["font.family"] = KFONT
plt.rcParams["axes.unicode_minus"] = False

TABLES = {
    "raw": [
        ("opendart_corp_code", ["corp_code PK", "corp_name", "stock_code", "modify_date"]),
        ("opendart_disclosure_calls", ["call_id PK", "corp_code", "bgn_de / end_de", "response (jsonb)"]),
        ("opendart_financial_calls", ["call_id PK", "corp_code", "bsns_year / reprt_code / fs_div", "response (jsonb)"]),
        ("opendart_document_files", ["rcept_no PK", "file_path", "byte_size"]),
        ("ecos_rate_calls", ["call_id PK", "stat_code / item_code", "cycle", "response (jsonb)"]),
    ],
    "core": [
        ("companies", ["corp_code PK", "corp_name", "stock_code", "corp_cls"]),
        ("disclosures", ["rcept_no PK", "corp_code FK -> companies", "orig_rcept_no FK -> self",
                          "report_nm_clean", "is_correction", "match_method", "rcept_dt / flr_nm"]),
        ("disclosure_sections", ["rcept_no FK -> disclosures  PK", "section_no PK", "section_title", "section_text"]),
        ("account_mapping", ["account_nm_raw PK", "account_std_code PK", "account_std_name", "agg_method"]),
        ("financial_accounts", ["corp_code FK -> companies  PK", "bsns_year / reprt_code / fs_div PK",
                                 "account_std_code PK", "thstrm_amount / frmtrm_amount"]),
        ("rate_observations", ["stat_code PK", "item_code PK", "obs_date PK", "value / unit"]),
        ("rule_catalog", ["rule_id PK", "rule_type", "description", "weight"]),
    ],
    "mart": [
        ("company_priority", ["corp_code PK", "review_date PK", "priority_level / score",
                               "financial_rule_count", "disclosure_rule_count"]),
        ("change_events", ["event_id PK", "corp_code / review_date", "change_type / rule_id / weight",
                            "old_value / new_value", "description"]),
        ("explanation_sentences", ["rcept_no PK", "sentence_no PK", "sentence_text",
                                    "evidence_rcept_no / section / excerpt"]),
        ("kpi_daily", ["review_date PK", "companies_changed", "companies_high_priority",
                        "new_disclosures / corrections"]),
        ("rate_context", ["review_date PK", "base_rate / prior_base_rate", "rate_direction"]),
    ],
}

COL_X = {"raw": 0.5, "core": 7.3, "mart": 14.4}
COL_W = {"raw": 5.6, "core": 6.0, "mart": 5.3}
ROW_H = 0.34
PAD = 0.28
GAP = 0.75
HEADER_BLOCK = 2.25  # title + schema labels + top margin before first table
FOOTER_BLOCK = 1.2  # legend + bottom margin

FIG_W = 21.0


def table_height(cols: list[str]) -> float:
    return PAD * 2 + 0.42 + len(cols) * ROW_H


def column_content_height(schema: str) -> float:
    total = 0.0
    for _, cols in TABLES[schema]:
        total += table_height(cols) + GAP
    return total - GAP  # no trailing gap after the last table


def draw_table(ax, schema: str, x: float, y_top: float, name: str, cols: list[str]) -> float:
    c = SCHEMA_COLOR[schema]
    w = COL_W[schema]
    h = table_height(cols)
    y_bot = y_top - h

    box = FancyBboxPatch(
        (x, y_bot), w, h,
        boxstyle="round,pad=0,rounding_size=0.09",
        linewidth=1.1, edgecolor=c["border"], facecolor=c["body"], zorder=2,
    )
    ax.add_patch(box)

    header_h = 0.42
    header = FancyBboxPatch(
        (x, y_top - header_h), w, header_h,
        boxstyle="round,pad=0,rounding_size=0.09",
        linewidth=0, facecolor=c["header"], zorder=3,
    )
    ax.add_patch(header)
    ax.add_patch(Rectangle((x, y_top - header_h), w, header_h / 2, facecolor=c["header"], edgecolor="none", zorder=3))

    ax.text(x + 0.18, y_top - header_h / 2, f"{schema}.{name}", fontsize=11.5, fontweight="bold",
            color=c["header_text"], va="center", ha="left", zorder=4)

    for i, col in enumerate(cols):
        cy = y_top - header_h - PAD - i * ROW_H - ROW_H / 2 + 0.05
        is_pk = "PK" in col
        is_fk = "FK" in col
        weight = "bold" if is_pk else "normal"
        color = INK if is_pk else (SUB_INK if not is_fk else "#184f95")
        ax.text(x + 0.22, cy, col, fontsize=9.3, color=color, weight=weight, va="center", ha="left", zorder=4,
                style="italic" if is_fk else "normal")

    return y_bot


def main() -> None:
    max_content_h = max(column_content_height(s) for s in ["raw", "core", "mart"])
    FIG_H = HEADER_BLOCK + max_content_h + FOOTER_BLOCK

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=160)
    ax.set_xlim(0, 20.3)
    ax.set_ylim(0, FIG_H)
    ax.axis("off")
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    ax.text(0.5, FIG_H - 0.75, "DeltaWatch AI — Database ERD", fontsize=24, fontweight="bold", color=INK, va="top")
    ax.text(0.5, FIG_H - 1.25, "OpenDART + ECOS  ->  PostgreSQL RAW -> CORE -> MART  ->  SQL 변화 탐지 / OpenAI 해설", fontsize=11.5, color=MUTED, va="top")

    for schema, color in [("raw", SCHEMA_COLOR["raw"]["header"]), ("core", SCHEMA_COLOR["core"]["header"]), ("mart", SCHEMA_COLOR["mart"]["header"])]:
        x = COL_X[schema]
        ax.text(x, FIG_H - 1.75, schema.upper(), fontsize=13, fontweight="bold", color=color, va="top")

    top_y = FIG_H - 2.25
    positions: dict[str, dict[str, tuple[float, float, float]]] = {"raw": {}, "core": {}, "mart": {}}
    for schema in ["raw", "core", "mart"]:
        y = top_y
        for name, cols in TABLES[schema]:
            x = COL_X[schema]
            y_bot = draw_table(ax, schema, x, y, name, cols)
            positions[schema][name] = (x, y, y_bot)
            y = y_bot - GAP

    # ---- FK relationship lines inside CORE ----
    def anchor_right(schema, name):
        x, yt, yb = positions[schema][name]
        return (x + COL_W[schema], (yt + yb) / 2)

    def anchor_left(schema, name):
        x, yt, yb = positions[schema][name]
        return (x, (yt + yb) / 2)

    def anchor_top(schema, name):
        x, yt, yb = positions[schema][name]
        return (x + COL_W[schema] * 0.5, yt)

    def anchor_bottom(schema, name):
        x, yt, yb = positions[schema][name]
        return (x + COL_W[schema] * 0.5, yb)

    fk_color = "#184f95"
    # 인접한 테이블끼리는 위/아래로 바로 연결
    straight_edges = [
        ("companies", "disclosures"),
        ("disclosures", "disclosure_sections"),
    ]
    for t1, t2 in straight_edges:
        p1 = anchor_bottom("core", t1)
        p2 = anchor_top("core", t2)
        arrow = FancyArrowPatch(p1, p2, connectionstyle="arc3,rad=0.0", arrowstyle="-|>", mutation_scale=14,
                                 linewidth=1.4, color=fk_color, zorder=1, shrinkA=2, shrinkB=2)
        ax.add_patch(arrow)

    # companies -> financial_accounts: 사이 테이블들을 관통하지 않도록 RAW/CORE 사이 여백
    # 안에서만 살짝 우회 (rad는 두 점 사이 거리에 비례해서 휘므로 작게 잡아야 함)
    p1 = anchor_left("core", "companies")
    p2 = anchor_left("core", "financial_accounts")
    bow = FancyArrowPatch(p1, p2, connectionstyle="arc3,rad=0.045", arrowstyle="-|>", mutation_scale=14,
                           linewidth=1.3, color=fk_color, zorder=1, shrinkA=3, shrinkB=3, linestyle=(0, (4, 2)))
    ax.add_patch(bow)
    ax.text(p1[0] - 0.18, p1[1] - 0.35, "FK corp_code", fontsize=7.6, color=fk_color,
            ha="right", va="center", style="italic", rotation=90)

    # self reference: disclosures.orig_rcept_no -> disclosures.rcept_no
    x, yt, yb = positions["core"]["disclosures"]
    loop = FancyArrowPatch((x, (yt + yb) / 2 - 0.3), (x, (yt + yb) / 2 + 0.3),
                            connectionstyle="arc3,rad=-0.9", arrowstyle="-|>", mutation_scale=12,
                            linewidth=1.2, color=fk_color, zorder=1)
    ax.add_patch(loop)
    ax.text(x - 1.55, (yt + yb) / 2, "self FK\n(orig_rcept_no)", fontsize=7.6, color=fk_color, ha="center", va="center", style="italic")

    # ---- pipeline flow arrows between schema columns ----
    raw_mid_y = (positions["raw"]["opendart_disclosure_calls"][1] + positions["raw"]["ecos_rate_calls"][2]) / 2
    core_mid_y = (positions["core"]["companies"][1] + positions["core"]["rule_catalog"][2]) / 2
    mart_mid_y = (positions["mart"]["company_priority"][1] + positions["mart"]["rate_context"][2]) / 2

    for (sx, sy), (ex, ey), label in [
        ((COL_X["raw"] + COL_W["raw"] + 0.1, raw_mid_y), (COL_X["core"] - 0.65, core_mid_y), "load_raw.py\n->\nload_core.py"),
        ((COL_X["core"] + COL_W["core"] + 0.1, core_mid_y), (COL_X["mart"] - 0.1, mart_mid_y), "run_rules.py\n(F1-F5 / D1-D5)"),
    ]:
        arrow = FancyArrowPatch((sx, sy), (ex, ey), arrowstyle="-|>", mutation_scale=24,
                                 linewidth=2.6, color="#eb6834", zorder=1)
        ax.add_patch(arrow)
        ax.text((sx + ex) / 2, sy + 0.55, label, fontsize=9, color="#9a4a1f", ha="center", va="bottom", fontweight="bold")

    # legend
    ly = 0.65
    ax.add_patch(Rectangle((0.5, ly), 0.3, 0.2, facecolor=INK))
    ax.text(0.9, ly + 0.1, "PK (볼드)", fontsize=9, color=INK, va="center")
    ax.text(3.3, ly + 0.1, "FK (기울임, 파란색)", fontsize=9, color=fk_color, style="italic", va="center")
    ax.text(6.7, ly + 0.1, "굵은 화살표 = ETL 파이프라인 흐름 (schema 간)", fontsize=9, color="#9a4a1f", va="center")
    ax.text(14.0, ly + 0.1, "가는 화살표 = FK 관계 (CORE 내부)", fontsize=9, color=fk_color, va="center")

    out = Path(r"C:\Users\redsu\OneDrive\바탕 화면\erd.jpg")
    fig.savefig(out, facecolor=BG, bbox_inches="tight", pad_inches=0.3)
    print("saved:", out)


if __name__ == "__main__":
    main()
