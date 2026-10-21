"""Tests for pcbenv — the shared environment-discovery module.

Covers the behaviors every skill now depends on:
  - the junction-safe import bootstrap (Windows harness junctions point
    back into this repo; Path.resolve() must carry the import to pcbenv.py)
  - canonical precedence: explicit > env var > PATH > install roots
  - numeric version sort (KiCad 10.0 ranks above 9.0)
  - venv layout per OS (the JP skill shipped a POSIX-only path — fixed)
  - project CLAUDE.md walk-up that stops before the workspace root
  - byte-identical duplicated assets stay in sync (drift guards)
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pcbenv


# ----------------------------------------------------------------------
# Roots
# ----------------------------------------------------------------------

def test_root_is_repo_root():
    assert pcbenv.ROOT == REPO_ROOT
    assert (pcbenv.ROOT / "CLAUDE.md").is_file()
    assert (pcbenv.ROOT / ".venv").is_dir()


def test_workspace_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_ROOT", str(tmp_path))
    assert pcbenv.workspace_root() == tmp_path


def test_workspace_root_explicit_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_ROOT", r"C:\definitely\not\real")
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()
    assert pcbenv.workspace_root(explicit) == explicit


def test_skills_root_exists():
    assert pcbenv.skills_root().is_dir()
    assert (pcbenv.skills_root() / "draw-pcb").is_dir()


# ----------------------------------------------------------------------
# Version sort
# ----------------------------------------------------------------------

def test_version_sorted_numeric_not_lexicographic():
    paths = [r"C:\KiCad\9.0\bin\x.exe", r"C:\KiCad\10.0\bin\x.exe",
             r"C:\KiCad\7.0\bin\x.exe"]
    assert "10.0" in pcbenv.version_sorted(paths, descending=True)[0]


# ----------------------------------------------------------------------
# kicad-cli
# ----------------------------------------------------------------------

def test_find_kicad_cli_env_var(monkeypatch, tmp_path):
    fake = tmp_path / "kicad-cli.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("KICAD_CLI", str(fake))
    assert pcbenv.find_kicad_cli() == str(fake)


def test_find_kicad_cli_dead_env_var_falls_through(monkeypatch):
    monkeypatch.setenv("KICAD_CLI", r"C:\definitely\not\real\kicad-cli.exe")
    # On this workspace's CI machine KiCad is always installed; the contract
    # under test is the fall-through, so assert "not the dead path".
    assert pcbenv.find_kicad_cli() != r"C:\definitely\not\real\kicad-cli.exe"


def test_find_kicad_cli_explicit_missing_returns_none(tmp_path):
    assert pcbenv.find_kicad_cli(str(tmp_path / "nope.exe")) is None


def test_cli_argv_plain_path_stays_single_element():
    argv = pcbenv.cli_argv(r"C:\Program Files\KiCad\bin\kicad-cli.exe",
                           ["pcb", "drc", "board.kicad_pcb"])
    assert argv[:1] == [r"C:\Program Files\KiCad\bin\kicad-cli.exe"]
    assert argv[1:] == ["pcb", "drc", "board.kicad_pcb"]


def test_cli_argv_multi_word_wrapper():
    argv = pcbenv.cli_argv("flatpak run --command=kicad-cli org.kicad.KiCad",
                           ["--version"])
    assert argv == ["flatpak", "run", "--command=kicad-cli",
                    "org.kicad.KiCad", "--version"]


# ----------------------------------------------------------------------
# KiCad bundled Python
# ----------------------------------------------------------------------

def test_find_kicad_python_env_var(monkeypatch, tmp_path):
    fake = tmp_path / ("python.exe" if os.name == "nt" else "python")
    fake.write_bytes(b"")
    monkeypatch.setenv("KICAD_PYTHON", str(fake))
    assert pcbenv.find_kicad_python() == str(fake)


def test_find_kicad_python_dead_env_var_falls_through(monkeypatch):
    monkeypatch.setenv("KICAD_PYTHON", r"C:\definitely\not\real\python.exe")
    assert pcbenv.find_kicad_python() != r"C:\definitely\not\real\python.exe"


# ----------------------------------------------------------------------
# venv layout
# ----------------------------------------------------------------------

def test_venv_relpath_per_os():
    assert pcbenv.venv_relpath("nt") == os.path.join("Scripts", "python.exe")
    assert pcbenv.venv_relpath("posix") == os.path.join("bin", "python")


def test_venv_python_explicit_root(tmp_path):
    got = pcbenv.venv_python(tmp_path)
    rel = os.path.join(".venv", pcbenv.venv_relpath(os.name))
    assert got.endswith(rel)


# ----------------------------------------------------------------------
# Project CLAUDE.md walk
# ----------------------------------------------------------------------

def _make_project_tree(ws: Path):
    (ws / "CLAUDE.md").write_text("# workspace routing index\n", encoding="utf-8")
    proj = ws / "Projects" / "demo"
    deep = proj / "subs" / "deeper"
    deep.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("# project spec\n", encoding="utf-8")
    (deep / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    return proj, deep


def test_find_project_claude_md_walks_up_and_skips_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    proj, deep = _make_project_tree(ws)
    found = pcbenv.find_project_claude_md(deep, ws)
    assert found == proj / "CLAUDE.md"


def test_find_project_claude_md_none_when_only_workspace_has_one(tmp_path):
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("# workspace only\n", encoding="utf-8")
    empty_proj = ws / "Projects" / "empty"
    empty_proj.mkdir(parents=True)
    assert pcbenv.find_project_claude_md(empty_proj, ws) is None


def test_find_project_claude_md_fallback_rglob(tmp_path):
    ws = tmp_path / "ws3"
    ws.mkdir()
    proj = ws / "Projects" / "nested"
    inner = proj / "hardware"
    inner.mkdir(parents=True)
    # CLAUDE.md sits *below* the start dir, not above it.
    (inner / "CLAUDE.md").write_text("# nested spec\n", encoding="utf-8")
    found = pcbenv.find_project_claude_md(proj, ws)
    assert found == inner / "CLAUDE.md"


# ----------------------------------------------------------------------
# Junction-safe import bootstrap (the multi-harness contract)
# ----------------------------------------------------------------------

PROBE = '''\
import sys
from pathlib import Path
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
else:
    raise ImportError("pcbenv.py not found above " + __file__)
import pcbenv
print(pcbenv.ROOT)
'''


@pytest.mark.skipif(os.name != "nt", reason="junction test is Windows-specific")
def test_import_bootstrap_through_junction(tmp_path):
    """A script reached via a harness junction (like ~/.zcode/skills/<skill>)
    must still find pcbenv.py — Path.resolve() follows the junction back to
    the real workspace."""
    ws = tmp_path / "real-workspace"
    scripts = ws / ".claude" / "skills" / "demo" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "pcbenv.py", ws / "pcbenv.py")
    probe = scripts / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")

    junction = tmp_path / "junction-skills" / "demo"
    junction.parent.mkdir()
    import _winapi
    _winapi.CreateJunction(str(scripts.parent), str(junction))

    ran = subprocess_run_no_utf8_env([sys.executable, str(junction / "scripts" / "probe.py")])
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == str(ws)


def subprocess_run_no_utf8_env(argv):
    """Run without PYTHONUTF8/PYTHONIOENCODING to prove the stdio guard
    holds under a default cp936-style console."""
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    return subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          cwd=str(Path(argv[1]).parent))


# ----------------------------------------------------------------------
# Drift guards: byte-identical duplicated assets must stay in sync
# ----------------------------------------------------------------------

def test_kicad_utils_copies_identical():
    a = REPO_ROOT / ".claude/skills/check-pcb/scripts/kicad_utils.py"
    b = REPO_ROOT / ".claude/skills/check-schematic/scripts/kicad_utils.py"
    assert a.is_file() and b.is_file()
    assert a.read_bytes() == b.read_bytes(), (
        "check-pcb and check-schematic kicad_utils.py drifted apart — "
        "apply the same edit to both (or unify them) and re-run.")


def test_report_generation_copies_identical():
    a = REPO_ROOT / ".claude/skills/check-pcb/references/report-generation.md"
    b = REPO_ROOT / ".claude/skills/check-schematic/references/report-generation.md"
    assert a.is_file() and b.is_file()
    assert a.read_bytes() == b.read_bytes(), (
        "check-pcb and check-schematic report-generation.md drifted apart — "
        "apply the same edit to both and re-run.")
