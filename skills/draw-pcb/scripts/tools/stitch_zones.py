#!/usr/bin/env python3
"""draw-pcb toolbox tool: stitch_zones

Tie one net's copper pours on two layers together with vias. Run AFTER routing
and AFTER add_zones — the pours have to exist and be filled before there is
anything to stitch, and every track laid later re-cuts them.

Why it matters on a 2-layer board: each back-side track slices the back pour
into islands, so return current under a signal keeps hunting for a way across
(EMC reference-plane gaps), and a front-side thermal pad has nothing carrying
heat or return current to the other side. Stitching vias answer both.

ONE net per call, named explicitly. Stitching every GND-like net at once would
happily bridge an isolation barrier and destroy the separation it exists for —
on a board with HV_GND / LV_GND, run it once per side.

Usage:
  stitch_zones.py <board.kicad_pcb> [--net GND] [--pitch 5.0]
                  [--layers F.Cu B.Cu] [--via-dia 0.6] [--drill 0.3]
                  [--clearance 0.2] [--min-sep 2.0] [--hole-to-hole 0.5]
                  [--thermal-pad-vias MIN_AREA_MM2]

  --pitch              grid spacing for candidate vias, mm
  --thermal-pad-vias   also drop vias inside SMD pads on this net at least
                       this many mm² (0 / omitted = off). The pour test cannot
                       find these on its own: a pad is not pour copper.

Placement rule: a candidate is kept only where BOTH pours are filled after
shrinking each by (via radius + clearance), so a via can never touch foreign
copper. Holes are judged separately against hole-to-hole spacing — a plated
pad sits inside the pour, so a copper-only test would drill straight into it.

Output JSON: {ok, stitch_vias_added, thermal_pad_vias_added,
candidates_rejected{}, net, layers, pitch_mm}

Wraps _kicad_python_helper.py's stitch_zones mode (needs KiCad's pcbnew).
"""

# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _kicad import call_helper  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Stitch a net's pours with vias")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--net", default="GND",
                    help="net whose pours to stitch (default GND). One net per "
                         "call — never auto-stitch across an isolation barrier")
    ap.add_argument("--layers", nargs=2, default=["F.Cu", "B.Cu"],
                    metavar=("LAYER_A", "LAYER_B"))
    ap.add_argument("--pitch", type=float, default=5.0,
                    help="candidate grid spacing mm (default 5.0)")
    ap.add_argument("--via-dia", type=float, default=0.6,
                    help="via outer diameter mm (default 0.6)")
    ap.add_argument("--drill", type=float, default=0.3,
                    help="via drill diameter mm (default 0.3)")
    ap.add_argument("--clearance", type=float, default=0.2,
                    help="copper clearance the via must respect, mm")
    ap.add_argument("--min-sep", type=float, default=2.0,
                    help="keep this far from vias already on the board, mm")
    ap.add_argument("--hole-to-hole", type=float, default=0.5,
                    help="edge-to-edge drill spacing to respect, mm — this is "
                         "what keeps stitching out of existing THT holes")
    ap.add_argument("--thermal-pad-vias", type=float, default=0.0,
                    metavar="MIN_AREA_MM2",
                    help="also via SMD pads on this net at least this big "
                         "(0 = off)")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    result = call_helper({
        "mode": "stitch_zones",
        "pcb_path": str(args.pcb),
        "output_pcb": str(args.pcb),
        "net": args.net,
        "layers": args.layers,
        "pitch_mm": args.pitch,
        "via_dia_mm": args.via_dia,
        "drill_mm": args.drill,
        "clearance_mm": args.clearance,
        "min_sep_mm": args.min_sep,
        "hole_to_hole_mm": args.hole_to_hole,
        "thermal_pad_min_mm2": args.thermal_pad_vias,
    }, timeout=180)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
