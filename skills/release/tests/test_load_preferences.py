import json
from pathlib import Path

from scripts.load_preferences import channel_to_recommended_path, load_preferences


def _write_prefs(proj_dir: Path, payload: dict) -> Path:
    out = proj_dir / "_artifacts" / "component_selecting" / "user_preferences.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


def _valid_payload(**override) -> dict:
    base = {
        "schema_version": "v1",
        "asked_at": "2026-05-08T01:00:00+00:00",
        "channel": "lcsc_jlcpcb",
        "brand": "any",
        "price_vs_stock": "balanced",
        "blacklist_mpns": [],
    }
    base.update(override)
    return base


def test_load_returns_dict_when_valid(tmp_path):
    _write_prefs(tmp_path, _valid_payload())
    result = load_preferences(tmp_path)
    assert result is not None
    assert result["channel"] == "lcsc_jlcpcb"


def test_load_returns_none_when_file_missing(tmp_path):
    assert load_preferences(tmp_path) is None


def test_load_returns_none_when_schema_unsupported(tmp_path):
    _write_prefs(tmp_path, _valid_payload(schema_version="v999"))
    assert load_preferences(tmp_path) is None


def test_load_returns_none_when_channel_unknown(tmp_path):
    _write_prefs(tmp_path, _valid_payload(channel="random_channel"))
    assert load_preferences(tmp_path) is None


def test_load_returns_none_when_required_keys_missing(tmp_path):
    payload = _valid_payload()
    del payload["brand"]
    _write_prefs(tmp_path, payload)
    assert load_preferences(tmp_path) is None


def test_load_returns_none_when_json_corrupt(tmp_path):
    out = tmp_path / "_artifacts" / "component_selecting" / "user_preferences.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("{ not json", encoding="utf-8")
    assert load_preferences(tmp_path) is None


def test_channel_mapping():
    assert channel_to_recommended_path("lcsc_jlcpcb") == "lcsc"
    assert channel_to_recommended_path("jp_domestic_fast") == "jp_domestic"
    assert channel_to_recommended_path("auto_cheapest") == "auto"
    # unknown → conservative auto
    assert channel_to_recommended_path("garbage") == "auto"
