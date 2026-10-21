#!/usr/bin/env python3
"""draw-pcb toolbox tool: check_connector_access

The PHYSICAL-USABILITY GATE for interface parts. `check_placement` proves a
placement is *legal* (nothing overlaps, nothing off-board); this tool proves
the board can actually be *plugged into*.

The failure it exists to catch: a connector sitting correctly on the board edge
with its mating opening pointing at the board interior. Nothing else sees it —
DRC is happy, courtyards do not overlap, "hugs the edge" passes, the render
looks fine — and the fabricated board has terminals you cannot land a wire on.
Rotation is the usual culprit (a 180° error is invisible in a bbox), so is a
mating face recessed far enough behind the edge that the plug's overmold hits
the board.

HOW THE MATING DIRECTION IS FOUND (no library metadata exists for this):
on a SIDE-ENTRY connector the solder interface sits away from the mouth, so the
body outline extends further past the pad bbox on the mating side than on any
other. Measure that overhang on each of the four local sides; the winner is the
mating side, and it must win by both an absolute distance and a RATIO over the
runner-up (absolute-only makes confidence track part size, not evidence).

The premise holds only for side-entry parts. A top-entry (+Z) housing walls its
pads in on every side and has no in-plane mating side at all, yet its widest
wall still "wins" — measured against KiCad's own library that mislabelled 26%
of vertical connectors. `looks_vertical()` catches those by name before any
geometry runs. Even for side-entry parts a bounding box cannot see the wire-
entry funnels that actually mark the mouth, so a thin winning ratio is reported
as a `thin_margin` warning rather than passed off as certainty.

Evaluated on every body layer present (CrtYd / Fab / SilkS); layers that
disagree, a near-symmetric body, or a footprint with no body outline at all
yield `undetermined` — a HARD FAIL, not a silent pass, because a guess here
ships a board that cannot be used. Declare those explicitly with --declare.

HARD checks (any one → hard_fail):
  faces_inward   mating direction does not point at the nearest board edge
  not_at_edge    mating face further than --max-gap behind the edge it faces
  blocked        another part sits in the insertion corridor
  undetermined   mating direction not derivable and not declared

Reported as warnings (not the gate):
  overhang       body extends past the board edge by more than --max-overhang
  switch parts   skipped unless declared (most are top-actuated)

Output JSON:
  hard_fail (bool — the gate), connectors[], violations[], warnings[]

Usage:
  check_connector_access.py <board.kicad_pcb> [--declare decl.json]
      [--max-gap MM] [--max-overhang MM] [--include R1,R2] [--exclude R3]

--declare JSON: {"CN8": {"mating_dir": "-Y", "note": "datasheet fig 3"}}
  mating_dir ∈ +X | -X | +Y | -Y | +Z | none
  FOOTPRINT-LOCAL, as drawn in the footprint editor — the same frame as
  placement_brief's `mating_side_local`, NOT a board direction. It states what
  the datasheet says about the part; the tool still rotates it by the placed
  angle and checks where that lands. Declaring a board direction instead would
  answer the question being asked and let a part rotated into the board pass.
  +Z   mates from above (vertical header, mezzanine) — edge rule not applied
  none not an interface part — skip entirely (counted in `silenced[]`)
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from connector_id import (classify, looks_like_interface_ref,  # noqa: E402
                          looks_vertical)

_CHECK_PCB = (Path(__file__).resolve().parents[3] / "check-pcb" / "scripts")
sys.path.insert(0, str(_CHECK_PCB))
try:
    from sexp_parser import parse_file, find_all, find_first, get_at, get_property, get_value  # noqa: E402
except ImportError as e:  # pragma: no cover - environment guard
    print(json.dumps({"ok": False,
                      "error": f"cannot import check-pcb scripts: {e}"}))
    sys.exit(1)

# Body outline layers, best first. CrtYd is the true extent; Fab is the
# mechanical body; SilkS is a proxy that survives libraries converted from
# other EDA tools (which frequently ship no CrtYd at all).
_BODY_LAYERS = ("CrtYd", "Fab", "SilkS")
_GRAPHIC = ("fp_line", "fp_rect", "fp_arc", "fp_poly", "fp_circle")

DIRS = {"-X": (-1.0, 0.0), "+X": (1.0, 0.0), "-Y": (0.0, -1.0), "+Y": (0.0, 1.0)}
_OPPOSITE = {"-X": "+X", "+X": "-X", "-Y": "+Y", "+Y": "-Y"}

# A mating side must beat the runner-up by BOTH this many mm and this factor
# before it is trusted without a declaration. Absolute-only made confidence a
# proxy for part SIZE rather than evidence quality.
MIN_CONFIDENCE_MM = 0.5
# Ratio chosen by measurement, not taste: swept over KiCad's own ~6.6k
# _Vertical/_Horizontal-labelled connector footprints. looks_vertical() already
# drives vertical false-confidence to zero by itself, so this threshold only
# trades side-entry recall — 1.25 keeps ~70% resolvable including the THT
# screw-terminal family (ratio ≈1.32); 1.4 starts dropping that family and 2.0
# costs ~19pp of recall for no accuracy gain.
MIN_CONFIDENCE_RATIO = 1.25
# Below this the winner barely beat the runner-up. Still reported, but flagged
# `thin_margin` — a bbox cannot see the wire-entry funnels that really mark the
# mouth, so the operator is told to confirm on the render.
THIN_MARGIN_RATIO = 1.6
# Overhang on every side above this = pads walled in. INFORMATION ONLY: a THT
# screw terminal has its pins mid-block, so "enclosed" does not imply top-entry
# and must never gate the verdict (that cost 19pp of recall when it did).
_ENCLOSED_MM = 0.3


def _rotate(px: float, py: float, angle_deg: float) -> tuple[float, float]:
    """Footprint-local → board-relative offset.

    KiCad's `(at x y angle)` rotates counter-clockwise on screen in a Y-down
    file frame, i.e. the local point is rotated by -angle in ordinary maths
    terms. Verified against kicad-cli's own SVG render of a rotated
    asymmetric footprint — using +angle mirrors the part.
    """
    rad = math.radians(-angle_deg)
    c, s = math.cos(rad), math.sin(rad)
    return px * c - py * s, px * s + py * c


def _pts_of_graphic(node: list) -> list[tuple[float, float]]:
    """Extent points of one graphic, in its own frame.

    A circle is stored as centre + a point on the rim, so taking those two
    verbatim yields half the true extent, offset to one side — which reads as
    asymmetry and hands a confident mating side to a perfectly round body
    (DIN/XLR shells, barrel jacks, coin cells). Expand to the real square.
    An arc likewise needs its swept extremes, not just three points.
    """
    kind = node[0] if node and isinstance(node[0], str) else ""
    pts = []
    named = {}
    for c in node:
        if not isinstance(c, list) or not c:
            continue
        key = c[0] if isinstance(c[0], str) else None
        if key in ("start", "end", "mid", "center") and len(c) >= 3:
            named[key] = (float(c[1]), float(c[2]))
            pts.append((float(c[1]), float(c[2])))
        elif key == "pts":
            for p in c[1:]:
                if isinstance(p, list) and p and p[0] == "xy" and len(p) >= 3:
                    pts.append((float(p[1]), float(p[2])))

    if kind in ("fp_circle", "gr_circle") and "center" in named and "end" in named:
        (cx, cy), (ex, ey) = named["center"], named["end"]
        r = math.hypot(ex - cx, ey - cy)
        return [(cx - r, cy - r), (cx + r, cy + r)]

    if kind in ("fp_arc", "gr_arc") and {"start", "mid", "end"} <= named.keys():
        arc = _arc_extent(named["start"], named["mid"], named["end"])
        if arc:
            pts.extend(arc)
    return pts


def _arc_extent(p0, p1, p2):
    """Sample a 3-point arc so an arc >180° is not understated by its chords."""
    (x0, y0), (x1, y1), (x2, y2) = p0, p1, p2
    d = 2 * (x0 * (y1 - y2) + x1 * (y2 - y0) + x2 * (y0 - y1))
    if abs(d) < 1e-12:
        return []
    ux = ((x0 ** 2 + y0 ** 2) * (y1 - y2) + (x1 ** 2 + y1 ** 2) * (y2 - y0)
          + (x2 ** 2 + y2 ** 2) * (y0 - y1)) / d
    uy = ((x0 ** 2 + y0 ** 2) * (x2 - x1) + (x1 ** 2 + y1 ** 2) * (x0 - x2)
          + (x2 ** 2 + y2 ** 2) * (x1 - x0)) / d
    r = math.hypot(x0 - ux, y0 - uy)
    a0 = math.atan2(y0 - uy, x0 - ux)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    # walk start → mid → end the short way round each hop
    def _unwrap(a, ref):
        while a - ref > math.pi:
            a -= 2 * math.pi
        while a - ref < -math.pi:
            a += 2 * math.pi
        return a
    a1 = _unwrap(a1, a0)
    a2 = _unwrap(a2, a1)
    steps = 16
    return [(ux + r * math.cos(a0 + (a2 - a0) * i / steps),
             uy + r * math.sin(a0 + (a2 - a0) * i / steps))
            for i in range(steps + 1)]


def _bbox(pts) -> tuple[float, float, float, float] | None:
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


def _local_footprint(fp: list) -> dict:
    """Pull local-frame pad bbox and per-layer body outlines out of a footprint."""
    pad_pts: list[tuple[float, float]] = []
    bodies: dict[str, list[tuple[float, float]]] = {ly: [] for ly in _BODY_LAYERS}

    for c in fp:
        if not isinstance(c, list) or not c:
            continue
        key = c[0] if isinstance(c[0], str) else None
        if key == "pad":
            at = find_first(c, "at")
            size = find_first(c, "size")
            if not (at and size and len(at) >= 3 and len(size) >= 3):
                continue
            px, py = float(at[1]), float(at[2])
            prot = float(at[3]) if len(at) > 3 else 0.0
            hw, hh = float(size[1]) / 2.0, float(size[2]) / 2.0
            for dx in (-hw, hw):
                for dy in (-hh, hh):
                    rx, ry = _rotate(dx, dy, prot)
                    pad_pts.append((px + rx, py + ry))
        elif key in _GRAPHIC:
            layer = get_value(c, "layer") or ""
            for ly in _BODY_LAYERS:
                if ly in layer:
                    bodies[ly].extend(_pts_of_graphic(c))
                    break

    return {"pad_bbox": _bbox(pad_pts),
            "bodies": {ly: _bbox(p) for ly, p in bodies.items() if p}}


def _overhangs(pad: tuple, body: tuple) -> dict[str, float]:
    """How far the body sticks out past the pad bbox, per local side."""
    pxmin, pxmax, pymin, pymax = pad
    bxmin, bxmax, bymin, bymax = body
    return {"-X": pxmin - bxmin, "+X": bxmax - pxmax,
            "-Y": pymin - bymin, "+Y": bymax - pymax}


def _infer_mating(local: dict, footprint: str = "") -> dict:
    """Body-past-pad asymmetry → local mating side + confidence.

    Two guards keep this honest, both added after measuring the rule against
    KiCad's own ~7.7k connector footprints:

    * The margin is RELATIVE (top / second), not absolute mm. An absolute
      threshold makes confidence track part SIZE rather than evidence quality:
      a big housing clears 0.5mm on its widest side no matter how symmetric it
      is, while a small side-entry connector with an unmistakable cavity does
      not. Ratio asks the question that actually matters — did one side win,
      or are two sides comparable?
    * A top-entry (+Z) housing surrounds its pads on ALL four sides, because
      the wall/latch/polarising rib runs the whole way round. That geometry
      has no in-plane mating side at all, yet the widest wall still "wins".
      Enclosure on all four sides is therefore treated as +Z-suspect and
      returns undetermined regardless of margin.
    """
    pad = local["pad_bbox"]
    if not pad:
        return {"dir": None, "confidence": 0.0, "layer": None,
                "reason": "footprint has no pads"}

    if looks_vertical(footprint):
        return {"dir": None, "confidence": 0.0, "layer": None,
                "reason": "footprint name says this is a vertical / top-entry "
                          "part — it has no in-plane mating side; declare "
                          "'+Z' if that is right",
                "per_layer": {}}

    best = None
    per_layer = {}
    for ly in _BODY_LAYERS:
        body = local["bodies"].get(ly)
        if not body:
            continue
        ov = _overhangs(pad, body)
        ranked = sorted(ov.items(), key=lambda kv: kv[1], reverse=True)
        top, second = ranked[0], ranked[1]
        margin = top[1] - max(second[1], 0.0)
        ratio = (top[1] / second[1]) if second[1] > 1e-6 else float("inf")
        enclosed = all(v > _ENCLOSED_MM for v in ov.values())
        per_layer[ly] = {"dir": top[0], "confidence": round(margin, 3),
                         "ratio": None if ratio == float("inf") else round(ratio, 2),
                         "pads_enclosed": enclosed}
        # A side only counts as decisive if it beats the runner-up by BOTH a
        # real distance and a real factor, and the pads are not walled in.
        decisive = (margin >= MIN_CONFIDENCE_MM
                    and ratio >= MIN_CONFIDENCE_RATIO)
        score = margin if decisive else -1.0
        if best is None or score > best["_score"]:
            best = {"dir": top[0] if decisive else None,
                    "confidence": margin if decisive else 0.0,
                    "ratio": None if ratio == float("inf") else round(ratio, 2),
                    "layer": ly, "_score": score}

    if best is not None and best["dir"] is None:
        return {"dir": None, "confidence": 0.0, "layer": None,
                "reason": (f"no side won decisively (need ≥{MIN_CONFIDENCE_MM}mm "
                           f"AND ≥{MIN_CONFIDENCE_RATIO}× the runner-up) — the "
                           f"body is near-symmetric about the pads"),
                "per_layer": per_layer}

    if best is None:
        return {"dir": None, "confidence": 0.0, "layer": None,
                "reason": "footprint has no CrtYd / Fab / SilkS body outline — "
                          "mating side cannot be derived from geometry",
                "per_layer": per_layer}
    best.pop("_score", None)

    # Two layers that both look decisive but disagree → trust neither.
    confident = {ly: v for ly, v in per_layer.items()
                 if v["confidence"] >= MIN_CONFIDENCE_MM}
    if len({v["dir"] for v in confident.values()}) > 1:
        return {"dir": None, "confidence": 0.0, "layer": None,
                "reason": f"body layers disagree on the mating side: "
                          + ", ".join(f"{ly}→{v['dir']}"
                                      for ly, v in confident.items()),
                "per_layer": per_layer}

    best["confidence"] = round(best["confidence"], 3)
    best["per_layer"] = per_layer
    return best


def _board_bbox(local: dict, at) -> list[float] | None:
    """Pads + body outline of one footprint, in board coordinates.

    Wider than `get_geometry`'s courtyard whenever the library ships no CrtYd
    layer — there the courtyard degrades to the pad bbox, which for a connector
    understates the real part by several mm (the whole mating cavity is
    missing). Any "how far is this from the board edge" question must use this.
    """
    x, y, angle = at
    pts = []
    for src in ([local["pad_bbox"]] if local["pad_bbox"] else []) \
            + [b for b in local["bodies"].values() if b]:
        pts += [(src[0], src[2]), (src[1], src[2]),
                (src[0], src[3]), (src[1], src[3])]
    if not pts:
        return None
    gp = [(x + rx, y + ry) for rx, ry in (_rotate(px, py, angle)
                                          for px, py in pts)]
    xs = [p[0] for p in gp]
    ys = [p[1] for p in gp]
    return [round(min(xs), 3), round(max(xs), 3),
            round(min(ys), 3), round(max(ys), 3)]


def angle_for_edge(local_side: str, board_dir: str) -> float | None:
    """Rotation that aims a footprint's local mating side at a board direction.

    Answers the question the AI actually has *before* it places anything:
    "this connector must open toward the left edge — what angle is that?"
    """
    lx, ly = DIRS[local_side]
    want = DIRS[board_dir]
    for angle in (0.0, 90.0, 180.0, 270.0):
        gx, gy = _rotate(lx, ly, angle)
        if abs(gx - want[0]) < 1e-6 and abs(gy - want[1]) < 1e-6:
            return angle
    return None


def mating_sides(pcb_path: str) -> dict[str, dict]:
    """ref → mating-side facts that depend only on the footprint.

    Valid before anything has been placed, which is exactly when placement
    needs them — hence separate from the placement gate below.
    """
    root = parse_file(pcb_path)
    out = {}
    for fp in find_all(root, "footprint") or find_all(root, "module"):
        ref = get_property(fp, "Reference") or ""
        if not ref:
            for ft in find_all(fp, "fp_text"):
                if len(ft) >= 3 and ft[1] == "reference":
                    ref = ft[2]
        if not ref:
            continue
        lib = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""
        local = _local_footprint(fp)
        pad = local["pad_bbox"]
        max_ov = 0.0
        if pad:
            for body in local["bodies"].values():
                if body:
                    max_ov = max(max_ov, max(_overhangs(pad, body).values()))
        inf = _infer_mating(local, lib)
        at = get_at(fp)
        out[ref] = {"footprint": lib, "local_side": inf["dir"],
                    "confidence_mm": inf["confidence"],
                    "body_layer": inf.get("layer"),
                    "body_overhang_mm": round(max_ov, 3),
                    "body_bbox": _board_bbox(local, at) if at else None,
                    "reason": inf.get("reason")}
    return out


def _edge_segments(root: list) -> list[tuple[float, float, float, float]]:
    """Edge.Cuts → straight segments (arcs/circles approximated by chords)."""
    segs = []
    for kind in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"):
        for item in find_all(root, kind):
            if get_value(item, "layer") != "Edge.Cuts":
                continue
            st = find_first(item, "start")
            en = find_first(item, "end")
            if kind == "gr_line" and st and en:
                segs.append((float(st[1]), float(st[2]),
                             float(en[1]), float(en[2])))
            elif kind == "gr_rect" and st and en:
                x0, y0, x1, y1 = (float(st[1]), float(st[2]),
                                  float(en[1]), float(en[2]))
                segs += [(x0, y0, x1, y0), (x1, y0, x1, y1),
                         (x1, y1, x0, y1), (x0, y1, x0, y0)]
            elif kind == "gr_arc" and st and en:
                mid = find_first(item, "mid")
                if mid:
                    segs.append((float(st[1]), float(st[2]),
                                 float(mid[1]), float(mid[2])))
                    segs.append((float(mid[1]), float(mid[2]),
                                 float(en[1]), float(en[2])))
                else:
                    segs.append((float(st[1]), float(st[2]),
                                 float(en[1]), float(en[2])))
            elif kind == "gr_circle":
                ctr = find_first(item, "center")
                if ctr and en:
                    cx, cy = float(ctr[1]), float(ctr[2])
                    r = math.hypot(float(en[1]) - cx, float(en[2]) - cy)
                    prev = None
                    for i in range(33):
                        a = 2 * math.pi * i / 32
                        p = (cx + r * math.cos(a), cy + r * math.sin(a))
                        if prev:
                            segs.append((prev[0], prev[1], p[0], p[1]))
                        prev = p
            elif kind == "gr_poly":
                pts = _pts_of_graphic(item)
                for i in range(len(pts)):
                    a, b = pts[i], pts[(i + 1) % len(pts)]
                    segs.append((a[0], a[1], b[0], b[1]))
    return segs


def _edge_loops(segs):
    """Split Edge.Cuts segments into closed loops, largest-area first.

    Needed because a ray does not care whether the boundary it hit is the
    board's outer perimeter or the wall of an internal cutout. Measuring a
    connector's reach against a display window or cable slot both passes
    boards that open into a void and fails correctly-placed ones sitting near
    a hole — the two failure directions the gate exists to separate.
    """
    remaining = list(segs)
    loops = []
    tol = 0.01

    def near(a, b):
        return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol

    while remaining:
        x1, y1, x2, y2 = remaining.pop()
        chain = [(x1, y1), (x2, y2)]
        used = [(x1, y1, x2, y2)]
        grew = True
        while grew:
            grew = False
            for i, (a1, b1, a2, b2) in enumerate(remaining):
                if near((a1, b1), chain[-1]):
                    chain.append((a2, b2))
                elif near((a2, b2), chain[-1]):
                    chain.append((a1, b1))
                elif near((a1, b1), chain[0]):
                    chain.insert(0, (a2, b2))
                elif near((a2, b2), chain[0]):
                    chain.insert(0, (a1, b1))
                else:
                    continue
                used.append(remaining.pop(i))
                grew = True
                break
        area = 0.0
        for i in range(len(chain)):
            px, py = chain[i]
            qx, qy = chain[(i + 1) % len(chain)]
            area += px * qy - qx * py
        loops.append({"segments": used, "area": abs(area) / 2.0,
                      "closed": near(chain[0], chain[-1])})
    loops.sort(key=lambda L: L["area"], reverse=True)
    return loops


def _ray_edge_distance(ox: float, oy: float, dx: float, dy: float,
                       segs) -> float | None:
    """Signed distance from (ox,oy) to Edge.Cuts along (dx,dy).

    Positive = the edge is ahead of the mating face (face is inside the board).
    Negative = the face already sits past the edge (overhang).
    """
    hits = []
    for (x1, y1, x2, y2) in segs:
        ex, ey = x2 - x1, y2 - y1
        den = dx * ey - dy * ex
        if abs(den) < 1e-12:
            continue
        t = ((x1 - ox) * ey - (y1 - oy) * ex) / den          # along ray
        u = ((x1 - ox) * dy - (y1 - oy) * dx) / den          # along segment
        if -1e-9 <= u <= 1 + 1e-9:
            hits.append(t)
    if not hits:
        return None
    fwd = [t for t in hits if t >= -1e-9]
    if fwd:
        return min(fwd)
    return max(hits)


def _rects_overlap(a, b, tol=0.0) -> bool:
    return not (a[1] <= b[0] + tol or b[1] <= a[0] + tol
                or a[3] <= b[2] + tol or b[3] <= a[2] + tol)


def check_connector_access(pcb_path: str, declare: dict | None = None,
                           max_gap: float = 2.0, max_overhang: float = 1.0,
                           include: set[str] | None = None,
                           exclude: set[str] | None = None) -> dict:
    root = parse_file(pcb_path)
    segs = _edge_segments(root)
    loops = _edge_loops(segs)
    # Measure only against the board's outer perimeter. Hole walls are kept
    # separately so "opens into an internal cutout" can be reported as its own
    # verdict instead of silently reading as a clean edge.
    outer = loops[0]["segments"] if loops else segs
    holes = [s for L in loops[1:] for s in L["segments"]]
    declare = declare or {}
    # M5: an unrecognised value used to raise KeyError deep in the loop, and
    # the blanket handler turned that into "ok: false" with no verdict for ANY
    # part — one typo blinding the whole gate.
    _VALID = set(DIRS) | {"+Z", "none"}
    bad_decl = {r: d.get("mating_dir") for r, d in declare.items()
                if d.get("mating_dir") not in _VALID}

    parts = []
    for fp in find_all(root, "footprint") or find_all(root, "module"):
        at = get_at(fp)
        if not at:
            continue
        x, y, angle = at
        ref = get_property(fp, "Reference") or ""
        if not ref:
            for ft in find_all(fp, "fp_text"):
                if len(ft) >= 3 and ft[1] == "reference":
                    ref = ft[2]
        lib = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""
        local = _local_footprint(fp)
        pad = local["pad_bbox"]
        # board-frame extent of the whole part, for corridor blocking
        allpts = []
        for src in ([pad] if pad else []) + list(local["bodies"].values()):
            if src:
                allpts += [(src[0], src[2]), (src[1], src[2]),
                           (src[0], src[3]), (src[1], src[3])]
        gpts = [(x + rx, y + ry)
                for rx, ry in (_rotate(px, py, angle) for px, py in allpts)]
        tht = any(isinstance(c, list) and c and c[0] == "pad"
                  and len(c) > 2 and c[2] == "thru_hole" for c in fp)
        parts.append({"ref": ref, "lib": lib, "x": x, "y": y, "angle": angle,
                      "value": get_property(fp, "Value") or "",
                      "layer": get_value(fp, "layer") or "F.Cu", "tht": tht,
                      "local": local, "bbox": _bbox(gpts)})

    results, violations, warnings = [], [], []
    # Every part that leaves this gate WITHOUT being checked, and how it got
    # out. Silencing is legitimate (a vertical header really is +Z), but a
    # silent silence is indistinguishable from a pass — so each one is named
    # and counted, and the loop's print contract has to echo the count.
    silenced: list[dict] = []
    # Parts with connector-like geometry that no rule claimed — the blind spot.
    unclassified: list[dict] = []
    if exclude:
        for r in sorted(exclude):
            silenced.append({"ref": r, "how": "excluded",
                             "note": "dropped by --exclude"})
    for p in parts:
        ref, local = p["ref"], p["local"]
        pad = local["pad_bbox"]
        max_ov = 0.0
        if pad:
            for body in local["bodies"].values():
                if body:
                    max_ov = max(max_ov, max(_overhangs(pad, body).values()))

        decl = declare.get(ref) or {}
        if ref in bad_decl:
            decl = {k: v for k, v in decl.items() if k != "mating_dir"}
        kind = classify(ref, p["lib"], body_overhang_mm=max_ov,
                        include=include, exclude=exclude)
        if decl.get("mating_dir") and ref not in (exclude or set()):
            kind = kind or "connector"
        if kind is None:
            # C4: a part with a real mating cavity that no rule recognised is
            # the gate's blind spot. Silently dropping it returns a green
            # verdict on a board whose only connector was never looked at.
            if looks_like_interface_ref(ref):
                unclassified.append({
                    "ref": ref, "footprint": p["lib"],
                    "body_overhang_mm": round(max_ov, 3),
                    "why": "refdes looks like an interface part but the "
                           "footprint name matches no connector keyword and "
                           "its body outline is too weak to confirm — pass "
                           "--include to force it into the gate"})
            continue

        entry = {"ref": ref, "value": p["value"], "footprint": p["lib"],
                 "kind": kind, "angle": p["angle"]}

        declared = decl.get("mating_dir")
        if declared == "none":
            entry.update(status="skipped", mating_dir="none",
                         source="declared",
                         detail="declared not an interface part")
            results.append(entry)
            silenced.append({"ref": ref, "how": "declared_none",
                             "note": decl.get("note") or "(no reason given)"})
            continue

        # A declaration states a fact about the FOOTPRINT ("the datasheet says
        # the opening is on this side of the part"), never about the board.
        # It therefore goes through the same rotation as an inferred side —
        # otherwise declaring would answer the very question being asked and a
        # part rotated 180° into the board would pass. Same frame as
        # placement_brief's `mating_side_local` / `angle_for_edge`.
        if declared:
            local_side, source, conf, layer = declared, "declared", None, None
        else:
            inf = _infer_mating(local, p["lib"])
            local_side = inf["dir"]
            conf, layer = inf["confidence"], inf.get("layer")
            source = "geometry"
            entry["inference"] = {k: v for k, v in inf.items()
                                  if k in ("per_layer", "reason")}
            entry["confidence_ratio"] = inf.get("ratio")
            if inf.get("dir") and (inf.get("ratio") or 99) < THIN_MARGIN_RATIO:
                warnings.append({
                    "type": "thin_margin", "ref": ref,
                    "detail": (f"mating side {inf['dir']} won by only "
                               f"{inf.get('ratio')}× — a bounding box cannot "
                               f"see wire-entry funnels or chamfers, so this "
                               f"family is resolved on thin evidence; confirm "
                               f"the opening on the render")})

        mdir = local_side
        if local_side in DIRS:
            gx, gy = _rotate(*DIRS[local_side], p["angle"])
            mdir = ("+X" if gx > 0.7 else "-X" if gx < -0.7
                    else "+Y" if gy > 0.7 else "-Y")

        if abs((p["angle"] % 90.0)) > 1e-6:
            warnings.append({"type": "non_orthogonal_rotation", "ref": ref,
                             "detail": f"placed at {p['angle']}° — the mating "
                                       f"face plane is quantised to the "
                                       f"nearest axis and the extent is a "
                                       f"re-boxed AABB; verify on the render"})
        entry.update(mating_side_local=local_side, mating_dir=mdir,
                     mating_dir_frame="board (mating_side_local rotated by angle)",
                     source=source, confidence_mm=conf, body_layer=layer)

        if decl.get("note"):
            entry["note"] = decl["note"]

        if mdir == "+Z":
            entry.update(status="ok",
                         detail="mates from above — board-edge rule NOT applied")
            results.append(entry)
            silenced.append({"ref": ref, "how": "declared_vertical",
                             "note": decl.get("note") or "(no reason given)"})
            continue

        # Switches are checked only when the caller declares how they mate.
        # Most are top-actuated, so geometry says "no dominant side" — turning
        # that into a hard fail would make every board with a tact switch red
        # and train the operator to ignore the gate.
        if kind == "switch" and source == "geometry":
            entry.update(status="skipped",
                         detail="switch — most are top-actuated; declare "
                                "mating_dir if it is edge-operated")
            results.append(entry)
            warnings.append({"type": "switch_not_checked", "ref": ref,
                             "detail": "reachability not machine-checkable; "
                                       "confirm on the render"})
            silenced.append({"ref": ref, "how": "switch_skipped",
                             "note": "top-actuated assumed; declare "
                                     "mating_dir if edge-operated"})
            continue

        if mdir is None or (source == "geometry" and conf < MIN_CONFIDENCE_MM):
            reason = (entry.get("inference", {}).get("reason")
                      or f"mating side won by only {conf}mm "
                         f"(needs ≥{MIN_CONFIDENCE_MM}mm)")
            entry.update(status="undetermined", detail=reason,
                         fix_hint=f"read the datasheet and declare it: "
                                  f'--declare with {{"{ref}": '
                                  f'{{"mating_dir": "+X|-X|+Y|-Y|+Z"}}}}')
            results.append(entry)
            violations.append({"type": "undetermined", "ref": ref,
                               "detail": reason})
            continue

        # mating face plane, in board coords, along the mating direction
        bb = p["bbox"]
        face = {"+X": bb[1], "-X": bb[0], "+Y": bb[3], "-Y": bb[2]}[mdir]
        cx, cy = (bb[0] + bb[1]) / 2, (bb[2] + bb[3]) / 2
        ox, oy = (face, cy) if mdir in ("+X", "-X") else (cx, face)
        dx, dy = DIRS[mdir]
        gap = _ray_edge_distance(ox, oy, dx, dy, outer)
        hole_gap = (_ray_edge_distance(ox, oy, dx, dy, holes)
                    if holes else None)
        entry["gap_mm"] = None if gap is None else round(gap, 3)

        # which edge is actually nearest — makes the fix hint concrete
        back = _ray_edge_distance(ox, oy, -dx, -dy, outer)
        entry["gap_behind_mm"] = None if back is None else round(back, 3)

        if gap is None:
            # NOT a datasheet problem — --declare cannot clear this one, and
            # sending the operator back to the datasheet loops forever.
            entry.update(status="undetermined",
                         detail=(f"mating side is known ({mdir}) but the ray "
                                 f"hits no Edge.Cuts — the board outline is "
                                 f"open or broken; this is not a footprint "
                                 f"problem"),
                         fix_hint="fix Edge.Cuts (see references/known_issues.md "
                                  "— open loop / multiple closed rings), then "
                                  "re-run; --declare will NOT clear this")
            violations.append({"type": "board_outline_broken", "ref": ref,
                               "detail": entry["detail"]})
            results.append(entry)
            continue

        if back is not None and back < gap:
            entry.update(
                status="faces_inward",
                detail=(f"mating face points {mdir} at an edge {gap:.2f}mm "
                        f"away, but the board edge behind it is only "
                        f"{back:.2f}mm away — the opening is aimed at the "
                        f"board interior"),
                fix_hint=f"rotate {ref} by 180° (currently {p['angle']:.0f}°)")
            violations.append({"type": "faces_inward", "ref": ref,
                               "detail": entry["detail"]})
        elif gap > max_gap:
            entry.update(
                status="not_at_edge",
                detail=(f"mating face is {gap:.2f}mm behind the {mdir} board "
                        f"edge (max {max_gap}mm) — a plug cannot reach it"),
                fix_hint=f"move {ref} {gap - max_gap:.2f}mm further {mdir}")
            violations.append({"type": "not_at_edge", "ref": ref,
                               "detail": entry["detail"]})
        elif gap < -max_overhang:
            entry.update(
                status="overhang",
                detail=(f"body hangs {-gap:.2f}mm past the {mdir} board edge "
                        f"(max {max_overhang}mm)"))
            warnings.append({"type": "overhang", "ref": ref,
                             "detail": entry["detail"]})
        else:
            entry.update(status="ok")

        # C3: the outer perimeter may be reachable while an internal cutout
        # sits in the way. Opening into a slot is not the same as opening off
        # the board — say which, never silently treat a hole as an edge.
        if (hole_gap is not None and hole_gap > 0
                and (gap is None or hole_gap < gap - 1e-6)):
            entry["hole_gap_mm"] = round(hole_gap, 3)
            entry["status"] = "blocked"
            entry["detail"] = (f"mating face reaches an internal cutout wall "
                               f"{hole_gap:.2f}mm away before the outer board "
                               f"edge ({gap:.2f}mm) — it opens into a slot, "
                               f"not off the board")
            entry["fix_hint"] = ("aim it at the outer perimeter, or declare "
                                 "the cutout intentional and re-check by eye")
            violations.append({"type": "opens_into_cutout", "ref": ref,
                               "detail": entry["detail"]})

        # Insertion corridor — anything between the mating face and the edge.
        # Only meaningful once the part already points the right way: a
        # connector aimed at the board interior sweeps half the board and would
        # bury the real diagnosis (wrong rotation) under a blocker list.
        if entry["status"] in ("ok", "overhang") and gap is not None and gap > 0:
            if mdir in ("+X", "-X"):
                lo, hi = (face, face + gap) if mdir == "+X" else (face - gap, face)
                corridor = (lo, hi, bb[2], bb[3])
            else:
                lo, hi = (face, face + gap) if mdir == "+Y" else (face - gap, face)
                corridor = (bb[0], bb[1], lo, hi)
            # The corridor is the volume in front of the mouth, on the
            # connector's own side of the board. A part mounted on the far
            # side sits below that volume and obstructs nothing — including a
            # THT one, whose body is still on its placement layer and whose
            # leads are clipped. check_placement's overlap test already skips
            # cross-layer pairs; the two gates must not disagree.
            blockers = [q["ref"] for q in parts
                        if q["ref"] != ref and q["bbox"]
                        and q["layer"] == p["layer"]
                        and _rects_overlap(corridor, q["bbox"], tol=0.01)]
            if blockers:
                entry["blockers"] = sorted(blockers)
                entry["status"] = "blocked"
                entry["detail"] = (f"insertion corridor between the mating "
                                   f"face and the {mdir} edge is occupied by: "
                                   + ", ".join(sorted(blockers)))
                violations.append({"type": "blocked", "ref": ref,
                                   "refs": sorted(blockers),
                                   "detail": entry["detail"]})

        results.append(entry)

    for r, v in sorted(bad_decl.items()):
        violations.append({"type": "bad_declaration", "ref": r,
                           "detail": f"--declare mating_dir {v!r} is not one of "
                                     f"{sorted(_VALID)}; this part was checked "
                                     f"from geometry instead"})

    results.sort(key=lambda e: e["ref"])
    bad = {"faces_inward", "not_at_edge", "blocked", "undetermined"}
    return {
        "ok": True,
        "pcb_path": pcb_path,
        "hard_fail": any(e["status"] in bad for e in results),
        "connector_count": sum(1 for e in results if e["kind"] == "connector"),
        "switch_count": sum(1 for e in results if e["kind"] == "switch"),
        "thresholds": {"max_gap_mm": max_gap, "max_overhang_mm": max_overhang,
                       "min_confidence_mm": MIN_CONFIDENCE_MM},
        # parts that left the gate unchecked — report every one with its reason
        "silenced_count": len(silenced),
        "silenced": silenced,
        "unclassified_count": len(unclassified),
        "unclassified": unclassified,
        "connectors": results,
        "violation_count": len(violations),
        "violations": violations,
        "warnings": warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Connector physical-usability gate → JSON")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--declare",
                    help='JSON file: {"CN8": {"mating_dir": "-Y", "note": "..."}}')
    ap.add_argument("--max-gap", type=float, default=2.0,
                    help="max mm the mating face may sit behind its board edge "
                         "(default 2.0)")
    ap.add_argument("--max-overhang", type=float, default=1.0,
                    help="max mm the body may hang past the edge (default 1.0)")
    ap.add_argument("--include", help="extra refs to treat as connectors")
    ap.add_argument("--exclude", help="refs to skip entirely")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    declare = None
    if args.declare:
        if not Path(args.declare).exists():
            print(json.dumps({"ok": False,
                              "error": f"not found: {args.declare}"}))
            return 1
        declare = json.loads(Path(args.declare).read_text(encoding="utf-8"))

    def _set(v):
        return {r.strip() for r in v.split(",") if r.strip()} if v else None

    try:
        result = check_connector_access(
            args.pcb, declare=declare, max_gap=args.max_gap,
            max_overhang=args.max_overhang,
            include=_set(args.include), exclude=_set(args.exclude))
    except Exception as e:  # noqa: BLE001 - tool boundary
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 2 = gate failed, 1 = tool could not run, 0 = clean. A shell wiring this
    # into `&&` must not read a failed gate as success.
    return 2 if result.get("hard_fail") else 0


if __name__ == "__main__":
    sys.exit(main())
