#!/usr/bin/env python3
"""sch → pcb: export netlist from .kicad_sch, then create .kicad_pcb via pcbnew API.

This script runs in the workspace .venv. It:
  1. Calls `kicad-cli sch export netlist` to get netlist (kicadsexpr format)
  2. Parses netlist into {components, nets} JSON
  3. Subprocess-calls _kicad_python_helper.py with KiCad's bundled Python 3.9
     (which has the pcbnew module) to actually create the .kicad_pcb

Why subprocess: pcbnew is only available in KiCad's bundled Python, not workspace .venv.
We avoid string-concatenation S-expression generation (which the previous version did).

Usage:
    python sch_to_pcb.py <project_dir>
    python sch_to_pcb.py <sch_file> --output <pcb_file>
"""

# -- shared environment: pcbenv (workspace root) also applies the utf-8
#    stdio guard on import, needed under cp936 consoles/pipes --
import sys as _sys
from pathlib import Path as _Path
for _p in _Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        _sys.path.insert(0, str(_p))
        break
else:
    raise ImportError("pcbenv.py not found above " + __file__)
import pcbenv

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── KiCad CLI + bundled Python detection (shared: pcbenv) ─────

KICAD_CLI = pcbenv.find_kicad_cli() or ""
KICAD_PYTHON = pcbenv.find_kicad_python() or ""
_fp_dir = pcbenv.find_kicad_subdir("footprints")
KICAD_FP_DIR = _fp_dir if _fp_dir else Path(
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"
)

SCRIPT_DIR = Path(__file__).resolve().parent
HELPER_SCRIPT = SCRIPT_DIR / "_kicad_python_helper.py"


# ── Minimal S-expression parser (for netlist parsing only) ──

def _parse_one(text: str, start: int):
    """Parse one '(...)' starting at `start`. Returns (parsed_list, end_index)."""
    i = start + 1  # skip '('
    tokens = []
    while i < len(text):
        c = text[i]
        if c == '(':
            inner, end = _parse_one(text, i)
            tokens.append(inner)
            i = end + 1
        elif c == ')':
            return tokens, i
        elif c in ' \t\n\r':
            i += 1
        elif c == '"':
            j = text.index('"', i + 1)
            tokens.append(text[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] not in ' \t\n\r()':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens, i


def parse_sexp(text: str) -> List:
    """Parse a full S-expression file; returns list of top-level forms."""
    result = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == '(':
            tree, end = _parse_one(text, i)
            result.append(tree)
            i = end + 1
        else:
            i += 1
    return result


def sexp_find(tree: List, key: str) -> Optional[List]:
    if not isinstance(tree, list):
        return None
    for item in tree:
        if isinstance(item, list) and item and item[0] == key:
            return item
    return None


def sexp_find_all(tree: List, key: str) -> List[List]:
    if not isinstance(tree, list):
        return []
    return [item for item in tree if isinstance(item, list) and item and item[0] == key]


# ── Netlist export + parse ────────────────────────────────────

def export_netlist(sch_path: Path, out_path: Path) -> bool:
    """Run kicad-cli sch export netlist --format kicadsexpr."""
    if not KICAD_CLI:
        print("❌ kicad-cli not found", file=sys.stderr)
        return False
    cmd = pcbenv.cli_argv(KICAD_CLI, ["sch", "export", "netlist",
           "--format", "kicadsexpr",
           "-o", str(out_path), str(sch_path)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(f"❌ netlist export failed: {proc.stderr}", file=sys.stderr)
        return False
    return out_path.exists()


def parse_netlist(net_path: Path) -> Tuple[List[Dict], List[Dict]]:
    """Parse kicadsexpr netlist → (components, nets)."""
    text = net_path.read_text(encoding="utf-8")
    forms = parse_sexp(text)
    if not forms:
        return [], []
    root = forms[0]  # outer (export ...)

    components = []
    comps_node = sexp_find(root, "components")
    if comps_node:
        for comp in sexp_find_all(comps_node, "comp"):
            ref = sexp_find(comp, "ref")
            ref_v = ref[1] if ref and len(ref) > 1 else ""
            val = sexp_find(comp, "value")
            val_v = val[1] if val and len(val) > 1 else ""
            fp = sexp_find(comp, "footprint")
            fp_v = fp[1] if fp and len(fp) > 1 else ""
            if ref_v:
                components.append({"ref": ref_v, "value": val_v, "footprint": fp_v})

    nets = []
    nets_node = sexp_find(root, "nets")
    if nets_node:
        for net in sexp_find_all(nets_node, "net"):
            name = sexp_find(net, "name")
            name_v = name[1] if name and len(name) > 1 else ""
            pins = []
            for node in sexp_find_all(net, "node"):
                ref_n = sexp_find(node, "ref")
                pin_n = sexp_find(node, "pin")
                if ref_n and pin_n:
                    pins.append({"ref": ref_n[1], "pin": pin_n[1]})
            if name_v:
                nets.append({"name": name_v, "pins": pins})

    return components, nets


# ── lib_external auto-discovery ───────────────────────────────

def find_lib_search_dirs(sch_path: Path) -> List[str]:
    """Walk up from sch_path looking for lib_external/, plus KiCad built-in dir."""
    dirs = []
    p = sch_path.resolve().parent
    while p.parent != p:
        candidate = p / "lib_external"
        if candidate.is_dir():
            dirs.append(str(candidate))
            break
        p = p.parent
    if KICAD_FP_DIR.is_dir():
        dirs.append(str(KICAD_FP_DIR))
    return dirs


# ── Main entry ────────────────────────────────────────────────

def create_pcb_from_sch(sch_path: Path, pcb_path: Path) -> Dict:
    """Top-level: SCH → netlist → JSON spec → KiCad Python helper → .kicad_pcb."""
    print(f"  SCH: {sch_path}")
    print(f"  PCB: {pcb_path}")

    if not KICAD_PYTHON:
        return {"ok": False, "error": "KiCad bundled Python not found (need pcbnew module)"}
    if not HELPER_SCRIPT.exists():
        return {"ok": False, "error": f"helper missing: {HELPER_SCRIPT}"}

    # 1. Export netlist
    with tempfile.TemporaryDirectory() as td:
        net_path = Path(td) / "netlist.net"
        if not export_netlist(sch_path, net_path):
            return {"ok": False, "error": "Netlist export failed"}

        # 2. Parse netlist
        components, nets = parse_netlist(net_path)
        if not components:
            return {"ok": False, "error": "No components parsed from netlist"}
        print(f"  Parsed: {len(components)} components, {len(nets)} nets")

        # 3. Build helper input JSON
        spec = {
            "mode": "create_pcb",
            "components": components,
            "nets": nets,
            "footprint_libs": find_lib_search_dirs(sch_path),
            "output_pcb": str(pcb_path),
        }
        input_json = Path(td) / "spec.json"
        input_json.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        # 4. Subprocess call to KiCad Python
        cmd = [KICAD_PYTHON, str(HELPER_SCRIPT), "--input", str(input_json)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "pcbnew helper timed out"}

        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"pcbnew helper failed (rc={proc.returncode})",
                "stderr": proc.stderr[:1000],
                "stdout": proc.stdout[:1000],
            }

        # 5. Parse helper JSON output
        try:
            helper_result = json.loads(proc.stdout.strip().split("\n")[-1])
        except json.JSONDecodeError as e:
            return {
                "ok": False,
                "error": f"Failed to parse helper output: {e}",
                "stdout": proc.stdout[:1000],
            }

        if not helper_result.get("ok"):
            return helper_result

        if not pcb_path.exists():
            return {"ok": False, "error": f"helper claimed success but PCB file missing: {pcb_path}"}

        result = {
            "ok": True,
            "pcb_path": str(pcb_path),
            "components_total": len(components),
            "nets_total": len(nets),
            "footprints_added": helper_result.get("footprints_added", 0),
            "nets_added": helper_result.get("nets_added", 0),
            "pad_net_assignments": helper_result.get("pad_net_assignments", 0),
            "missing": helper_result.get("missing", []),
            "pcbnew_version": helper_result.get("pcbnew_version"),
        }

        if result["missing"]:
            print(f"  ⚠ {len(result['missing'])} footprints missing:")
            for m in result["missing"]:
                print(f"    - {m.get('ref')}: {m.get('reason')}")
        print(f"  ✓ {result['footprints_added']}/{len(components)} footprints, "
              f"{result['nets_added']} nets, {result['pad_net_assignments']} pad-net assignments")
        return result


def ensure_pcb_exists(project_dir: Path, force: bool = False) -> Dict:
    """Ensure .kicad_pcb exists. Create from SCH if missing or --force."""
    sch_files = [f for f in project_dir.rglob("*.kicad_sch") if ".history" not in str(f)]
    pcb_files = [f for f in project_dir.rglob("*.kicad_pcb") if ".history" not in str(f)]

    if pcb_files and not force:
        print(f"  ✓ PCB already exists: {pcb_files[0]}")
        return {"ok": True, "pcb_path": str(pcb_files[0]), "created": False}

    if not sch_files:
        return {"ok": False, "error": "No .kicad_sch in project_dir"}

    sch_path = sch_files[0]
    pcb_path = sch_path.with_suffix(".kicad_pcb")

    result = create_pcb_from_sch(sch_path, pcb_path)
    if result.get("ok"):
        result["created"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create .kicad_pcb from .kicad_sch via pcbnew API")
    parser.add_argument("project_dir", type=Path, help="Project directory (or .kicad_sch path)")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output .kicad_pcb path")
    parser.add_argument("--force", action="store_true", help="Recreate even if PCB exists")
    args = parser.parse_args()

    if args.project_dir.is_file() and args.project_dir.suffix == ".kicad_sch":
        out = args.output or args.project_dir.with_suffix(".kicad_pcb")
        result = create_pcb_from_sch(args.project_dir, out)
    else:
        result = ensure_pcb_exists(args.project_dir, force=args.force)

    print("\n--- JSON ---")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)
