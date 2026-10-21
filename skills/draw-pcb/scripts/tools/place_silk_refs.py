#!/usr/bin/env python3
"""draw-pcb toolbox tool: place_silk_refs

Greedy placement of every visible reference designator.

Why: set_silk_spec sets character size to the fab's spec; nothing else in the
toolbox decides WHERE each designator sits, and fab-height text on a dense
board lands on pads and neighbouring parts — while tidy designators are a
common review / scoring item.

Each designator tries a ring of candidate spots around its part's body
(8 directions × several gaps, horizontal and rotated 90°, plus dead centre on
bodies much larger than the text) and keeps the cheapest by weighted overlap:

  off-board            fatal     — silk outside Edge.Cuts gets clipped
  over an exposed pad  50 /mm²   — ink on a pad degrades the solder joint
  over another ref     10 /mm²   — designators on top of each other
  over other silk       8 /mm²   — designator on another part's outline
  over a body           1 /mm²   — cosmetic
  distance from body    --w-dist /mm — far-parked designators read ambiguous

Run AFTER set_silk_spec (text size changes the boxes being packed) and BEFORE
run_drc. Judge the result by the DRC silk_overlap / silk_over_copper counts
before vs after — the greedy cost minimises overlap, it cannot guarantee zero
on a dense board.

Usage:
  place_silk_refs.py <board.kicad_pcb> [--passes N] [--gaps MM ...]
                     [--w-dist X] [--no-rotate] [-o OUT]

Output JSON: {ok, output_pcb, refs_placed, hidden_skipped, passes, gaps_mm,
next}

Wraps _kicad_python_helper.py's place_silk_refs mode (needs KiCad's pcbnew).
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
    ap = argparse.ArgumentParser(
        description="Greedy silkscreen reference-designator placement")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--passes", type=int, default=4, metavar="N",
                    help="re-optimisation passes (default 4) — a designator "
                         "placed early cannot see the ones placed after it, "
                         "later passes fix that")
    ap.add_argument("--gaps", type=float, nargs="+", metavar="MM",
                    help="candidate gaps between body and text, mm "
                         "(default 0.15 0.3 0.5 0.8 1.2)")
    ap.add_argument("--w-dist", type=float, default=3.0, metavar="X",
                    help="cost per mm of distance from the body (default "
                         "3.0); raise to keep designators tighter to their "
                         "parts, lower to let them escape dense areas")
    ap.add_argument("--no-rotate", action="store_true",
                    help="forbid 90° designators (default allowed — rotated "
                         "text fits narrow gaps between parts)")
    ap.add_argument("--edge-margin", type=float, default=0.5, metavar="MM",
                    help="keep-out from Edge.Cuts, mm (default 0.5) — text "
                         "inside the outline but closer than the fab's "
                         "silk-to-edge clearance trades silk_overlap for "
                         "silk_edge_clearance; match this to that clearance")
    ap.add_argument("-o", "--output", metavar="OUT",
                    help="write here instead of in place")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    result = call_helper({
        "mode": "place_silk_refs",
        "pcb_path": str(args.pcb),
        "output_pcb": str(args.output or args.pcb),
        "passes": args.passes,
        "gaps_mm": args.gaps,
        "w_dist": args.w_dist,
        "allow_rotate": not args.no_rotate,
        "edge_margin_mm": args.edge_margin,
    }, timeout=600)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
