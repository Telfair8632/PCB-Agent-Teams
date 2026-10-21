#!/usr/bin/env python3
"""draw-pcb toolbox tool: move

The atomic action for AI-driven placement — move and/or rotate one or more
footprints to absolute targets. The target (x, y) is where the footprint's
body-bbox CENTRE lands, matching the `center` field get_geometry reports, so
"read center → decide new center → move" is a consistent loop.

Only footprint positions change. Edge.Cuts, tracks and zones are untouched —
safe to call repeatedly inside a placement loop.

Usage:
  move.py <board.kicad_pcb> --move "R1:42.0,18.0,90" --move "C8:50,20" ...
  move.py <board.kicad_pcb> --moves-json moves.json   # {"R1":[42,18,90],...}

  rot is optional; omit it to keep the footprint's current rotation.

Output JSON: {ok, moved[], not_found[]}.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _kicad import call_helper  # noqa: E402


def _parse_move(s: str) -> tuple[str, list]:
    """'R1:42.0,18.0,90' → ('R1', [42.0, 18.0, 90.0])."""
    ref, _, coords = s.partition(":")
    ref = ref.strip()
    if not ref or not coords:
        raise ValueError(f"bad --move '{s}' (want 'REF:x,y[,rot]')")
    parts = [p.strip() for p in coords.split(",")]
    if len(parts) not in (2, 3):
        raise ValueError(f"bad --move '{s}' (want 'REF:x,y[,rot]')")
    vals = [float(parts[0]), float(parts[1])]
    vals.append(float(parts[2]) if len(parts) == 3 else None)
    return ref, vals


def main() -> int:
    ap = argparse.ArgumentParser(description="Move/rotate footprints")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--move", action="append", default=[],
                    metavar="REF:x,y[,rot]", help="one footprint move (repeatable)")
    ap.add_argument("--moves-json", help="JSON file: {ref: [x,y,rot], ...}")
    ap.add_argument("-o", "--out", help="write to a different .kicad_pcb")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    moves: dict[str, list] = {}
    try:
        for m in args.move:
            ref, vals = _parse_move(m)
            moves[ref] = vals
        if args.moves_json:
            raw = json.loads(Path(args.moves_json).read_text(encoding="utf-8"))
            for ref, v in raw.items():
                moves[ref] = [v[0], v[1], v[2] if len(v) > 2 else None]
    except (ValueError, OSError, json.JSONDecodeError, IndexError) as e:
        print(json.dumps({"ok": False, "error": f"bad moves input: {e}"}))
        return 1

    if not moves:
        print(json.dumps({"ok": False, "error": "no moves given"}))
        return 1

    result = call_helper({
        "mode": "move_footprints",
        "pcb_path": str(args.pcb),
        "output_pcb": str(args.out) if args.out else str(args.pcb),
        "moves": moves,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
