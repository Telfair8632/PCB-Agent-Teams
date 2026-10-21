#!/usr/bin/env python3
"""draw-pcb toolbox tool: check_zones

Isolation-barrier copper-pour validator. Asserts that no GND (or any) copper
zone bridges the isolation barrier — i.e. a single net's FILLED copper must
not appear on both sides of the milled isolation slot. A bridging zone shorts
the two ground domains across the barrier, which voids galvanic isolation and
fails certification. DRC only catches this indirectly via clearance; this is
a direct, net-agnostic geometric check.

Run AFTER add_ground_zones (zones must be filled), before / alongside run_drc.

Method (see mode_validate_zones in _kicad_python_helper.py):
  1. Detect the slot band [slot_left, slot_right] from interior vertical
     Edge.Cuts lines.
  2. For each filled zone, take its filled-copper X extent. If it has copper
     at x < slot_left AND at x > slot_right, it bridges the barrier -> error.

Net-agnostic / voltage-agnostic / name-agnostic — works for any project with
a vertical isolation slot. Horizontal / multi-segment barriers are not
detected (slot_detected=False -> check skipped, never a false pass).

Usage:
  check_zones.py <board.kicad_pcb> [--tol 0.05]

Exit code 0 = pass (or skipped, no slot); 1 = a zone bridges the barrier or
the helper errored. Output JSON: {ok, slot_detected, zones_checked,
crossings[], verdict}.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _kicad import call_helper  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Validate that no copper zone bridges the isolation barrier")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--tol", type=float, default=0.05,
                    help="slack (mm) added to each slot edge before judging a "
                         "crossing (default 0.05)")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    result = call_helper({
        "mode": "validate_zones",
        "pcb_path": str(args.pcb),
        "tol_mm": args.tol,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result.get("ok"):
        return 1
    # A non-empty crossings list is a hard failure even though ok=True.
    return 1 if result.get("crossings") else 0


if __name__ == "__main__":
    sys.exit(main())
