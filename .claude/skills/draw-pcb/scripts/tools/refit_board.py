#!/usr/bin/env python3
"""draw-pcb toolbox tool: refit_board

Phase D finishing step — shrink the Edge.Cuts outline to hug the actual
placement. The board outline is sized once at init_layout from CLAUDE.md
pack_density; after the agentic loop compacts components, that outline is
stale (too loose). This re-fits the outer rectangle to the real footprint
extent + margin and redraws the isolation slot continuous at its existing x.

Run order in Phase D: refit_board -> bridge_slot -> add_zones -> check_zones -> run_drc
(refit must precede bridge_slot + add_zones — both read Edge.Cuts).

Output JSON includes `fill_ratio` — the compactness metric: summed footprint
bounding box (pads + graphics, text excluded) / board area. NOT courtyard area:
most library footprints ship no CrtYd graphics at all, so a courtyard-based
ratio would silently mean "pad area" on those boards and change meaning from
one library to the next. A very low fill_ratio means the placement is still too
spread out; tighten it in the loop before refitting.

⚠ This REWRITES Edge.Cuts. When the outline is fixed by something outside this
skill (enclosure, chassis, customer spec), pass --keep-outline: the outline is
left alone and the tool only reports fill_ratio against it, writing nothing.

Usage:
  refit_board.py <board.kicad_pcb> [--margin 2.5] [--keep-outline]

Wraps _kicad_python_helper.py's refit_board mode (needs KiCad's pcbnew).
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
    ap = argparse.ArgumentParser(description="Re-fit Edge.Cuts to placement")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--margin", type=float, default=2.5,
                    help="board edge margin around footprint extent, mm")
    ap.add_argument("--keep-outline", action="store_true",
                    help="do NOT touch Edge.Cuts — just report fill_ratio "
                         "against the outline already on the board. Use when "
                         "an enclosure / chassis / customer spec owns it.")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    result = call_helper({
        "mode": "refit_board",
        "pcb_path": str(args.pcb),
        "output_pcb": str(args.pcb),
        "margin_mm": args.margin,
        "keep_outline": args.keep_outline,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
