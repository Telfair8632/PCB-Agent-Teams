#!/usr/bin/env python3
"""draw-pcb toolbox tool: bridge_slot

Phase D finishing step — re-draw the isolation slot(s) leaving a solid PCB
bridge under every barrier device that straddles the slot.

Why: a continuous milled isolation slot is wider than the pad pitch of a
through-hole barrier device (e.g. a SIP isolated DC-DC), so the slot cuts
through its pads → DRC copper_edge_clearance, and the device can't be
soldered. The device itself provides the isolation there, so a bridge under
its body is the correct fix. Run after placement has converged, before
run_drc.

Barrier devices are detected via placement_brief (a barrier device bridges
two ground nets). Run this AFTER placement is final — the bridge is placed
at each barrier device's final position.

Usage:
  bridge_slot.py <board.kicad_pcb> [--margin 1.0]

Output JSON: {ok, slot_x_mm, bridges[], slot_segments_drawn}.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _kicad import call_helper          # noqa: E402
from placement_brief import build_brief  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Bridge isolation slot under "
                                             "barrier devices")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--margin", type=float, default=1.0,
                    help="bridge margin around the device body, mm")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    try:
        brief = build_brief(str(args.pcb))
        barrier_refs = [b["ref"] for b in brief.get("barrier_devices", [])]
    except Exception as e:  # noqa: BLE001 - tool boundary
        print(json.dumps({"ok": False,
                          "error": f"placement_brief failed: {e}"}))
        return 1

    if not barrier_refs:
        print(json.dumps({"ok": True, "skipped": True,
                          "reason": "no barrier devices — nothing to bridge"}))
        return 0

    result = call_helper({
        "mode": "bridge_slots",
        "pcb_path": str(args.pcb),
        "output_pcb": str(args.pcb),
        "barrier_refs": barrier_refs,
        "bridge_margin_mm": args.margin,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
