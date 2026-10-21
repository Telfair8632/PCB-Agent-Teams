"""
Regression tests for aa_filter_sim.py.

Run: python3 -m pytest tests/test_aa_filter_sim.py
or  : python3 tests/test_aa_filter_sim.py
"""

# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "scripts" / "aa_filter_sim.py"
FIXTURE = HERE / "fixtures" / "sample_lowpass_analysis.json"


def _have_ngspice():
    return shutil.which("ngspice") or Path("/opt/homebrew/bin/ngspice").exists()


def test_se_lowpass_detected():
    """Single-ended R-C-to-GND filter is detected and fc matches analytic."""
    sys.path.insert(0, str(SCRIPT.parent))
    from aa_filter_sim import find_aa_candidates
    analysis = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cands = find_aa_candidates(analysis)
    se = [c for c in cands if c['cap']['ref'] == 'C1']
    assert len(se) == 1, f"Expected 1 SE candidate for C1, got {len(se)}"
    c = se[0]
    assert c['hot_net'] == 'SIGNAL'
    assert c['gnd_net'] == 'GND'
    # R_eq = R1 (1kΩ), C = 10nF → fc = 1/(2π × 1k × 10n) ≈ 15915 Hz
    assert abs(c['r_eq_ohms'] - 1000) < 1, f"R_eq wrong: {c['r_eq_ohms']}"
    assert abs(c['fc_analytic_hz'] - 15915) < 50, f"fc wrong: {c['fc_analytic_hz']}"


def test_diff_filter_detected():
    """Differential R-R-C bridge is detected with correct R_total."""
    sys.path.insert(0, str(SCRIPT.parent))
    from aa_filter_sim import find_diff_aa_candidates
    analysis = json.loads(FIXTURE.read_text(encoding="utf-8"))
    diff = find_diff_aa_candidates(analysis)
    assert len(diff) == 1, f"Expected 1 diff candidate, got {len(diff)}"
    c = diff[0]
    # R_total = R2 + R3 = 200Ω, C = 100nF → fc = 1/(2π × 200 × 100n) ≈ 7958 Hz
    assert abs(c['r_total_ohms'] - 200) < 1
    assert abs(c['fc_diff_analytic_hz'] - 7958) < 50


def test_uuid_net_coalesced():
    """`/uuid/X` and `X` are merged into one logical net."""
    sys.path.insert(0, str(SCRIPT.parent))
    from aa_filter_sim import coalesce_nets
    analysis = json.loads(FIXTURE.read_text(encoding="utf-8"))
    coalesced = coalesce_nets(analysis['nets'])
    assert 'SIGNAL' in coalesced
    # Should contain both R1.2 (from /uuid/SIGNAL) AND C1.1 (from bare SIGNAL)
    pins = coalesced['SIGNAL']
    refs = {ref for ref, _pin in pins}
    assert 'R1' in refs and 'C1' in refs, f"SIGNAL pins not coalesced: {pins}"


def test_end_to_end_with_ngspice():
    """Full sweep runs ngspice and reports pass for both candidates."""
    if not _have_ngspice():
        print("SKIP: ngspice not installed")
        return
    venv_py = sys.executable
    out = HERE / "fixtures" / "_aa_filter_sim_actual.json"
    result = subprocess.run(
        [venv_py, str(SCRIPT), str(FIXTURE), "-o", str(out)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report['summary']['total'] == 2
    # Without spec, expect numeric_only verdict for both
    assert report['summary']['numeric_only'] + report['summary']['pass'] == 2
    assert report['summary']['fail'] == 0
    out.unlink()


if __name__ == "__main__":
    tests = [
        ("test_se_lowpass_detected", test_se_lowpass_detected),
        ("test_diff_filter_detected", test_diff_filter_detected),
        ("test_uuid_net_coalesced", test_uuid_net_coalesced),
        ("test_end_to_end_with_ngspice", test_end_to_end_with_ngspice),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed += 1
    sys.exit(1 if failed else 0)
