#!/usr/bin/env python3
"""draw-pcb toolbox tool: set_design_rules

Write the fab's design rules into the board's `.kicad_pro`, then echo back what
actually took effect.

Why this exists: `kicad-cli pcb drc` reads its rules from the `.kicad_pro`, NOT
from the `.kicad_pcb` setup block. A board whose project file still carries
default rules gets DRC'd against rules it was never designed to — the run looks
authoritative and every number in it is wrong. Same trap in the other
direction: rules looser than the fab's let a board pass here and fail there.

Two independent layers, both of which KiCad enforces:
  * global minimums (`board.design_settings.rules`) — absolute floors
  * per-netclass values (`net_settings.classes`)     — what tracks/vias get

A netclass value BELOW a global minimum makes DRC flag the whole board, so this
tool reports that conflict instead of quietly picking a winner.

Usage:
  set_design_rules.py <board.kicad_pcb> [--mil]
      [--min-track-width X] [--min-clearance X] [--min-via-diameter X]
      [--min-through-hole X] [--min-copper-edge-clearance X]
      [--min-hole-clearance X] [--min-hole-to-hole X] [--min-annular-width X]
      [--netclass NAME] [--track-width X] [--clearance X]
      [--via-diameter X] [--via-drill X]
      [--diff-pair-width X] [--diff-pair-gap X] [--nets PAT ...]

  --mil    every value above is in mil instead of mm (fab specs often are)

Output JSON: {ok, pro, units, changed{}, effective{}, conflicts[]}
Only the flags you pass are touched; everything else in the project is left
byte-for-byte alone.
"""
import argparse
import json
import sys
from pathlib import Path

MIL_MM = 0.0254

# CLI flag -> key in board.design_settings.rules
GLOBAL_RULES = {
    "min_track_width": "min_track_width",
    "min_clearance": "min_clearance",
    "min_via_diameter": "min_via_diameter",
    "min_through_hole": "min_through_hole_diameter",
    "min_copper_edge_clearance": "min_copper_edge_clearance",
    "min_hole_clearance": "min_hole_clearance",
    "min_hole_to_hole": "min_hole_to_hole",
    "min_annular_width": "min_via_annular_width",
}

# CLI flag -> key in a net_settings.classes[] entry
NETCLASS_VALUES = {
    "track_width": "track_width",
    "clearance": "clearance",
    "via_diameter": "via_diameter",
    "via_drill": "via_drill",
    "diff_pair_width": "diff_pair_width",
    "diff_pair_gap": "diff_pair_gap",
}

# netclass value -> the global minimum it must not fall below
FLOORS = {
    "track_width": "min_track_width",
    "clearance": "min_clearance",
    "via_diameter": "min_via_diameter",
    "via_drill": "min_through_hole_diameter",
    "diff_pair_width": "min_track_width",
    "diff_pair_gap": "min_clearance",
}


def set_design_rules(pcb_path: str, values: dict, netclass: str = "Default",
                     nets: list[str] | None = None,
                     to_mm: float = 1.0) -> dict:
    pro_path = Path(pcb_path).with_suffix(".kicad_pro")
    if not pro_path.exists():
        return {"ok": False, "error":
                f"no {pro_path.name} beside the board — DRC would run on KiCad "
                "defaults regardless of what this tool wrote. Create the "
                "project file first (create_pcb does), or copy it in."}

    try:
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"{pro_path.name} is not valid JSON: {e}"}

    changed: dict = {}
    rules = pro.setdefault("board", {}).setdefault(
        "design_settings", {}).setdefault("rules", {})
    for flag, key in GLOBAL_RULES.items():
        if values.get(flag) is None:
            continue
        mm = round(values[flag] * to_mm, 6)
        if rules.get(key) != mm:
            changed[f"rules.{key}"] = [rules.get(key), mm]
        rules[key] = mm

    net_settings = pro.setdefault("net_settings", {})
    classes = net_settings.setdefault("classes", [])
    wants_class = any(values.get(f) is not None for f in NETCLASS_VALUES) or nets
    target = None
    if wants_class:
        target = next((c for c in classes if c.get("name") == netclass), None)
        if target is None:
            # Clone Default so the new class inherits every field KiCad expects
            # rather than being a sparse dict it has to guess defaults for.
            base = next((c for c in classes if c.get("name") == "Default"), None)
            target = dict(base) if base else {"name": netclass}
            target["name"] = netclass
            target.pop("priority", None)
            classes.append(target)
            changed[f"classes.{netclass}"] = ["(created)", "cloned from Default"]
        for flag, key in NETCLASS_VALUES.items():
            if values.get(flag) is None:
                continue
            mm = round(values[flag] * to_mm, 6)
            if target.get(key) != mm:
                changed[f"classes.{netclass}.{key}"] = [target.get(key), mm]
            target[key] = mm

    if nets:
        # KiCad 8+ assigns nets to classes by pattern list.
        patterns = net_settings.setdefault("netclass_patterns", [])
        have = {(p.get("netclass"), p.get("pattern")) for p in patterns
                if isinstance(p, dict)}
        added = []
        for pat in nets:
            if (netclass, pat) not in have:
                patterns.append({"netclass": netclass, "pattern": pat})
                added.append(pat)
        if added:
            changed[f"netclass_patterns.{netclass}"] = ["(added)", added]

    # A netclass value under a global floor makes DRC flag the entire board.
    # Report it; do not pick a winner on the user's behalf.
    conflicts = []
    if target:
        for nc_key, floor_key in FLOORS.items():
            v = target.get(nc_key)
            floor = rules.get(floor_key)
            if v is None or floor is None:
                continue
            if v < floor - 1e-9:
                conflicts.append(
                    f"{netclass}.{nc_key}={v}mm is below "
                    f"{floor_key}={floor}mm — DRC will flag every "
                    f"{nc_key.replace('_', ' ')} on the board")

    if changed:
        pro_path.write_text(json.dumps(pro, indent=2), encoding="utf-8")

    # Read back from disk: the point of the tool is reporting what is in
    # effect, not what we believe we wrote.
    verify = json.loads(pro_path.read_text(encoding="utf-8"))
    v_rules = verify.get("board", {}).get("design_settings", {}).get("rules", {})
    v_class = next((c for c in verify.get("net_settings", {}).get("classes", [])
                    if c.get("name") == netclass), {})
    return {
        "ok": True,
        "pro": str(pro_path),
        "changed": changed or "nothing (values already matched)",
        "effective_mm": {
            "global": {k: v_rules.get(k) for k in GLOBAL_RULES.values()},
            netclass: {k: v_class.get(k) for k in NETCLASS_VALUES.values()},
        },
        "conflicts": conflicts,
        "next": "re-run run_drc.py — rules changed, so the old DRC output is stale",
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Write fab design rules into the board's .kicad_pro")
    ap.add_argument("pcb", help="path to .kicad_pcb (project file sits beside it)")
    ap.add_argument("--mil", action="store_true",
                    help="values are in mil, not mm")
    ap.add_argument("--netclass", default="Default",
                    help="netclass to write (created from Default if absent)")
    ap.add_argument("--nets", nargs="+", metavar="PAT",
                    help="net name patterns to assign to --netclass")
    for flag in list(GLOBAL_RULES) + list(NETCLASS_VALUES):
        ap.add_argument(f"--{flag.replace('_', '-')}", type=float, default=None,
                        dest=flag, metavar="X")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    values = {f: getattr(args, f)
              for f in list(GLOBAL_RULES) + list(NETCLASS_VALUES)}
    if all(v is None for v in values.values()) and not args.nets:
        print(json.dumps({"ok": False, "error":
                          "nothing to set — pass at least one rule flag "
                          "(--min-clearance, --track-width, ...) or --nets"}))
        return 1

    result = set_design_rules(args.pcb, values, netclass=args.netclass,
                              nets=args.nets,
                              to_mm=MIL_MM if args.mil else 1.0)
    result["units_in"] = "mil" if args.mil else "mm"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
