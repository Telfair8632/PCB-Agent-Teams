
# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
from pathlib import Path

from scripts.render_order_guide import render_all

SAMPLE_CONTEXT = {
    "project": "test_project",
    "generated_at": "2026-05-07 14:30 JST",
    "release_id": "rel_20260507_143012",
    "board": {
        "thickness_mm": 1.6, "layers": 4, "size_mm": "80 × 60 mm",
        "n_components": 24, "n_smt": 18, "n_tht": 6,
    },
    "coverage": {
        "n_unique_mpn": 17,
        "data_source": "all-lane",
        "single_vendor_coverage": {
            "digikey_jp": "15/17", "mouser_jp": "17/17", "lcsc": "12/17",
        },
        "recommended_paths": ["mouser_jp"],
        "matrix": [
            {"mpn": "AMC1311BDWVR", "qty": 1, "refs": "U1",
             "digikey_jp": {"active": True, "stock": 12756, "price": 823.2,
                            "currency": "JPY", "price_jpy": 823.2,
                            "url": "...", "is_primary": True},
             "mouser_jp":  {"active": True, "stock": 187, "price": 823.2,
                            "currency": "JPY", "price_jpy": 823.2,
                            "url": "...", "is_primary": False},
             "lcsc":       {"active": True, "stock": 5221, "price": 1.06,
                            "currency": "CNY", "price_jpy": None,
                            "url": "...", "is_primary": False}},
        ],
    },
    "user_intent": {
        "channel": "auto_cheapest",
        "brand": "any",
        "price_vs_stock": "balanced",
        "blacklist_mpns": [],
        "recommended_path": "auto",
        "asked_at": "2026-05-07T14:25:00+00:00",
    },
    "gate": {"status": "PASS", "timestamp": "2026-05-07 13:55 JST"},
}


def test_render_all_writes_three_files(tmp_path):
    paths = render_all(SAMPLE_CONTEXT, tmp_path)

    assert (tmp_path / "ORDER_GUIDE.md").exists()
    assert (tmp_path / "coverage_matrix.md").exists()
    assert (tmp_path / "fab_options.md").exists()
    assert set(paths.keys()) == {"ORDER_GUIDE.md", "coverage_matrix.md", "fab_options.md"}


def test_order_guide_substitutes_project_name(tmp_path):
    render_all(SAMPLE_CONTEXT, tmp_path)
    text = (tmp_path / "ORDER_GUIDE.md").read_text(encoding="utf-8")
    assert "test_project" in text
    assert "rel_20260507_143012" in text
    assert "板厚 / 层数 | 1.6 mm / 4 layers" in text


def test_order_guide_marks_recommended_path(tmp_path):
    render_all(SAMPLE_CONTEXT, tmp_path)
    text = (tmp_path / "ORDER_GUIDE.md").read_text(encoding="utf-8")
    assert "Mouser JP（覆盖率 17/17） ✅ 推荐" in text
    assert "DigiKey JP（覆盖率 15/17）" in text
    assert "DigiKey JP（覆盖率 15/17） ✅ 推荐" not in text


def test_coverage_matrix_lists_mpn_row(tmp_path):
    render_all(SAMPLE_CONTEXT, tmp_path)
    text = (tmp_path / "coverage_matrix.md").read_text(encoding="utf-8")
    assert "`AMC1311BDWVR`" in text
    assert "12756" in text
    assert "¥823.2" in text
    # all-lane data source banner
    assert "全 lane 实测" in text
    # primary marker on the winning lane
    assert "★" in text
    # LCSC priced in CNY (no JPY conversion)
    assert "CNY" in text


def test_coverage_matrix_primary_only_banner(tmp_path):
    """Legacy projects without _artifacts/ should get the warning banner."""
    ctx = {**SAMPLE_CONTEXT, "coverage": {**SAMPLE_CONTEXT["coverage"],
                                          "data_source": "primary-only"}}
    render_all(ctx, tmp_path)
    text = (tmp_path / "coverage_matrix.md").read_text(encoding="utf-8")
    assert "primary winner" in text
    assert "artifact shortlist 缺失" in text


def test_fab_options_shows_lcsc_coverage(tmp_path):
    render_all(SAMPLE_CONTEXT, tmp_path)
    text = (tmp_path / "fab_options.md").read_text(encoding="utf-8")
    assert "LCSC 覆盖率 12/17" in text


def test_user_intent_lcsc_marks_path_a(tmp_path):
    """Channel preference=lcsc_jlcpcb → ORDER_GUIDE 把 Path A 标 ★."""
    ctx = {**SAMPLE_CONTEXT, "user_intent": {**SAMPLE_CONTEXT["user_intent"],
                                             "channel": "lcsc_jlcpcb",
                                             "recommended_path": "lcsc"}}
    render_all(ctx, tmp_path)
    text = (tmp_path / "ORDER_GUIDE.md").read_text(encoding="utf-8")
    assert "★ Path A" in text or "★ 你的首选" in text  # appears at Path A header
    # Path A header should carry the star
    assert "Path A：JLCPCB 一站式" in text
    a_idx = text.find("Path A：JLCPCB 一站式")
    a_line_end = text.find("\n", a_idx)
    assert "★" in text[a_idx:a_line_end]


def test_user_intent_jp_domestic_marks_dk_mouser(tmp_path):
    """Channel preference=jp_domestic_fast → C-1 (DigiKey JP) and C-2 (Mouser JP) 都标 ★."""
    ctx = {**SAMPLE_CONTEXT, "user_intent": {**SAMPLE_CONTEXT["user_intent"],
                                             "channel": "jp_domestic_fast",
                                             "recommended_path": "jp_domestic"}}
    render_all(ctx, tmp_path)
    text = (tmp_path / "ORDER_GUIDE.md").read_text(encoding="utf-8")
    # C-1 line carries star
    c1_idx = text.find("C-1：DigiKey JP")
    c1_line_end = text.find("\n", c1_idx)
    assert "★" in text[c1_idx:c1_line_end]
    # C-2 line carries star
    c2_idx = text.find("C-2：Mouser JP")
    c2_line_end = text.find("\n", c2_idx)
    assert "★" in text[c2_idx:c2_line_end]


def test_user_intent_auto_falls_back_to_coverage_recommended(tmp_path):
    """Channel preference=auto_cheapest → 不强加 ★，依然写 coverage_scan recommended."""
    render_all(SAMPLE_CONTEXT, tmp_path)
    text = (tmp_path / "ORDER_GUIDE.md").read_text(encoding="utf-8")
    # SAMPLE_CONTEXT has Mouser as the only fully-covered vendor
    assert "Mouser JP（覆盖率 17/17） ✅ 推荐" in text


def test_user_intent_renders_preference_table(tmp_path):
    render_all(SAMPLE_CONTEXT, tmp_path)
    text = (tmp_path / "ORDER_GUIDE.md").read_text(encoding="utf-8")
    assert "用户下单偏好" in text
    assert "balanced" in text
    assert "any" in text
