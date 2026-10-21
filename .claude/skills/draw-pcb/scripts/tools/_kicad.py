"""Shared helper for draw-pcb toolbox tools that mutate the board.

Board mutation needs the `pcbnew` module, which only exists inside KiCad's
bundled Python (not the workspace .venv). This module locates that interpreter
and invokes _kicad_python_helper.py as a subprocess.
"""
import glob
import json
import os
import subprocess
import tempfile
from pathlib import Path

_HELPER = Path(__file__).resolve().parent.parent / "_kicad_python_helper.py"

_KICAD_PYTHON_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/3.9/bin/python3.9",
    "/usr/lib/kicad/python3",
]


def find_kicad_python() -> str | None:
    env_py = os.environ.get("KICAD_PYTHON")
    if env_py and Path(env_py).exists():
        return env_py
    for p in _KICAD_PYTHON_CANDIDATES:
        if Path(p).exists():
            return p
    # Windows install roots (versioned sub-dirs + flat layout on custom drives)
    for pat in (r"C:\Program Files\KiCad\*\bin\python.exe",
                r"F:\KiCad\*\bin\python.exe",
                r"F:\KiCad\bin\python.exe"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def call_helper(spec: dict, timeout: int = 120) -> dict:
    """Run _kicad_python_helper.py with `spec` and return its JSON result."""
    kp = find_kicad_python()
    if not kp:
        return {"ok": False,
                "error": "KiCad bundled Python not found (need pcbnew)"}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f, ensure_ascii=False)
        spec_path = f.name
    try:
        proc = subprocess.run([kp, str(_HELPER), "--input", spec_path],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "helper timed out"}
    finally:
        Path(spec_path).unlink(missing_ok=True)
    # Helper prints JSON to BOTH stdout and stderr (macOS wx eats stdout
    # under subprocess sometimes) — scan both, take the last JSON line.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    json_lines = [ln for ln in combined.splitlines()
                  if ln.lstrip().startswith("{")]
    if not json_lines:
        return {"ok": False, "error": "no JSON from helper",
                "stderr": (proc.stderr or "")[:400]}
    try:
        return json.loads(json_lines[-1])
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"helper output parse: {e}"}
