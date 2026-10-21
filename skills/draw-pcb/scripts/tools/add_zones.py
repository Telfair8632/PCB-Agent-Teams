#!/usr/bin/env python3
"""draw-pcb toolbox tool: add_zones

Add a GND copper pour. Which layers / which ground nets to pour is a
per-layer × per-region judgment — see references/copper_pour.md. Call once
per (layer-set, net-set): the helper is idempotent per (net, layer), so
e.g. `--layers B.Cu` then `--layers F.Cu --nets LV_GND` compose cleanly.
Run again after routing (Phase E) so copper re-flows around traces/vias.

Usage:
  add_zones.py <board.kicad_pcb> [--layers B.Cu] [--nets LV_GND ...]
               [--clearance 0.3] [--rect XMIN YMIN XMAX YMAX]
               [--pad-connect {thermal,solid,none}]

Wraps _kicad_python_helper.py's add_ground_zones mode (needs KiCad's pcbnew).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _kicad import call_helper  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Add GND copper pour")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--layers", default="B.Cu",
                    help="comma-separated copper layers (default B.Cu)")
    ap.add_argument("--nets", nargs="+",
                    help="restrict to ground nets containing these substrings "
                         "(e.g. LV_GND); default: all GND-like nets")
    ap.add_argument("--clearance", type=float, default=0.3,
                    help="zone clearance mm (default 0.3)")
    ap.add_argument("--rect", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    help="confine the pour to this rect (mm), intersected with "
                         "the per-net/slot rect. A zone has one clearance, which "
                         "cannot express per-voltage creepage — keep the pour off "
                         "HV areas with this instead of widening --clearance")
    ap.add_argument("--pad-connect", choices=["thermal", "solid", "none"],
                    default="thermal",
                    help="how the pour meets pads. thermal (default) = spoked "
                         "relief, easy to hand-solder but each spoke needs "
                         "room, so crowded pads end up starved. solid = full "
                         "contact, no spokes to starve, but every pad becomes "
                         "a heat sink (fine for reflow, harder for rework)")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    result = call_helper({
        "mode": "add_ground_zones",
        "pcb_path": str(args.pcb),
        "ground_net_keywords": ["GND"],
        "net_filter": args.nets or None,
        "layers": [l.strip() for l in args.layers.split(",") if l.strip()],
        "clearance_mm": args.clearance,
        "rect_mm": args.rect,
        "pad_connect": args.pad_connect,
        "fill_now": True,
    }, timeout=90)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
