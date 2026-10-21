"""
Regression tests for inject_mpn_props.py.

Run: python3 tests/test_inject_mpn_props.py
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
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SCRIPTS = HERE.parent / "scripts"
FIXTURE_DIR = HERE / "fixtures" / "sample_project"
sys.path.insert(0, str(SCRIPTS))


def _setup_tmp_project():
    """Copy sample_project fixture to a tmp dir; bom_readiness gets py_mtime fix."""
    tmp = Path(tempfile.mkdtemp(prefix="inject_mpn_test_"))
    shutil.copytree(FIXTURE_DIR, tmp / "sample_project")
    py = tmp / "sample_project" / "sample_project.py"
    sentinel = tmp / "sample_project" / "datasheets" / ".bom_readiness.json"
    s = json.loads(sentinel.read_text(encoding="utf-8"))
    s["py_mtime"] = int(py.stat().st_mtime)
    sentinel.write_text(json.dumps(s), encoding="utf-8")
    return tmp / "sample_project", py


def test_canonical_mpn_resolution():
    """R2 sentinel.mpn='100R' but evidence has canonical 'RC0805FR-07100RL'."""
    from inject_mpn_props import _resolve_evidence
    proj, _ = _setup_tmp_project()
    ev_dir = proj / "datasheets" / "component_selecting"
    ds_path = proj / "datasheets" / "RC0805FR-07100RL.pdf"
    ev = _resolve_evidence(ds_path, "100R", ev_dir)
    assert ev["mpn"] == "RC0805FR-07100RL", f"Wrong canonical MPN: {ev}"
    assert ev["manufacturer"] == "Yageo", f"Wrong manufacturer: {ev}"
    shutil.rmtree(proj.parent)


def test_inject_kwargs_format():
    """Inject produces direct kwargs (MPN=, Datasheet=, Manufacturer=) not properties dict."""
    from inject_mpn_props import inject, _load_sentinel
    proj, py = _setup_tmp_project()
    sentinel = _load_sentinel(py)
    n, _log = inject(py, sentinel, dry_run=False)
    assert n == 2, f"Expected 2 injections (U1+R2), got {n}"
    src = py.read_text(encoding="utf-8")
    # U1 should have all three direct kwargs
    assert 'MPN="TLV70033DDCR"' in src
    assert 'Manufacturer="Texas Instruments"' in src
    # R2 should resolve to canonical Yageo MPN, not "100R"
    assert 'MPN="RC0805FR-07100RL"' in src, f"R2 MPN not canonical: {src}"
    assert 'Manufacturer="Yageo"' in src
    # NOT use the dict-style properties= (which circuit-synth serializes wrong)
    assert 'properties={' not in src, "Should not use properties dict"
    shutil.rmtree(proj.parent)


def test_idempotent():
    """Re-running inject on already-injected .py is a no-op."""
    from inject_mpn_props import inject, _load_sentinel
    proj, py = _setup_tmp_project()
    sentinel = _load_sentinel(py)
    inject(py, sentinel, dry_run=False)
    src1 = py.read_text(encoding="utf-8")
    n2, _log = inject(py, sentinel, dry_run=False)
    src2 = py.read_text(encoding="utf-8")
    assert n2 == 0, f"Second pass injected {n2}; should be 0 (idempotent)"
    assert src1 == src2, "Re-run modified file"
    shutil.rmtree(proj.parent)


def test_skip_generic_passive():
    """R1 (10k, no datasheet) is skipped — no MPN injected."""
    from inject_mpn_props import inject, _load_sentinel
    proj, py = _setup_tmp_project()
    sentinel = _load_sentinel(py)
    inject(py, sentinel, dry_run=False)
    src = py.read_text(encoding="utf-8")
    # R1 line should NOT have MPN= kwarg
    r1_lines = [l for l in src.split('\n') if 'ref="R1"' in l]
    assert len(r1_lines) == 1
    assert 'MPN=' not in r1_lines[0], f"R1 wrongly got MPN: {r1_lines[0]}"
    shutil.rmtree(proj.parent)


if __name__ == "__main__":
    tests = [
        ("test_canonical_mpn_resolution", test_canonical_mpn_resolution),
        ("test_inject_kwargs_format", test_inject_kwargs_format),
        ("test_idempotent", test_idempotent),
        ("test_skip_generic_passive", test_skip_generic_passive),
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
