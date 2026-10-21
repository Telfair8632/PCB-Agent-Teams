#!/usr/bin/env python3
"""draw-pcb toolbox tool: check_placement

The LEGALITY GATE for AI-driven placement. It answers one question — "is this
placement physically legal" — as hard pass/fail. It is deliberately NOT a
quality score: minimising a scalar is how the deleted SA placement failed, and
an LLM minimising a scalar would fail the same way. Placement QUALITY (tight
loops, sane orientation, EMC, "looks right") is the AI's judgement, made from
placement_brief + the rendered image — never folded into this number.

HARD checks (any one → hard_fail, gate blocks):
  courtyard_overlap   two components physically collide
  out_of_board        a courtyard extends past Edge.Cuts
  pad_clearance       two pads of different nets closer than --min-clearance
  barrier_crossing    a NON-isolation courtyard straddles the barrier X
                      (true isolation devices are auto-exempt — they must
                      straddle it; detected via placement_brief)

Reported metric (NOT part of the gate, just a hint the AI may weigh):
  hpwl_mm   sum of per-net half-perimeter wire length — a compactness hint

Output JSON:
  hard_fail (bool — the gate), score (0-100 legality progress), metrics{},
  violations[]

Usage:
  check_placement.py <board.kicad_pcb> [--min-clearance MM] [--barrier-x MM]
                     [--barrier-exempt R1,U2]
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
from math import hypot
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from get_geometry import get_geometry  # noqa: E402
from placement_brief import build_brief  # noqa: E402

HARD_PENALTY = 15.0   # score points lost per HARD violation
WARN_PENALTY = 3.0    # score points lost per WARNING
EDGE_MM = 3.0         # a connector within this of a board edge counts as "on edge"
CAP_NEAR_MM = 2.0     # decoupling cap nearest-pad-to-IC-pad gap beyond this is flagged


def _aabb_gap(a: dict, b: dict) -> float:
    """Euclidean gap between two axis-aligned boxes. 0.0 means they overlap."""
    dx = max(0.0, a["min_x"] - b["max_x"], b["min_x"] - a["max_x"])
    dy = max(0.0, a["min_y"] - b["max_y"], b["min_y"] - a["max_y"])
    return hypot(dx, dy)


def _pad_box(p: dict) -> dict | None:
    if p.get("x") is None or not p.get("w"):
        return None
    hw, hh = p["w"] / 2.0, p["h"] / 2.0
    return {"min_x": p["x"] - hw, "max_x": p["x"] + hw,
            "min_y": p["y"] - hh, "max_y": p["y"] + hh}


def _min_pad_gap(a: dict, b: dict) -> float | None:
    """Nearest pad-to-pad gap between two footprints, mm — the true
    electrical distance (courtyard-to-courtyard overstates it)."""
    best = None
    for pa in a.get("pads", []):
        ba = _pad_box(pa)
        if not ba:
            continue
        for pb in b.get("pads", []):
            bb = _pad_box(pb)
            if not bb:
                continue
            g = _aabb_gap(ba, bb)
            best = g if best is None else min(best, g)
    return best


def check_placement(pcb_path: str, min_clearance: float = 0.2,
                    barrier_x: float | None = None,
                    barrier_exempt: set[str] | None = None,
                    decoupling_pairs: dict | None = None) -> dict:
    geo = get_geometry(pcb_path, with_pads=True)
    fps = [f for f in geo["footprints"] if f.get("courtyard")]
    violations: list[dict] = []

    # Isolation devices MUST straddle the barrier — exempt them from the
    # barrier-crossing check. Auto-detect via placement_brief (a barrier
    # device bridges >=2 ground nets); --barrier-exempt adds manual ones.
    exempt: set[str] = set(barrier_exempt or set())
    brief: dict | None = None
    try:
        brief = build_brief(pcb_path)  # barrier devices + cap-IC links
    except Exception:  # noqa: BLE001 - brief is best-effort here
        brief = None
    if barrier_x is not None and brief:
        exempt |= {b["ref"] for b in brief.get("barrier_devices", [])}

    # ── HARD: courtyard overlap ───────────────────────────────────────────
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            a, b = fps[i], fps[j]
            if a["layer"] != b["layer"]:
                continue  # opposite sides never collide
            if _aabb_gap(a["courtyard"], b["courtyard"]) == 0.0:
                violations.append({
                    "type": "courtyard_overlap", "severity": "hard",
                    "refs": [a["ref"], b["ref"]],
                    "detail": f"{a['ref']} and {b['ref']} courtyards intersect",
                })

    # ── HARD: out of board ────────────────────────────────────────────────
    board = geo.get("board")
    if board:
        bx0, by0 = board["min_x"], board["min_y"]
        bx1, by1 = bx0 + board["w"], by0 + board["h"]
        for f in fps:
            c = f["courtyard"]
            if (c["min_x"] < bx0 or c["max_x"] > bx1
                    or c["min_y"] < by0 or c["max_y"] > by1):
                violations.append({
                    "type": "out_of_board", "severity": "hard",
                    "refs": [f["ref"]],
                    "detail": f"{f['ref']} courtyard extends past Edge.Cuts",
                })

    # ── HARD: pad-to-pad clearance (different nets, same layer) ───────────
    pads = []
    for f in fps:
        for p in f.get("pads", []):
            box = _pad_box(p)
            if box:
                pads.append((f["ref"], p.get("number"), p.get("net"), box))
    for i in range(len(pads)):
        for j in range(i + 1, len(pads)):
            ra, na, neta, ba = pads[i]
            rb, nb, netb, bb = pads[j]
            if ra == rb:
                continue            # same footprint — by design
            if neta and neta == netb:
                continue            # same net — touching is fine
            gap = _aabb_gap(ba, bb)
            if gap < min_clearance:
                violations.append({
                    "type": "pad_clearance", "severity": "hard",
                    "refs": [ra, rb],
                    "detail": (f"{ra}.{na} ↔ {rb}.{nb} gap {gap:.3f}mm "
                               f"< {min_clearance}mm"),
                })

    # ── HARD: isolation barrier crossing (isolation devices exempt) ───────
    if barrier_x is not None:
        for f in fps:
            if f["ref"] in exempt:
                continue  # isolation device — straddling is correct
            c = f["courtyard"]
            if c["min_x"] < barrier_x < c["max_x"]:
                violations.append({
                    "type": "barrier_crossing", "severity": "hard",
                    "refs": [f["ref"]],
                    "detail": (f"{f['ref']} (not an isolation device) "
                               f"straddles barrier x={barrier_x}"),
                })

    # ── METRIC: HPWL (half-perimeter wire length) ─────────────────────────
    from collections import defaultdict
    net_xy: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for f in fps:
        for p in f.get("pads", []):
            if p.get("net") and p.get("x") is not None:
                net_xy[p["net"]].append((p["x"], p["y"]))
    hpwl = 0.0
    for pts in net_xy.values():
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        hpwl += (max(xs) - min(xs)) + (max(ys) - min(ys))

    # ── score ─────────────────────────────────────────────────────────────
    hard = [v for v in violations if v["severity"] == "hard"]
    warn = [v for v in violations if v["severity"] == "warning"]
    score = max(0.0, 100.0 - HARD_PENALTY * len(hard) - WARN_PENALTY * len(warn))

    # Where each footprint's extent came from. `graphics` (body drawings) is a
    # sound proxy; `pad_bbox` means the extent under-reports the real body.
    extent_source = {"courtyard": 0, "graphics": 0, "pad_bbox": 0}
    for f in fps:
        extent_source[f["courtyard"].get("_from", "courtyard")] += 1

    metrics = {
        "hpwl_mm": round(hpwl, 2),
        "courtyard_overlaps": sum(1 for v in hard
                                  if v["type"] == "courtyard_overlap"),
        "out_of_board": sum(1 for v in hard if v["type"] == "out_of_board"),
        "pad_clearance_violations": sum(1 for v in hard
                                        if v["type"] == "pad_clearance"),
        "barrier_crossings": sum(1 for v in hard
                                 if v["type"] == "barrier_crossing"),
        "extent_source": extent_source,
        # Self-attesting gate state: without these, "check skipped" and
        # "checked and clean" produce identical output — the caller cannot
        # tell a disarmed gate from a passing one.
        "barrier_check": "run" if barrier_x is not None else "skipped_no_flag",
        "decoupling_pairing": "declared" if decoupling_pairs else "net_inferred",
    }

    # Footprints with no courtyard AND no body graphics — their extent is a
    # pad-bbox guess smaller than the real body, so this gate may MISS a real
    # overlap. KiCad's DRC does NOT back this up: its courtyard check needs
    # the CrtYd layer too, so a library without it is blind in BOTH tools.
    # Surface these so the AI verifies against a render / datasheet instead.
    uncertain = sorted(f["ref"] for f in fps if f.get("geometry_uncertain"))
    warnings = []
    if uncertain:
        warnings.append({
            "type": "geometry_uncertain",
            "refs": uncertain,
            "detail": (f"{len(uncertain)} footprint(s) have neither courtyard "
                       f"nor body graphics — overlap check used a pad-bbox "
                       f"guess that under-reports the body, and KiCad DRC is "
                       f"equally blind without CrtYd; verify spacing visually"),
        })

    # Connectors / switches sitting mid-board — wires/cables can't reach,
    # a switch can't be operated. Not a hard fail (placement is legal), but
    # a real quality problem the AI must fix (see loop.md Phase B priority).
    if board:
        bx0, by0 = board["min_x"], board["min_y"]
        bx1, by1 = bx0 + board["w"], by0 + board["h"]
        # Which parts count as interface parts comes from placement_brief's
        # edge_devices (footprint-name based). A refdes-prefix rule here would
        # re-open the blind spot the shared identifier exists to close.
        edge_dev = {e["ref"]: e for e in (brief or {}).get("edge_devices", [])}
        off_edge = []
        for f in fps:
            e = edge_dev.get(f["ref"])
            if e is None:
                continue
            c = f["courtyard"]
            lo_x, hi_x = c["min_x"], c["max_x"]
            lo_y, hi_y = c["min_y"], c["max_y"]
            # Measure from the real body: a degenerate courtyard is the pad
            # bbox, which sits mm inside a connector's shell and would report
            # a correctly edge-mounted part as "mid-board".
            bb = e.get("body_bbox")
            if bb:
                lo_x, hi_x = min(lo_x, bb[0]), max(hi_x, bb[1])
                lo_y, hi_y = min(lo_y, bb[2]), max(hi_y, bb[3])
            gap = min(lo_x - bx0, bx1 - hi_x, lo_y - by0, by1 - hi_y)
            if gap > EDGE_MM:
                off_edge.append((f["ref"], round(gap, 1)))
        if off_edge:
            warnings.append({
                "type": "connector_not_on_edge",
                "refs": sorted(r for r, _ in off_edge),
                "detail": (f"{len(off_edge)} connector/switch(es) sit "
                           f">{EDGE_MM}mm from every board edge — move them "
                           f"to the perimeter so wires/cables can reach: "
                           + ", ".join(f"{r}({g}mm)" for r, g in
                                       sorted(off_edge))),
            })

    # Decoupling-cap proximity. The AUTHORITATIVE cap→IC pairing is the
    # project's declared decoupling_pairs (--decoupling-pairs); placement_brief
    # net-inference can mis-pair a cap to the wrong IC, so it is only a
    # fallback and the warning then says so. Distance is the real nearest
    # pad-to-pad gap (courtyard-to-courtyard overstates it).
    if decoupling_pairs:
        pairs = list(decoupling_pairs.items())
        inferred = False
    elif brief:
        pairs = [(l["cap"], l["ic"]) for l in brief.get("cap_ic_links", [])]
        inferred = True
    else:
        pairs, inferred = [], False
    if pairs:
        fp_by_ref = {f["ref"]: f for f in fps}
        far = []
        for cap, ic in pairs:
            ca, icf = fp_by_ref.get(cap), fp_by_ref.get(ic)
            if not (ca and icf):
                continue
            gap = _min_pad_gap(ca, icf)
            if gap is not None and gap > CAP_NEAR_MM:
                far.append((cap, ic, round(gap, 2)))
        if far:
            tail = (" (pairing INFERRED from nets, may be wrong — pass "
                    "--decoupling-pairs from the project CLAUDE.md)"
                    if inferred else "")
            warnings.append({
                "type": "cap_far_from_ic",
                "refs": sorted({c for c, _, _ in far}),
                "detail": (f"{len(far)} decoupling cap(s) with nearest pad "
                           f">{CAP_NEAR_MM}mm from their IC{tail}: "
                           + ", ".join(f"{c}→{i}({g}mm)" for c, i, g in far)),
            })

    return {
        "ok": True,
        "pcb_path": pcb_path,
        "score": round(score, 1),
        "hard_fail": len(hard) > 0,
        "metrics": metrics,
        "violation_count": len(violations),
        "violations": violations,
        "warnings": warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Placement scoring gate → JSON")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--min-clearance", type=float, default=0.2,
                    help="min pad-to-pad gap in mm (default 0.2)")
    ap.add_argument("--barrier-x", type=float,
                    help="isolation barrier X — flag non-isolation courtyards "
                         "straddling it (isolation devices auto-exempt)")
    ap.add_argument("--barrier-exempt",
                    help="extra refs exempt from barrier check (comma-separated)")
    ap.add_argument("--decoupling-pairs",
                    help="authoritative cap:IC pairs from the project CLAUDE.md, "
                         "e.g. 'C6:U1,C10:U1,C5:U3' — used for cap_far_from_ic")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    exempt = None
    if args.barrier_exempt:
        exempt = {r.strip() for r in args.barrier_exempt.split(",") if r.strip()}

    decoupling = None
    if args.decoupling_pairs:
        decoupling = {}
        for tok in args.decoupling_pairs.split(","):
            cap, _, ic = tok.strip().partition(":")
            if cap.strip() and ic.strip():
                decoupling[cap.strip()] = ic.strip()

    try:
        result = check_placement(args.pcb, min_clearance=args.min_clearance,
                                 barrier_x=args.barrier_x,
                                 barrier_exempt=exempt,
                                 decoupling_pairs=decoupling)
    except Exception as e:  # noqa: BLE001 - tool boundary
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
