#!/usr/bin/env python3
"""draw-pcb toolbox tool: render

Render the current placement to an annotated PNG — the visual-feedback channel
for AI-driven placement. Draws each footprint's courtyard as a labelled box,
the board outline, courtyard overlaps as red boxes, and (optionally) an
isolation-barrier line. The AI looks at this PNG to judge "what's crowded,
what crosses the barrier".

This is a lean matplotlib renderer on top of get_geometry — deliberately NOT
the release/kidoc SVG pipeline, which is built for fab-quality doc figures and
is too rigid for fast placement iteration.

Usage:
  render.py <board.kicad_pcb> [-o out.png] [--ratsnest] [--barrier-x MM]
            [--label-pads]

Exit 0 on success; prints JSON {ok, png, overlaps, ...} to stdout.
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless — no display
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

sys.path.insert(0, str(Path(__file__).resolve().parent))
from get_geometry import get_geometry  # noqa: E402


def _bbox_overlap(a: dict, b: dict) -> bool:
    """AABB intersection test on two courtyard dicts (strict overlap)."""
    return (a["min_x"] < b["max_x"] and b["min_x"] < a["max_x"]
            and a["min_y"] < b["max_y"] and b["min_y"] < a["max_y"])


def _find_overlaps(fps: list[dict]) -> list[tuple[str, str]]:
    """Footprint pairs whose courtyards intersect on the SAME layer.
    Layer filter matches check_placement — opposite sides never collide."""
    out = []
    cs = [f for f in fps if f.get("courtyard")]
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            if cs[i]["layer"] != cs[j]["layer"]:
                continue
            if _bbox_overlap(cs[i]["courtyard"], cs[j]["courtyard"]):
                out.append((cs[i]["ref"], cs[j]["ref"]))
    return out


# Layer → courtyard edge colour. B.Cu deliberately NOT red — overlap is red,
# and a back-side part must never look like a collision.
_LAYER_COLOR = {"F.Cu": "#1f77b4", "B.Cu": "#2ca02c"}


def render(pcb_path: str, out_png: str, ratsnest: bool = False,
           barrier_x: float | None = None, label_pads: bool = False) -> dict:
    geo = get_geometry(pcb_path, with_pads=True)
    fps = [f for f in geo["footprints"] if f.get("courtyard")]
    if not fps:
        return {"ok": False, "error": "no footprints with geometry"}

    overlaps = _find_overlaps(fps)
    overlapping_refs = {r for pair in overlaps for r in pair}

    # Drawing extent: board outline if present, else union of courtyards.
    xs, ys = [], []
    for f in fps:
        c = f["courtyard"]
        xs += [c["min_x"], c["max_x"]]
        ys += [c["min_y"], c["max_y"]]
    pad = 5.0
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad

    fig, ax = plt.subplots(figsize=(11, 11 * (y1 - y0) / max(x1 - x0, 1)))

    # Board outline (None until Edge.Cuts exists).
    if geo.get("board"):
        b = geo["board"]
        ax.add_patch(Rectangle((b["min_x"], b["min_y"]), b["w"], b["h"],
                               fill=False, edgecolor="#222", linewidth=2.0))

    # Net ratsnest — thin grey lines between pad centroids sharing a net.
    if ratsnest:
        from collections import defaultdict
        net_pads = defaultdict(list)
        for f in fps:
            for p in f.get("pads", []):
                if p.get("net") and p.get("x") is not None:
                    net_pads[p["net"]].append((p["x"], p["y"]))
        for pts in net_pads.values():
            for k in range(len(pts) - 1):
                ax.plot([pts[k][0], pts[k + 1][0]],
                        [pts[k][1], pts[k + 1][1]],
                        color="#bbbbbb", linewidth=0.4, zorder=0)

    # Footprint courtyards.
    for f in fps:
        c = f["courtyard"]
        is_ovl = f["ref"] in overlapping_refs
        edge = "#d62728" if is_ovl else _LAYER_COLOR.get(f["layer"], "#1f77b4")
        face = "#d6272822" if is_ovl else "#1f77b410"
        ax.add_patch(Rectangle((c["min_x"], c["min_y"]), c["w"], c["h"],
                               fill=True, facecolor=face, edgecolor=edge,
                               linewidth=1.6 if is_ovl else 0.9))
        cx, cy = (c["min_x"] + c["max_x"]) / 2, (c["min_y"] + c["max_y"]) / 2
        ax.text(cx, cy, f["ref"], ha="center", va="center",
                fontsize=7, color=edge, weight="bold")
        if label_pads:
            for p in f.get("pads", []):
                if p.get("x") is not None:
                    ax.plot(p["x"], p["y"], "s", color="#888",
                            markersize=2, zorder=2)

    # Isolation barrier marker.
    if barrier_x is not None:
        ax.axvline(barrier_x, color="#ff8c00", linestyle="--", linewidth=2.0)
        ax.text(barrier_x, y0 + 1, "  isolation barrier",
                color="#ff8c00", fontsize=8, va="bottom")

    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.invert_yaxis()  # KiCad y is downward
    ax.set_aspect("equal")
    ax.set_title(f"{Path(pcb_path).stem}  —  {len(fps)} footprints, "
                 f"{len(overlaps)} courtyard overlap(s)", fontsize=10)
    ax.grid(True, linewidth=0.3, color="#eee")

    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=130, bbox_inches="tight")
    plt.close(fig)

    return {
        "ok": True,
        "png": str(out_png),
        "footprint_count": len(fps),
        "overlap_count": len(overlaps),
        "overlaps": [list(p) for p in overlaps],
        "board": geo.get("board"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Render placement → annotated PNG")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("-o", "--out", help="output PNG (default: <pcb>.render.png)")
    ap.add_argument("--ratsnest", action="store_true",
                    help="draw thin net lines between pads")
    ap.add_argument("--barrier-x", type=float,
                    help="draw a vertical isolation-barrier line at X mm")
    ap.add_argument("--label-pads", action="store_true",
                    help="mark pad centres")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    out = args.out or str(Path(args.pcb).with_suffix(".render.png"))
    try:
        result = render(args.pcb, out, ratsnest=args.ratsnest,
                        barrier_x=args.barrier_x, label_pads=args.label_pads)
    except Exception as e:  # noqa: BLE001 - tool boundary
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
