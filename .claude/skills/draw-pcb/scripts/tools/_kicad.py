"""Shared helper for draw-pcb toolbox tools that mutate the board.

Board mutation needs the `pcbnew` module, which only exists inside KiCad's
bundled Python (not the workspace .venv). Interpreter discovery lives in
pcbenv (workspace root); this module invokes _kicad_python_helper.py as a
subprocess with that interpreter.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HELPER = Path(__file__).resolve().parent.parent / "_kicad_python_helper.py"

for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
import pcbenv


def find_kicad_python() -> str | None:
    return pcbenv.find_kicad_python()


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
                              capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
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
