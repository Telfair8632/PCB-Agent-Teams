#!/usr/bin/env python3
"""draw-pcb toolbox tool: set_silk_spec

Normalise silkscreen character height and line width across the whole board.

Why: fabs state a minimum silk line width and character height — below it the
screen print smears or drops out — and KiCad takes each footprint's text size
from whatever library that part came from, so an untouched board carries a
handful of different sizes. This sets one spec everywhere.

Reference designators land on the silk layer of the side the part is on. Values
are left alone unless asked: they usually hold an MPN, which buys nothing on the
board while crowding out the refdes (see references/known_issues.md on printing
electrical values instead of distributor codes).

Run this near the END of Phase D — after placement is final. Two consequences
to plan for, both expected rather than defects:
  * every touched part now differs from its library, so DRC reports
    `lib_footprint_issues` for each one. Triage that class, do not chase it.
  * bigger text overlaps more, so `silk_overlap` / `silk_over_copper` rise.
    That IS a real readability problem — fix it by moving text, not by
    abandoning the spec.

Usage:
  set_silk_spec.py <board.kicad_pcb> [--mil] [--height X] [--thickness X]
                   [--include-values | --hide-values]

  --mil  height / thickness are in mil rather than mm (fab specs often are)

Output JSON: {ok, height_mm, thickness_mm, references_set, values_touched,
board_texts_set, expect}

Wraps _kicad_python_helper.py's set_silk_spec mode (needs KiCad's pcbnew).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _kicad import call_helper  # noqa: E402

MIL_MM = 0.0254


def main() -> int:
    ap = argparse.ArgumentParser(description="Set board-wide silkscreen text spec")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--mil", action="store_true",
                    help="--height / --thickness are in mil, not mm")
    ap.add_argument("--height", type=float, default=None,
                    help="character height")
    ap.add_argument("--thickness", type=float, default=None,
                    help="stroke line width")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--include-values", action="store_true",
                    help="apply the spec to Value text too (default: leave it)")
    grp.add_argument("--hide-values", action="store_true",
                    help="hide Value text so refdes has the silk to itself")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1
    if args.height is None and args.thickness is None and not args.hide_values:
        print(json.dumps({"ok": False, "error":
                          "nothing to set — pass --height and/or --thickness "
                          "(or --hide-values)"}))
        return 1

    k = MIL_MM if args.mil else 1.0
    result = call_helper({
        "mode": "set_silk_spec",
        "pcb_path": str(args.pcb),
        "output_pcb": str(args.pcb),
        "height_mm": args.height * k if args.height is not None else None,
        "thickness_mm": args.thickness * k if args.thickness is not None else None,
        "include_values": args.include_values,
        "hide_values": args.hide_values,
    }, timeout=120)
    result["units_in"] = "mil" if args.mil else "mm"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
