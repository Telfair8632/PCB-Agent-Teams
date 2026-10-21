import json
from pathlib import Path

from scripts.coverage_scan import _is_locked_evidence_file, scan_coverage


def _write_evidence(
    dir: Path, mpn: str, vendor: str = "digikey_jp",
    active: bool = True, stock: int = 1000,
) -> None:
    payload = {
        "schema_version": "phase2.5_v1",
        "mpn": mpn,
        "designators": "U1",
        "qty_per_board": 1,
        "vendor": {
            "primary": vendor,
            "active": active,
            "stock": stock,
            "price_jpy": 100.0,
            "product_url": f"https://www.digikey.com/.../{mpn}",
        },
    }
    (dir / f"{mpn}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_scan_two_mpns_one_vendor(tmp_path):
    cs = tmp_path / "component_selecting"
    cs.mkdir()
    _write_evidence(cs, "AMC1311BDWVR")
    _write_evidence(cs, "SS14")

    result = scan_coverage(cs)

    assert result["n_unique_mpn"] == 2
    assert result["totals"]["digikey_jp"] == 2
    assert result["totals"]["mouser_jp"] == 0
    assert result["single_vendor_coverage"]["digikey_jp"] == "2/2"
    assert "digikey_jp" in result["recommended_paths"]


def test_scan_skips_longlist_files(tmp_path):
    cs = tmp_path / "component_selecting"
    cs.mkdir()
    _write_evidence(cs, "AMC1311BDWVR")
    (cs / "R1_iso_amp_longlist.json").write_text(json.dumps({"role": "iso_amp"}), encoding="utf-8")

    result = scan_coverage(cs)

    assert result["n_unique_mpn"] == 1
    assert all(row["mpn"] != "R1_iso_amp_longlist" for row in result["matrix"])


def test_scan_inactive_vendor_not_counted(tmp_path):
    cs = tmp_path / "component_selecting"
    cs.mkdir()
    _write_evidence(cs, "OLD_MPN", active=False)
    _write_evidence(cs, "NEW_MPN", active=True)

    result = scan_coverage(cs)

    assert result["totals"]["digikey_jp"] == 1
    assert result["recommended_paths"] == []


def test_scan_no_evidence_dir_returns_empty(tmp_path):
    result = scan_coverage(tmp_path / "does_not_exist")
    assert result["n_unique_mpn"] == 0
    assert result["matrix"] == []
    assert result["recommended_paths"] == []


def test_is_locked_evidence_filename_rules():
    assert _is_locked_evidence_file(Path("AMC1311BDWVR.json"))
    assert _is_locked_evidence_file(Path("SS14.json"))
    assert not _is_locked_evidence_file(Path("R1_iso_amp_longlist.json"))
    assert not _is_locked_evidence_file(Path("_pending_FOO.json"))
    assert not _is_locked_evidence_file(Path("foo.txt"))


def _write_artifact_shortlist(
    artifacts_dir: Path, role_stem: str, mpn: str, lanes: dict[str, dict],
) -> None:
    payload = {
        "schema": "component-selecting-JP/v1",
        "results": [{
            "mpn": mpn,
            "vendor_results": [
                {"vendor_id": vid, **info}
                for vid, info in lanes.items()
            ],
        }],
    }
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / f"{role_stem}_shortlist.json").write_text(json.dumps(payload), encoding="utf-8")


def test_all_lane_picks_real_coverage_not_just_primary(tmp_path):
    """Real fix for the LCSC=0/N false negative: when _artifacts/ shortlist
    has all 3 lanes active, coverage_matrix shows 3/3 not 1/3."""
    proj = tmp_path / "Projects" / "p"
    cs = proj / "datasheets" / "component_selecting"
    art = proj / "_artifacts" / "component_selecting"
    cs.mkdir(parents=True)

    # Locked evidence picks Mouser as primary
    _write_evidence(cs, "MPN1", vendor="mouser_jp")

    # Artifact shortlist: all 3 lanes active
    _write_artifact_shortlist(art, "role1", "MPN1", {
        "digikey_jp": {"status": "active", "stock": 100, "price": 50.0, "currency": "JPY",
                       "fetched_at": "2026-05-07T10:00:00+00:00"},
        "mouser_jp":  {"status": "active", "stock": 200, "price": 48.0, "currency": "JPY",
                       "fetched_at": "2026-05-07T10:00:00+00:00"},
        "lcsc":       {"status": "active", "stock": 1000, "price": 0.5, "currency": "CNY",
                       "fetched_at": "2026-05-07T10:00:00+00:00"},
    })

    result = scan_coverage(cs)

    assert result["data_source"] == "all-lane"
    # All 3 lanes active → all 3 count toward coverage
    assert result["totals"] == {"digikey_jp": 1, "mouser_jp": 1, "lcsc": 1}
    assert result["single_vendor_coverage"]["lcsc"] == "1/1"
    # Primary marker preserved
    row = result["matrix"][0]
    assert row["mouser_jp"]["is_primary"] is True
    assert row["digikey_jp"]["is_primary"] is False
    assert row["lcsc"]["is_primary"] is False
    # LCSC row keeps native CNY price
    assert row["lcsc"]["currency"] == "CNY"
    assert row["lcsc"]["price_jpy"] is None  # CNY → no auto-conversion


def test_all_lane_inactive_lane_gets_probed_note(tmp_path):
    proj = tmp_path / "Projects" / "p"
    cs = proj / "datasheets" / "component_selecting"
    art = proj / "_artifacts" / "component_selecting"
    cs.mkdir(parents=True)

    _write_evidence(cs, "MPN1", vendor="mouser_jp")
    _write_artifact_shortlist(art, "role1", "MPN1", {
        "digikey_jp": {"status": "active", "stock": 50, "price": 100.0, "currency": "JPY"},
        "mouser_jp":  {"status": "active", "stock": 30, "price": 95.0,  "currency": "JPY"},
        "lcsc":       {"status": "inactive", "reason": "exact_mpn_not_found_in_lcsc"},
    })

    result = scan_coverage(cs)

    assert result["totals"]["lcsc"] == 0
    row = result["matrix"][0]
    # Distinct from "no evidence" — explicitly says probed
    assert "probed but inactive" in row["lcsc"]["note"]
    assert "exact_mpn_not_found_in_lcsc" in row["lcsc"]["note"]


def test_falls_back_to_primary_only_when_artifacts_missing(tmp_path):
    proj = tmp_path / "Projects" / "p"
    cs = proj / "datasheets" / "component_selecting"
    cs.mkdir(parents=True)
    # No _artifacts/ dir created
    _write_evidence(cs, "MPN1", vendor="mouser_jp")

    result = scan_coverage(cs)

    assert result["data_source"] == "primary-only"
    assert result["totals"]["mouser_jp"] == 1
    # Other lanes still labeled "no evidence" (legacy behavior preserved)
    assert result["matrix"][0]["lcsc"]["note"] == "no evidence"
