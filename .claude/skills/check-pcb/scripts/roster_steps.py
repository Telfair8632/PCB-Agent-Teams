#!/usr/bin/env python3
"""roster_steps.py - data-availability roster for the check-pcb workflow.

Measures the project MECHANICALLY (which input files exist, which tools
are installed) and prints per-step READY / NO-DATA / TOOL-GATED with
reasons. The script is authoritative for DATA availability; whether the
user asked for a step (semantic scope) stays with the model. NO-DATA is
an unlock instruction first, a skip license second - the cross-domain
trio (Steps 2/4/5) must be unblocked, not skipped (SKILL.md 红线).

Rationale: "which steps have data to eat" is pure measurement; leaving
it to the model risks silent skips (e.g. declaring fab-ready without
noticing schematic.json was never produced).

Usage: python3 roster_steps.py <project_root>
       <project_root> = dir containing kicad/ and analysis/ (a Projects/<name>/)
Exit:  0 = roster printed; 2 = project_root unreadable.
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
import socket
import sys
from pathlib import Path

EXCLUDED_PARTS = {".history", "backups", "_autosave", "__pycache__"}


def excluded(p: Path, base: Path) -> bool:
    # KiCad noise: _autosave-*.kicad_pcb prefixes, <proj>-backups/ dirs.
    # Judge only the path BELOW base - an ancestor dir named "X-backups"
    # must not zero out the whole roster.
    try:
        parts = p.relative_to(base).parts
    except ValueError:
        # dead branch for current callers (all paths come from base.glob);
        # a foreign path would fall back to absolute parts = ancestor false hits
        parts = p.parts
    return any(part in EXCLUDED_PARTS or part.startswith("_autosave")
               or part.endswith("-backups") for part in parts)


def newest(paths, base: Path):
    paths = [p for p in paths if not excluded(p, base)]
    return max(paths, key=lambda p: p.stat().st_mtime, default=None)


def find_run(analysis: Path, filename: str):
    """Newest analysis/**/<filename>, returns (path, run_label)."""
    if not analysis.is_dir():
        return None, None
    p = newest(analysis.glob(f"**/{filename}"), analysis)
    if not p:
        return None, None
    return p, (p.parent.name if p.parent != analysis else "(analysis 顶层)")


def has_network(timeout=1.5) -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=timeout).close()
        return True
    except OSError:
        return False


def sch_has_mpn(sch_json: Path) -> bool:
    try:
        return '"mpn"' in sch_json.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def measure(root: Path) -> dict:
    analysis = root / "analysis"
    sch, sch_run = find_run(analysis, "schematic.json")
    # prefer the pcb.json of the SAME run as schematic.json; else newest anywhere
    if sch and (sch.parent / "pcb.json").is_file():
        pcb_json, pcb_run = sch.parent / "pcb.json", sch_run
    else:
        pcb_json, pcb_run = find_run(analysis, "pcb.json")
    gerber_dirs = [d for d in root.glob("**/gerbers")
                   if d.is_dir() and any(f.is_file() for f in d.rglob("*"))
                   and not excluded(d, root)]
    return {
        "pcb": newest(root.glob("**/*.kicad_pcb"), root),
        "sch": sch, "sch_run": sch_run,
        "pcb_json": pcb_json, "pcb_run": pcb_run,
        "gerbers": newest(gerber_dirs, root),
        "ngspice": shutil.which("ngspice") is not None,
        "network": has_network(),
        "mpn": sch_has_mpn(sch) if sch else False,
    }


def roster(m: dict) -> list:
    """Return [(state, step, reason)] for Steps 0-8."""
    rows = []
    pcb_ok = m["pcb"] is not None
    sch_ok = m["sch"] is not None
    # pcb.json is Step 1's product; "producible" is enough for Steps 2/4/5.
    pcb_json_ok = pcb_ok or m["pcb_json"] is not None
    cross_reason_bad = ("缺 schematic.json → 先跑 check-schematic 的 analyze_schematic.py（非可跳步）"
                        if not sch_ok else "缺 .kicad_pcb，pcb.json 无从产出")
    # compare run DIRS, not basename labels (analysis/a/run1 vs analysis/b/run1)
    cross_run_warn = ("" if not m["pcb_json"] or not m["sch"]
                      or m["pcb_json"].parent == m["sch"].parent
                      else f"；⚠ 现存 pcb.json 来自另一 run {m['pcb_run']}，用 Step 1 重产对齐")

    rows.append(("READY" if sch_ok else "NO-DATA", "Step 0 schematic.json",
                 str(m["sch"]) if sch_ok
                 else "缺 → 先跑 check-schematic 的 analyze_schematic.py"))
    rows.append(("READY" if pcb_ok else "NO-DATA", "Step 1 analyze_pcb --full",
                 str(m["pcb"]) if pcb_ok else "项目里找不到 .kicad_pcb → 先用 draw-pcb 画板"))
    for name in ("Step 2 cross_analysis", "Step 4 analyze_thermal", "Step 5 analyze_emc"):
        ok = sch_ok and pcb_json_ok
        why = (f"schematic.json (run {m['sch_run']}) + pcb.json"
               + ("" if m["pcb_json"] else "（Step 1 现产）") + cross_run_warn
               if ok else cross_reason_bad)
        if ok and name.endswith("emc") and not m["ngspice"]:
            why += "；ngspice 未装 → 不能加 --spice-enhanced"
        rows.append(("READY" if ok else "NO-DATA", name, why))
    rows.append(("READY" if m["gerbers"] else "NO-DATA", "Step 3 analyze_gerbers",
                 str(m["gerbers"]) if m["gerbers"]
                 else "无 gerbers/ → 先跑 release/scripts/export_gerbers.py <pcb>"))
    # SPICE half consumes schematic.json (simulate_subcircuits.py), hence sch_ok too
    if not (pcb_json_ok and sch_ok):
        rows.append(("NO-DATA", "Step 6 extract_parasitics+SPICE", cross_reason_bad))
    elif not m["ngspice"]:
        rows.append(("TOOL-GATED", "Step 6 extract_parasitics+SPICE", "ngspice 未装"))
    else:
        rows.append(("READY", "Step 6 extract_parasitics+SPICE",
                     "工具齐；是否需要（高阻反馈/LC/RF/长模拟线）由模型判断"))
    if not sch_ok:
        rows.append(("NO-DATA", "Step 7 lifecycle_audit",
                     "BOM 来自 schematic.json，缺 → 先跑 check-schematic"))
    elif not m["mpn"]:
        rows.append(("NO-DATA", "Step 7 lifecycle_audit",
                     "schematic.json 未见 MPN 字段（工作流：联网 + MPN 时才跑）"))
    elif not m["network"]:
        rows.append(("TOOL-GATED", "Step 7 lifecycle_audit", "无网络"))
    else:
        rows.append(("READY", "Step 7 lifecycle_audit", "有网络，schematic.json 含 MPN"))
    rows.append(("READY", "Step 8 design review", "总是跑（report-generation.md checklist）"))
    rows.sort(key=lambda r: r[1])
    return rows


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: roster_steps.py <project_root>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).expanduser()
    if not root.is_dir():
        print(f"[roster] ERROR: {root} is not a directory", file=sys.stderr)
        return 2
    m = measure(root)
    print(f"[roster] target: {root}")
    print(f"[roster] measured: pcb={'yes' if m['pcb'] else 'MISSING'}  "
          f"sch_json={m['sch_run'] or 'MISSING'}  pcb_json={m['pcb_run'] or 'none(Step1产)'}  "
          f"gerbers={'yes' if m['gerbers'] else 'none'}  ngspice={m['ngspice']}  "
          f"network={m['network']}  mpn={m['mpn']}")
    rows = roster(m)
    for state, step, why in rows:
        print(f"[roster] {state:<10} {step} — {why}")
    ready = sum(1 for s, _, _ in rows if s == "READY")
    print(f"[roster] {ready}/{len(rows)} steps have data. NO-DATA 先按理由解锁"
          f"（跨域三项 cross/thermal/EMC 非可跳步）；解锁不了或用户未要求，"
          f"才把上面理由抄进收尾总结的 ⏭ 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
