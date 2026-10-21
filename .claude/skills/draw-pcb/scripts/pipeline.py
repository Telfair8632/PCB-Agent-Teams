#!/usr/bin/env python3
"""draw-pcb pipeline: SCH + CLAUDE.md → .kicad_pcb (region placement + outline
+ GND zones + DRC + PDF + EMC).

Phases (all fail-fast, JSON output):
  0. Pre-flight   : SCH + project CLAUDE.md + KiCad CLI + bundled Python
  1. SCH → PCB    : create blank .kicad_pcb via pcbnew API (subprocess)
  2. Placement    : classify HV/LV/ISO + grid lay inside region + Edge.Cuts
                    outline + isolation slot via pcbnew API
  2.7 Slot check  : auto-rotate ISO ICs, auto-move pads that drifted into slot
  2.75 Pad sweep  : pad-pad clearance sweep (separate helper process)
  2.77 In-board   : slide footprints whose pads fell outside Edge.Cuts back in
  2.8 GND zones   : add B.Cu copper pour for GND-like nets
  3.  DRC         : kicad-cli pcb drc, structured JSON
  4.  PDF / SVG   : kicad-cli pcb export for Claude L2 visual verification
  5.  EMC (opt)   : runs check-pcb's analyze_emc if --with-emc
  6.  Doc (opt)   : design review markdown + PDF if --with-design-review

This one-shot pipeline does not route. Auto-routing lives in the skill's
Phase E (scripts/tools/route.py, KRT) — see SKILL.md. After this pipeline
finishes, pick one: run Phase E, route by hand in the KiCad GUI, or skip
routing and go straight to review.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
KICAD_SKILLS_ROOT = SCRIPT_DIR.parent.parent  # .claude/skills/

# Locate workspace root (contains CLAUDE.md + .venv)
KICAD_ROOT = SCRIPT_DIR
while KICAD_ROOT.parent != KICAD_ROOT:
    if (KICAD_ROOT / "CLAUDE.md").exists() and (KICAD_ROOT / ".venv").is_dir():
        break
    KICAD_ROOT = KICAD_ROOT.parent

def _ver_sorted(pattern: str):
    """Glob with numeric version sort (10.0 ranks above 9.0)."""
    return sorted(
        glob.glob(pattern),
        key=lambda s: [int(t) for t in re.findall(r"\d+", s)],
    )


KICAD_CLI = ""
for p in [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/snap/kicad/current/bin/kicad-cli",
]:
    if Path(p).exists():
        KICAD_CLI = p
        break
if not KICAD_CLI:
    KICAD_CLI = shutil.which("kicad-cli") or ""
if not KICAD_CLI:
    # Windows: KiCad install dirs, newest version first
    _win_cli = _ver_sorted(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe") + _ver_sorted(r"F:\KiCad\*\bin\kicad-cli.exe") + [r"F:\KiCad\bin\kicad-cli.exe"]
    for p in _win_cli:
        KICAD_CLI = p
        break

KICAD_PYTHON = os.environ.get("KICAD_PYTHON", "")
if not (KICAD_PYTHON and Path(KICAD_PYTHON).exists()):
    KICAD_PYTHON = ""
    for p in [
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9",
        "/usr/lib/kicad/python3",
    ] + _ver_sorted(r"C:\Program Files\KiCad\*\bin\python.exe") + _ver_sorted(r"F:\KiCad\*\bin\python.exe") + [r"F:\KiCad\bin\python.exe"]:
        if Path(p).exists():
            KICAD_PYTHON = p
            break

if KICAD_ROOT:
    _venv_sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    VENV_PYTHON = str(KICAD_ROOT / ".venv" / _venv_sub)
else:
    VENV_PYTHON = ""

# Paths updated 2026-05 after skill consolidation:
#   emc/kicad analyzers → check-schematic / check-pcb
#   kidoc → release/scripts/kidoc/
EMC_SCRIPT = KICAD_SKILLS_ROOT / "check-pcb" / "scripts" / "analyze_emc.py"
KICAD_ANALYZE_SCH = KICAD_SKILLS_ROOT / "check-schematic" / "scripts" / "analyze_schematic.py"
KICAD_ANALYZE_PCB = KICAD_SKILLS_ROOT / "check-pcb" / "scripts" / "analyze_pcb.py"
KIDOC_SCAFFOLD = KICAD_SKILLS_ROOT / "release" / "scripts" / "kidoc" / "kidoc_scaffold.py"
KIDOC_GENERATE = KICAD_SKILLS_ROOT / "release" / "scripts" / "kidoc" / "kidoc_generate.py"


# =============================================================
# Phase 0: Preflight
# =============================================================

def phase0_preflight(project_dir: Path) -> Dict:
    print("\n=== Phase 0: Pre-flight ===")
    sch_files = [f for f in project_dir.rglob("*.kicad_sch") if ".history" not in str(f)]
    # Filter out side files left by tools: _zones / _routed / _signal / _diff / _planes
    SIDE_SUFFIX = ("_zones", "_routed", "_signal", "_diff", "_planes", "_final", "_kt")
    pcb_files = [f for f in project_dir.rglob("*.kicad_pcb")
                 if ".history" not in str(f)
                 and not any(f.stem.endswith(sfx) for sfx in SIDE_SUFFIX)]

    # Find CLAUDE.md: walk up from project_dir until we hit workspace root.
    # Pick the one closest to project_dir but stop before reaching workspace
    # CLAUDE.md (which is a routing index, not a per-project spec).
    claude_md = None
    walk = project_dir.resolve()
    workspace_root = KICAD_ROOT.resolve() if KICAD_ROOT else None
    for _ in range(6):
        cand = walk / "CLAUDE.md"
        if cand.exists() and (workspace_root is None or walk != workspace_root):
            claude_md = cand
            break
        if walk.parent == walk or (workspace_root and walk == workspace_root):
            break
        walk = walk.parent
    if not claude_md:
        for cand in project_dir.rglob("CLAUDE.md"):
            claude_md = cand
            break

    result = {
        "ok": True,
        "project_dir": str(project_dir),
        "sch_path": str(sch_files[0]) if sch_files else None,
        "pcb_path": str(pcb_files[0]) if pcb_files else None,
        "claude_md": str(claude_md) if claude_md else None,
    }

    issues = []
    if not sch_files:
        issues.append("No .kicad_sch found")
    if not KICAD_CLI:
        issues.append("kicad-cli not found")
    if not KICAD_PYTHON:
        issues.append("KiCad bundled Python not found (need pcbnew)")

    if issues:
        result["ok"] = False
        result["error"] = "; ".join(issues)
        for i in issues:
            print(f"  ❌ {i}")
        return result

    print(f"  ✓ SCH: {sch_files[0].name}")
    print(f"  ✓ PCB: {pcb_files[0].name if pcb_files else '(will create)'}")
    print(f"  ✓ CLAUDE.md: {'yes' if claude_md else '(none, defaults will apply)'}")
    print(f"  ✓ kicad-cli: {KICAD_CLI}")
    print(f"  ✓ KiCad Python: {KICAD_PYTHON}")
    return result


# =============================================================
# Phase 1: SCH → PCB
# =============================================================

def phase1_sch_to_pcb(preflight: Dict) -> Dict:
    print("\n=== Phase 1: SCH → PCB ===")
    if preflight.get("pcb_path") and Path(preflight["pcb_path"]).exists():
        print(f"  ✓ PCB exists: {Path(preflight['pcb_path']).name}")
        return {"ok": True, "pcb_path": preflight["pcb_path"], "created": False}

    sch_to_pcb_script = SCRIPT_DIR / "sch_to_pcb.py"
    cmd = [VENV_PYTHON, str(sch_to_pcb_script), str(Path(preflight["sch_path"]).parent)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return {"ok": False, "error": f"sch_to_pcb failed: {proc.stderr[:500]}"}

    pcb_files = [f for f in Path(preflight["project_dir"]).rglob("*.kicad_pcb")
                 if ".history" not in str(f)]
    if not pcb_files:
        return {"ok": False, "error": "PCB not created"}

    print(f"  ✓ Created: {pcb_files[0].name}")
    return {"ok": True, "pcb_path": str(pcb_files[0]), "created": True}


# =============================================================
# Phase 2: Placement
# =============================================================

def phase2_placement(preflight: Dict, pcb_path: str) -> Dict:
    print("\n=== Phase 2: Placement + board outline ===")
    place_script = SCRIPT_DIR / "place_components.py"
    cmd = [VENV_PYTHON, str(place_script), pcb_path]
    if preflight.get("claude_md"):
        cmd += ["--claude-md", preflight["claude_md"]]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if proc.returncode != 0:
        return {"ok": False, "error": f"placement failed: {proc.stderr[:500]}",
                "stdout": proc.stdout[:500]}

    out = proc.stdout.strip().split("--- JSON ---")
    try:
        result = json.loads(out[-1])
    except (json.JSONDecodeError, IndexError) as e:
        return {"ok": False, "error": f"placement output parse: {e}",
                "stdout": proc.stdout[:500]}

    if result.get("ok"):
        print(f"  ✓ Placed {result['footprints_placed']} footprints, "
              f"board {result['board']['w']}×{result['board']['h']}mm")
    return result


# =============================================================
# Helper invocation (used by 2.7 / 2.75 / 2.77 / 2.8)
# =============================================================

def _call_helper_mode(spec: Dict, timeout: int = 120) -> Dict:
    """Invoke _kicad_python_helper.py with given spec dict."""
    helper = SCRIPT_DIR / "_kicad_python_helper.py"
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f, ensure_ascii=False)
        spec_path = f.name
    try:
        proc = subprocess.run(
            [KICAD_PYTHON, str(helper), "--input", spec_path],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "helper timed out"}
    finally:
        Path(spec_path).unlink(missing_ok=True)
    try:
        # JSON sometimes lands on stderr (multiple LoadBoard calls hijack stdout
        # via wx), so search both streams.
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        lines = [l for l in combined.split("\n") if l.strip()]
        json_lines = [l for l in lines if l.lstrip().startswith("{")]
        if not json_lines:
            raise ValueError("no JSON output from helper")
        return json.loads(json_lines[-1])
    except Exception as e:
        return {"ok": False, "error": f"helper output parse: {e}",
                "stdout": proc.stdout[:500], "stderr": proc.stderr[:500]}


# =============================================================
# Phase 2.7: Slot defense (auto-rotate ISO IC + auto-move slot violators)
# =============================================================

def phase2_7_check_slot(pcb_path: str) -> Dict:
    print("\n=== Phase 2.7: Slot clearance check ===")
    result = _call_helper_mode({
        "mode": "check_slot_clearance",
        "pcb_path": str(pcb_path),
        "auto_fix": True,
        "pad_margin_mm": 0.5,
    }, timeout=30)
    if not result.get("ok"):
        print(f"  ⚠ check_slot_clearance failed: {result.get('error')}")
        return result
    if result.get("skipped"):
        print(f"  ⏭ {result.get('reason')}")
        return result

    rotated = result.get("iso_rotations", [])
    if rotated:
        print(f"  ↻ rotated {len(rotated)} ISO IC(s) to align HV/LV pads with zones:")
        for r in rotated:
            print(f"      {r['ref']}: {r['old_rot']:.0f}° → {r['new_rot']:.0f}° "
                  f"(HV pads were @ x={r['hv_mean_x']}, LV pads @ x={r['lv_mean_x']})")

    violations = result.get("violations", [])
    fixed = result.get("fixed", [])
    if not violations:
        print(f"  ✓ no pads in slot (slot {result['slot']['left']:.2f}–{result['slot']['right']:.2f})")
    else:
        print(f"  ⚠ {len(violations)} footprint(s) had pads in slot:")
        for v in violations:
            pads = ",".join(p["pad"] for p in v["bad_pads"])
            print(f"      {v['ref']} (zone={v['zone']}, pads {pads})")
        if fixed:
            print(f"  ✓ auto-moved {len(fixed)}:")
            for f in fixed:
                print(f"      {f['ref']}: body x {f['from_body_x']:.2f} → "
                      f"{f['to_body_x']:.2f}, y preserved at {f['preserved_body_y']:.2f} "
                      f"({f['side']} side)")
    return result


# =============================================================
# Phase 2.75: Pad-pad conflict sweep
# =============================================================

def phase2_75_pad_conflicts(pcb_path: str) -> Dict:
    print("\n=== Phase 2.75: Pad-pad conflict sweep ===")
    result = _call_helper_mode({
        "mode": "resolve_pad_conflicts",
        "pcb_path": str(pcb_path),
        "pad_min_clearance_mm": 0.4,
    }, timeout=30)
    if not result.get("ok"):
        print(f"  ⚠ resolve_pad_conflicts failed: {result.get('error')}")
        return result
    moves = result.get("moves", [])
    if not moves:
        print(f"  ✓ no pad conflicts")
    else:
        print(f"  ✓ resolved {len(moves)} pad conflicts:")
        for m in moves[:6]:
            print(f"      {m['ref']} (pad {m['pad']} vs {m['vs']}): "
                  f"Δ({m['dx_mm']:+.2f}, {m['dy_mm']:+.2f})mm")
        if len(moves) > 6:
            print(f"      ... +{len(moves)-6} more")
    return result


# =============================================================
# Phase 2.77: Ensure footprints inside Edge.Cuts
# =============================================================

def phase2_77_ensure_in_board(pcb_path: str) -> Dict:
    print("\n=== Phase 2.77: Ensure footprints inside board ===")
    result = _call_helper_mode({
        "mode": "ensure_pads_in_board",
        "pcb_path": str(pcb_path),
        "margin_mm": 0.6,
    }, timeout=30)
    if not result.get("ok"):
        print(f"  ⚠ ensure_pads_in_board failed: {result.get('error')}")
        return result
    moved = result.get("moved", [])
    if not moved:
        print(f"  ✓ all footprints already in board")
    else:
        print(f"  ✓ slid {len(moved)} footprint(s) inward:")
        for m in moved[:6]:
            print(f"      {m['ref']}: Δ({m['dx_mm']:+.2f}, {m['dy_mm']:+.2f}) mm")
        if len(moved) > 6:
            print(f"      ... +{len(moved)-6} more")
    return result


# =============================================================
# Phase 2.8: GND copper zones
# =============================================================

def phase2_8_ground_zones(pcb_path: str, skip: bool = False) -> Dict:
    """Add B.Cu copper pour zones for each ground net (GND, HV_GND, etc).
    Single-sided fill — HV/LV are physically isolated by the slot."""
    print("\n=== Phase 2.8: GND copper zones ===")
    if skip:
        print("  ⏭ Skipped")
        return {"ok": True, "skipped": True}

    result = _call_helper_mode({
        "mode": "add_ground_zones",
        "pcb_path": str(pcb_path),
        "ground_net_keywords": ["GND"],
        "layers": ["B.Cu"],
        "clearance_mm": 0.3,
        "fill_now": True,
    }, timeout=60)

    if not result.get("ok"):
        print(f"  ⚠ add_ground_zones failed: {result.get('error')}")
        return result
    if result.get("skipped"):
        print(f"  ⏭ {result.get('reason')}")
        return result

    zones = result.get("zones_added", [])
    layers = sorted({z["layer"] for z in zones})
    nets = sorted({z["net"] for z in zones})
    print(f"  ✓ Added {len(zones)} ground zones (nets: {','.join(nets)}; "
          f"layers: {','.join(layers)}), fill={result.get('fill_status')}")
    return result


# =============================================================
# Phase 3: DRC
# =============================================================

def phase3_drc(pcb_path: str, skip: bool = False) -> Dict:
    print("\n=== Phase 3: DRC ===")
    if skip:
        return {"ok": True, "skipped": True}

    drc_file = Path(pcb_path).with_suffix(".drc.json")
    cmd = [KICAD_CLI, "pcb", "drc", "--format", "json", "-o", str(drc_file), pcb_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not drc_file.exists():
        return {"ok": False, "error": "DRC produced no output", "stderr": proc.stderr[:300]}

    data = json.loads(drc_file.read_text())
    total_violations = len(data.get("violations", []) or [])
    total_unconnected = len(data.get("unconnected_items", []) or [])

    # Honor user-set exclusions in .kicad_pro (KH-1: previously ignored).
    # Triaged violations stay in the JSON output for traceability — only
    # active counts drive pass/fail decisions downstream.
    sys.path.insert(0, str(Path(__file__).parent))
    from drc_exclusions import read_exclusions_from_pro, prune
    exclusions = read_exclusions_from_pro(pcb_path)
    pruned = prune(data, exclusions)

    violations = data.get("violations", [])
    unconnected = data.get("unconnected_items", [])

    by_type: Dict[str, int] = {}
    for x in violations + unconnected:
        t = x.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1

    if exclusions:
        drc_file.write_text(json.dumps(data, indent=2))

    print(f"  Violations: {len(violations)}/{total_violations} active, "
          f"Unconnected: {len(unconnected)}/{total_unconnected} active")
    if pruned["total_excluded"]:
        print(f"  Excluded by .kicad_pro: {pruned['total_excluded']} "
              f"(violations={pruned['removed_violations']}, "
              f"unconnected={pruned['removed_unconnected']}, "
              f"parity={pruned['removed_parity']})")
    print(f"  By type (active): {by_type}")
    return {
        "ok": True,
        "drc_json": str(drc_file),
        "violation_count": len(violations),
        "unconnected_count": len(unconnected),
        "violation_total_count": total_violations,
        "unconnected_total_count": total_unconnected,
        "excluded_count": pruned["total_excluded"],
        "by_type": by_type,
    }


# =============================================================
# Phase 4: PDF / SVG export (L2 visual)
# =============================================================

def phase4_export_visuals(pcb_path: str) -> Dict:
    print("\n=== Phase 4: PDF / SVG export ===")
    pcb = Path(pcb_path)
    pdf_path = pcb.with_suffix(".pdf")
    svg_path = pcb.with_suffix(".svg")

    layers = "F.Cu,B.Cu,F.SilkS,F.Fab,Edge.Cuts"

    pdf_cmd = [KICAD_CLI, "pcb", "export", "pdf", "-o", str(pdf_path),
               "--layers", layers, str(pcb)]
    pdf_ok = subprocess.run(pdf_cmd, capture_output=True, text=True, timeout=60).returncode == 0

    svg_cmd = [KICAD_CLI, "pcb", "export", "svg", "-o", str(svg_path),
               "--layers", layers, str(pcb)]
    svg_ok = subprocess.run(svg_cmd, capture_output=True, text=True, timeout=60).returncode == 0

    if pdf_ok:
        print(f"  ✓ PDF: {pdf_path.name}")
    if svg_ok:
        print(f"  ✓ SVG: {svg_path.name}")

    return {
        "ok": pdf_ok or svg_ok,
        "pdf_path": str(pdf_path) if pdf_ok else None,
        "svg_path": str(svg_path) if svg_ok else None,
        "next_step": "Claude must Read PDF for L2 visual verification (placement sanity)",
    }


# =============================================================
# Phase 5: EMC analysis (optional)
# =============================================================

def phase5_emc(preflight: Dict, pcb_path: str, skip: bool = False) -> Dict:
    print("\n=== Phase 5: EMC analysis ===")
    if skip:
        print("  ⏭ Skipped (--with-emc to enable)")
        return {"ok": True, "skipped": True, "reason": "opt-in only"}

    if not all(s.exists() for s in [EMC_SCRIPT, KICAD_ANALYZE_SCH, KICAD_ANALYZE_PCB]):
        print("  ⏭ Skipped: emc/kicad skill scripts not found")
        return {"ok": True, "skipped": True, "reason": "emc/kicad skill not found"}

    sch_path = preflight["sch_path"]
    analysis_dir = Path(pcb_path).parent / "_analysis"
    analysis_dir.mkdir(exist_ok=True)

    sch_json = analysis_dir / "sch_analysis.json"
    proc = subprocess.run([VENV_PYTHON, str(KICAD_ANALYZE_SCH), sch_path,
                           "--output", str(sch_json)],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not sch_json.exists():
        return {"ok": True, "skipped": True,
                "reason": f"sch analyzer failed: {proc.stderr[:200] or 'no output file'}"}

    pcb_json = analysis_dir / "pcb_analysis.json"
    proc = subprocess.run([VENV_PYTHON, str(KICAD_ANALYZE_PCB), pcb_path,
                           "--full",
                           "--output", str(pcb_json)],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not pcb_json.exists():
        return {"ok": True, "skipped": True,
                "reason": f"pcb analyzer failed: {proc.stderr[:200] or 'no output file'}"}

    emc_json = analysis_dir / "emc_report.json"
    proc = subprocess.run([VENV_PYTHON, str(EMC_SCRIPT),
                           "--schematic", str(sch_json),
                           "--pcb", str(pcb_json),
                           "--output", str(emc_json),
                           "--stage", "layout",
                           "--standard", "fcc-class-b"],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"ok": True, "skipped": True,
                "reason": f"emc analyzer failed: {proc.stderr[:200]}"}

    if emc_json.exists():
        emc_data = json.loads(emc_json.read_text())
        findings = emc_data.get("findings", [])
        by_severity = {}
        for f in findings:
            sev = f.get("severity", "info")
            by_severity[sev] = by_severity.get(sev, 0) + 1
        print(f"  ✓ EMC: {len(findings)} findings, by severity: {by_severity}")
        return {
            "ok": True,
            "emc_json": str(emc_json),
            "findings_count": len(findings),
            "by_severity": by_severity,
        }
    return {"ok": True, "skipped": True, "reason": "emc output missing"}


# =============================================================
# Phase 6: Design review PDF (optional)
# =============================================================

def phase6_design_review(preflight: Dict, pcb_path: str, skip: bool = False) -> Dict:
    """Generate a design review markdown + PDF using release umbrella's kidoc
    sub-module, reusing analysis JSONs already produced by Phase 5 EMC."""
    print("\n=== Phase 6: Design review document (release/scripts/kidoc) ===")
    if skip:
        print("  ⏭ Skipped")
        return {"ok": True, "skipped": True}

    if not (KIDOC_SCAFFOLD.exists() and KIDOC_GENERATE.exists()):
        print("  ⏭ Skipped: release kidoc scripts not found")
        return {"ok": True, "skipped": True, "reason": "release kidoc scripts missing"}

    pcb = Path(pcb_path)
    out_dir = pcb.parent / "_docs"
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / "design_review.md"

    analysis_dir = pcb.parent / "_analysis"
    sch_json = analysis_dir / "sch_analysis.json"
    pcb_json = analysis_dir / "pcb_analysis.json"
    emc_json = analysis_dir / "emc_report.json"

    cmd_scaffold = [
        VENV_PYTHON, str(KIDOC_SCAFFOLD),
        "--project-dir", str(pcb.parent),
        "--type", "design_review",
        "--output", str(md_path),
    ]
    if sch_json.exists():
        cmd_scaffold += ["--schematic-json", str(sch_json)]
    if pcb_json.exists():
        cmd_scaffold += ["--pcb-json", str(pcb_json)]
    if emc_json.exists():
        cmd_scaffold += ["--emc-json", str(emc_json)]

    try:
        proc = subprocess.run(cmd_scaffold, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": True, "skipped": True, "reason": "scaffold timed out"}

    if proc.returncode != 0 or not md_path.exists():
        print(f"  ⏭ scaffold failed: {proc.stderr[:300] or 'no output'}")
        return {"ok": True, "skipped": True, "reason": "scaffold failed"}

    cmd_pdf = [
        VENV_PYTHON, str(KIDOC_GENERATE),
        "--project-dir", str(pcb.parent),
        "--format", "pdf",
        "--doc", str(md_path),
    ]
    try:
        subprocess.run(cmd_pdf, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": True, "md_path": str(md_path),
                "pdf_failed": "generate timed out"}

    pdf_path = md_path.with_suffix(".pdf")
    if pdf_path.exists():
        print(f"  ✓ Design review PDF: {pdf_path.name}")
    else:
        print(f"  ✓ Markdown only (PDF generation failed)")

    return {
        "ok": True,
        "md_path": str(md_path),
        "pdf_path": str(pdf_path) if pdf_path.exists() else None,
    }


# =============================================================
# Main
# =============================================================

def run_pipeline(project_dir: Path, with_emc: bool = False,
                 with_design_review: bool = False) -> Dict:
    print(f"draw-pcb pipeline: {project_dir}")

    phases = {}
    phases["preflight"] = phase0_preflight(project_dir)
    if not phases["preflight"]["ok"]:
        return _finalize(phases, all_ok=False)

    phases["sch_to_pcb"] = phase1_sch_to_pcb(phases["preflight"])
    if not phases["sch_to_pcb"]["ok"]:
        return _finalize(phases, all_ok=False)
    pcb_path = phases["sch_to_pcb"]["pcb_path"]

    phases["placement"] = phase2_placement(phases["preflight"], pcb_path)
    if not phases["placement"]["ok"]:
        return _finalize(phases, all_ok=False)

    # Phase 2.7: catch pads pushed into slot or wrong-side after layout.
    phases["slot_clearance"] = phase2_7_check_slot(pcb_path)

    # Phase 2.75: pad-pad clearance sweep — fixes shorts caused by slot
    # moves and tight-snap pair placement.
    phases["pad_conflicts"] = phase2_75_pad_conflicts(pcb_path)
    phases["ensure_in_board"] = phase2_77_ensure_in_board(pcb_path)
    # Second pad sweep — moving footprints inward can create new clashes.
    phases["pad_conflicts_2"] = phase2_75_pad_conflicts(pcb_path)

    # Phase 2.8: GND copper zones on B.Cu.
    phases["ground_zones"] = phase2_8_ground_zones(pcb_path)

    phases["drc"] = phase3_drc(pcb_path)
    phases["visuals"] = phase4_export_visuals(pcb_path)
    # EMC + design-review default OFF — deep checking belongs to check-pcb,
    # not the draw-pcb generation pipeline. Opt in via --with-emc /
    # --with-design-review when you want them inline.
    phases["emc"] = phase5_emc(phases["preflight"], pcb_path, skip=not with_emc)
    phases["design_review"] = phase6_design_review(
        phases["preflight"], pcb_path, skip=not with_design_review
    )

    all_ok = all(p.get("ok", False) for p in phases.values())
    return _finalize(phases, all_ok=all_ok)


def _finalize(phases: Dict, all_ok: bool) -> Dict:
    print("\n" + "=" * 60)
    if all_ok:
        print("✓ draw-pcb pipeline completed")
    else:
        print("✗ draw-pcb pipeline finished with errors")

    next_step = ("Route it: Phase E auto-route (scripts/tools/route.py, then "
                 "re-pour zones, then run_drc) or hand-route in the KiCad GUI. "
                 "Re-run DRC + visuals afterwards.")
    return {"ok": all_ok, "phases": phases, "next_step": next_step}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="draw-pcb pipeline (placement + zones + DRC)")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--with-emc", action="store_true",
                        help="Run EMC analysis inline (default off — belongs to check-pcb)")
    parser.add_argument("--with-design-review", action="store_true",
                        help="Generate design-review document inline "
                             "(default off — belongs to release / check-pcb)")
    args = parser.parse_args()

    result = run_pipeline(args.project_dir, args.with_emc, args.with_design_review)

    if "phases" in result and "placement" in result["phases"]:
        result["phases"]["placement"].pop("placements", None)

    print("\n--- JSON ---")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["ok"] else 1)
