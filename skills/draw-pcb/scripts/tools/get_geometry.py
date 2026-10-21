#!/usr/bin/env python3
"""draw-pcb toolbox tool: get_geometry

Read a .kicad_pcb and emit per-footprint geometry as JSON — the perception
layer for AI-driven placement. Pure text parsing (s-expressions); no KiCad
binary, no pcbnew needed.

Output per footprint:
  ref, value, footprint (library id), x, y, angle, layer, type, pad_count,
  courtyard {min_x, min_y, max_x, max_y, w, h}   # real component extent
  pads [{number, x, y, w, h, net}]               # absolute board coords,
                                                 # w/h = board-space extent
                                                 # (rotation applied)
  nets [...]                                      # distinct net names touched

Usage:
  get_geometry.py <board.kicad_pcb> [--refs R1,C2,U1] [--no-pads]

Geometry extraction is delegated to check-pcb/analyze_pcb.py so this tool and
the PCB analyzer never drift apart.
"""
import argparse
import json
import sys
from pathlib import Path

# check-pcb scripts own the parser + footprint extractor. Reuse, don't fork.
# tools/ → scripts/ → draw-pcb/ → skills/  (parents[3] == skills/)
_CHECK_PCB = (Path(__file__).resolve().parents[3] / "check-pcb" / "scripts")
sys.path.insert(0, str(_CHECK_PCB))

try:
    from sexp_parser import parse_file
    from analyze_pcb import extract_footprints, extract_board_outline
except ImportError as e:  # pragma: no cover - environment guard
    print(json.dumps({"ok": False,
                       "error": f"cannot import check-pcb scripts: {e}"}))
    sys.exit(1)


def _pad_bbox(fp: dict) -> dict | None:
    """Bounding box of all pads — the fallback extent."""
    xs, ys = [], []
    for p in fp.get("pads", []):
        if "abs_x" not in p:
            continue
        hw = p.get("bbox_w", p.get("width", 0)) / 2.0
        hh = p.get("bbox_h", p.get("height", 0)) / 2.0
        xs += [p["abs_x"] - hw, p["abs_x"] + hw]
        ys += [p["abs_y"] - hh, p["abs_y"] + hh]
    if not xs:
        return None
    return {
        "min_x": round(min(xs), 3), "min_y": round(min(ys), 3),
        "max_x": round(max(xs), 3), "max_y": round(max(ys), 3),
        "w": round(max(xs) - min(xs), 3),
        "h": round(max(ys) - min(ys), 3),
    }


# A courtyard thinner than this on either axis is bad library data (e.g. a
# THT electrolytic whose courtyard is a single line) — pad bbox is safer.
_DEGENERATE_MM = 1.0


def _courtyard(fp: dict) -> dict | None:
    """Real component extent, three-level fallback:
      1. courtyard layer (non-degenerate)      — the library's own declaration
      2. body graphics (silk/fab/user) ∪ pads  — proxy for the real body
      3. pad bbox alone                        — flagged `geometry_uncertain`
    Converted libraries often ship NO CrtYd layer at all. A pad-bbox extent
    then under-reports the body (worst measured: ~80% short), and KiCad's own
    courtyard DRC is blind for the same reason — so level 2 closes the gap
    with graphics every footprint does have. Only level 3, where nothing but
    pads exists, is flagged uncertain."""
    c = fp.get("courtyard")
    court = None
    if c:
        court = {
            "min_x": c["min_x"], "min_y": c["min_y"],
            "max_x": c["max_x"], "max_y": c["max_y"],
            "w": round(c["max_x"] - c["min_x"], 3),
            "h": round(c["max_y"] - c["min_y"], 3),
        }
    degenerate = court is not None and (court["w"] < _DEGENERATE_MM
                                        or court["h"] < _DEGENERATE_MM)
    if court is not None and not degenerate:
        return court
    pad = _pad_bbox(fp)
    g = fp.get("graphics_bbox")
    if g is not None:
        boxes = [g] + ([pad] if pad else [])
        merged = {
            "min_x": round(min(b["min_x"] for b in boxes), 3),
            "min_y": round(min(b["min_y"] for b in boxes), 3),
            "max_x": round(max(b["max_x"] for b in boxes), 3),
            "max_y": round(max(b["max_y"] for b in boxes), 3),
        }
        merged["w"] = round(merged["max_x"] - merged["min_x"], 3)
        merged["h"] = round(merged["max_y"] - merged["min_y"], 3)
        merged["_from"] = "graphics"
        return merged
    if pad is not None:
        pad["_from"] = "pad_bbox"
        # No courtyard AND no body graphics — pad bbox is all there is, and
        # it is smaller than the real body; downstream must treat it as a hint.
        pad["geometry_uncertain"] = True
        return pad
    if court is not None:
        court["geometry_uncertain"] = True
    return court


def get_geometry(pcb_path: str, refs: set[str] | None = None,
                 with_pads: bool = True) -> dict:
    root = parse_file(pcb_path)
    raw = extract_footprints(root)
    outline = extract_board_outline(root)

    fps = []
    for fp in raw:
        ref = fp.get("reference", "")
        if refs is not None and ref not in refs:
            continue
        pads = fp.get("pads", [])
        nets = sorted({p["net_name"] for p in pads
                       if p.get("net_name") and p["net_name"] != ""})
        court = _courtyard(fp)
        # center == courtyard centre — this is the point `move` expects as a
        # target, so the AI reads center, picks a new one, and calls move.
        center = None
        if court:
            center = [round((court["min_x"] + court["max_x"]) / 2, 3),
                      round((court["min_y"] + court["max_y"]) / 2, 3)]
        entry = {
            "ref": ref,
            "value": fp.get("value", ""),
            # library id names the physical part — the only reliable signal for
            # "is this a connector" once a board uses non-KiCad refdes habits.
            "footprint": fp.get("library", ""),
            "x": fp.get("x", 0), "y": fp.get("y", 0),
            "center": center,
            "angle": fp.get("angle", 0),
            "layer": fp.get("layer", "F.Cu"),
            "type": fp.get("type", ""),
            "pad_count": fp.get("pad_count", len(pads)),
            "courtyard": court,
            "nets": nets,
        }
        if court and court.get("geometry_uncertain"):
            # no courtyard and no body graphics — extent is a pad-bbox guess,
            # smaller than the real body. check_placement warns on these.
            entry["geometry_uncertain"] = True
        if with_pads:
            # w/h are the BOARD-SPACE extent, not the library size: a pad on a
            # 90°-rotated footprint is as wide as the library pad is tall.
            entry["pads"] = [
                {"number": p.get("number"),
                 "x": p.get("abs_x"), "y": p.get("abs_y"),
                 "w": p.get("bbox_w", p.get("width")),
                 "h": p.get("bbox_h", p.get("height")),
                 "net": p.get("net_name")}
                for p in pads
            ]
        fps.append(entry)

    board = None
    if outline and outline.get("bounding_box"):
        b = outline["bounding_box"]
        board = {
            "min_x": b.get("min_x"), "min_y": b.get("min_y"),
            "w": b.get("width"), "h": b.get("height"),
            "edge_count": outline.get("edge_count", 0),
        }

    return {
        "ok": True,
        "pcb_path": pcb_path,
        "board": board,  # None until Edge.Cuts exists
        "footprint_count": len(fps),
        "footprints": fps,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-footprint PCB geometry → JSON")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--refs", help="comma-separated refs to include (default: all)")
    ap.add_argument("--no-pads", action="store_true",
                    help="omit per-pad detail (smaller output)")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    refs = None
    if args.refs:
        refs = {r.strip() for r in args.refs.split(",") if r.strip()}

    try:
        result = get_geometry(args.pcb, refs=refs, with_pads=not args.no_pads)
    except Exception as e:  # noqa: BLE001 - tool boundary, report cleanly
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
