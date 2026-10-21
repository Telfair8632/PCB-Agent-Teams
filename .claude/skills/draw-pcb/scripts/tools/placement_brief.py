#!/usr/bin/env python3
"""draw-pcb toolbox tool: placement_brief

The circuit-comprehension layer for AI-driven placement. The other tools are
the AI's *eyes* (where things are); this one gives it the *circuit facts* it
must place against — so the AI places loop-first, not grid-first.

It extracts MECHANICAL facts only. It does NOT decide "this loop is EMC-
critical" or "place these 3mm apart" — that judgement is the AI's job, made
on top of this brief plus the project CLAUDE.md placement intent.

Extracted:
  edge_devices     connectors / switches + which way each one has to face, and
                   the rotation that achieves it (the fact placement needs
                   before it moves anything)
  power_nets       supply rails, found by structure (≥2 decoupling caps to
                   ground) OR name — routing must widen these
  domains          each footprint → HV / ISO / LV (net-name + value regex vote)
  barrier_devices  footprints whose pads span two domains — must be oriented
                   so each domain's pads face that domain (the rotation fact)
  decoupling       cap bridging a power net and ground, where that power net
                   also reaches an IC → "this cap decouples that IC"
  chains           series strings of 2-terminal parts (e.g. a divider R1..R5)
  net_pads         net → [ref.pad] — the raw connectivity for loop reasoning

Usage:
  placement_brief.py <board.kicad_pcb>

Reuses placement_v2/partition.py's domain regex so the brief and init_layout
classify domains the same way.
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
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from get_geometry import get_geometry  # noqa: E402
from connector_id import classify  # noqa: E402
from net_roles import classify_nets  # noqa: E402
from check_connector_access import (MIN_CONFIDENCE_MM, angle_for_edge,  # noqa: E402
                                    mating_sides)

# Reuse init_layout's domain regex — brief and partition must agree.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "placement_v2"))
try:
    from partition import (DEFAULT_NET_REGEX, DEFAULT_VALUE_REGEX,  # noqa: E402
                           POWER_NET_REGEX)
except ImportError:  # pragma: no cover - fall back to local copy
    DEFAULT_NET_REGEX = {
        "HV": re.compile(r"(?i)(hv_gnd|hv_div|hv_sense|hv\+|hv\-|vinp|vinn|"
                         r"vdd1|primary)|(?:^|[/_+\-])hv(?:[/_+\-]|$)"),
        "ISO": re.compile(r"(?i)(iso|isow|amc|acpl|hcpl|si86|adum)"),
        "LV": re.compile(r"(?i)(3v3|3\.3v|vcc|vdda|vdd2|secondary|lv_gnd|"
                         r"agnd|dgnd|gnda)|(?:^|[/_+\-])(?:\+?5v|gnd)"
                         r"(?:[/_+\-]|$)"),
    }
    DEFAULT_VALUE_REGEX = {
        "ISO": re.compile(r"(?i)(amc\d|isow|iso\d|acpl|hcpl|si8[0-9]|adum\d|"
                          r"tma\s*\d|b\d{4}s|ib\d{4}|nve)"),
    }
    POWER_NET_REGEX = re.compile(r"(?i)(hv_gnd|lv_gnd|agnd|dgnd|gnda|vcc|"
                                 r"vdd1|vdd2)|(?:^|[/_+\-])(gnd|\+?5v|3v3|"
                                 r"3\.3v)(?:[/_+\-]|$)")

_GND_RE = re.compile(r"(?i)gnd")


def _net_domain(net: str) -> str | None:
    """HV / ISO / LV for a net name, or None if it matches nothing."""
    hits = [r for r, rx in DEFAULT_NET_REGEX.items() if rx.search(net)]
    if not hits:
        return None
    # A net hitting several regions is ambiguous (e.g. shared GND) — drop it.
    return hits[0] if len(hits) == 1 else None


def _footprint_domain(fp: dict) -> str:
    """Vote a footprint into a domain: value/MPN match (strong) then net votes."""
    votes: dict[str, int] = defaultdict(int)
    val = fp.get("value", "")
    for region, rx in DEFAULT_VALUE_REGEX.items():
        if rx.search(val):
            votes[region] += 5  # value match outweighs net votes
    for net in fp.get("nets", []):
        d = _net_domain(net)
        if d:
            votes[d] += 1
    if not votes:
        return "LV"  # fallback — projects without isolation are all-LV
    return max(votes, key=votes.get)


def build_brief(pcb_path: str) -> dict:
    geo = get_geometry(pcb_path, with_pads=True)
    fps = geo["footprints"]
    by_ref = {f["ref"]: f for f in fps}

    # net → [ref.pad]
    net_pads: dict[str, list[str]] = defaultdict(list)
    for f in fps:
        for p in f.get("pads", []):
            if p.get("net"):
                net_pads[p["net"]].append(f"{f['ref']}.{p.get('number')}")

    # Rails are found by structure first (a supply is what ≥2 decoupling caps
    # bypass to ground) and by name second — a name whitelist silently misses
    # VDD / VEXT / VBUS and then everything downstream routes them as signals.
    roles = classify_nets({f["ref"]: set(f.get("nets", [])) for f in fps})
    power_nets = sorted(n for n in roles["power"] if n in net_pads)
    ground_nets = sorted(n for n in roles["ground"] if n in net_pads)
    noise_nets = set(roles["topology_noise"])

    # domains
    footprint_domain = {f["ref"]: _footprint_domain(f) for f in fps}
    domains: dict[str, list[str]] = defaultdict(list)
    for ref, d in footprint_domain.items():
        domains[d].append(ref)

    # edge_devices — interface parts must sit on the board perimeter: cables
    # attach at the edge, a switch has to be reachable. Identification is by
    # footprint library name first (see connector_id) — a refdes-prefix rule
    # silently drops every connector on a board ported from another EDA tool,
    # and a part nobody flags is a part nobody places at the edge.
    #
    # Each entry also carries the footprint's mating side and the rotation that
    # aims it at each board edge, so orientation is decided *before* placement.
    # Hugging the edge with the opening pointing inward passes every other
    # check and yields a board that cannot be plugged in.
    sides = mating_sides(pcb_path)
    edge_devices = []
    for f in fps:
        ref = f["ref"]
        info = sides.get(ref, {})
        kind = classify(ref, f.get("footprint", ""),
                        body_overhang_mm=info.get("body_overhang_mm", 0.0))
        if kind is None:
            continue
        local = info.get("local_side")
        entry = {
            "ref": ref, "value": f.get("value", ""),
            "footprint": f.get("footprint", ""), "kind": kind,
            "mating_side_local": local,
            "mating_confidence_mm": info.get("confidence_mm"),
            # pads ∪ body outline, board coords — the extent any edge-distance
            # test must use (courtyard degrades to the pad bbox on libraries
            # that ship no CrtYd, understating a connector by several mm).
            "body_bbox": info.get("body_bbox"),
        }
        # Only hand out a rotation table when the mating side is actually
        # known. A table computed from a 0.01mm asymmetry reads exactly as
        # authoritative as a real one — and gets followed.
        if local and (info.get("confidence_mm") or 0.0) >= MIN_CONFIDENCE_MM:
            entry["angle_for_edge"] = {
                d: angle_for_edge(local, d) for d in ("-X", "+X", "-Y", "+Y")}
        else:
            entry["mating_side_local"] = None
            entry["mating_unknown_reason"] = (
                info.get("reason")
                or f"no side won by ≥{MIN_CONFIDENCE_MM}mm "
                   f"(best {info.get('confidence_mm')}mm) — read the datasheet "
                   f"and declare mating_dir")
        edge_devices.append(entry)
    edge_devices.sort(key=lambda e: e["ref"])

    # barrier devices — the rotation-critical parts. A galvanic-isolation
    # device is the thing that bridges two ground domains, so its true
    # signature is "pads touch >=2 distinct ground nets". This catches the
    # isolation amplifier AND the isolated DC-DC even when net-name regex
    # mis-domains them. For each, dump pads+nets so the AI can work out
    # which way to rotate it across the barrier.
    barrier_devices = []
    for f in fps:
        gnds = sorted({p["net"] for p in f.get("pads", [])
                       if p.get("net") and _GND_RE.search(p["net"])})
        if len(gnds) >= 2:
            barrier_devices.append({
                "ref": f["ref"], "value": f.get("value", ""),
                "bridges_grounds": gnds,
                "pads": [{"number": p.get("number"), "net": p.get("net")}
                         for p in f.get("pads", [])],
                "note": ("isolation device — must straddle the barrier; "
                         "orient so each ground domain's pads face that side"),
            })

    # cap ↔ IC links — a 2-pad cap bridging a power/signal net and ground,
    # where that net also reaches an IC. Mechanical fact only: "keep this
    # cap near this IC". Whether it is decoupling / filter / bulk is the
    # AI's call.
    ic_nets: dict[str, set[str]] = {}
    for f in fps:
        if f["ref"][:1] == "U":
            ic_nets[f["ref"]] = set(f.get("nets", []))
    cap_ic_links = []
    for f in fps:
        if f["ref"][:1] != "C" or f.get("pad_count") != 2:
            continue
        nets = [p.get("net") for p in f.get("pads", []) if p.get("net")]
        if len(nets) != 2:
            continue
        gnd = [n for n in nets if _GND_RE.search(n)]
        sig = [n for n in nets if not _GND_RE.search(n)]
        if not (gnd and sig):
            continue
        for ic, icn in ic_nets.items():
            if sig[0] in icn:
                cap_ic_links.append({"cap": f["ref"], "ic": ic,
                                     "via_net": sig[0], "gnd": gnd[0]})
                break

    # chains — series strings of 2-terminal parts (R/L). Built per domain so
    # a chain never crosses a domain boundary, and walked from an endpoint so
    # `members` is in physical series order (e.g. a divider R1..R5).
    twoterm = [f["ref"] for f in fps
               if f["ref"][:1] in ("R", "L") and f.get("pad_count") == 2]
    adj: dict[str, set[str]] = defaultdict(set)
    for net, pads in net_pads.items():
        if net in noise_nets:
            continue  # power/gnd nets connect everything — not a chain signal
        refs = sorted({pd.split(".")[0] for pd in pads} & set(twoterm))
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                adj[refs[i]].add(refs[j])
                adj[refs[j]].add(refs[i])

    def _walk_ordered(members: set[str]) -> list[str]:
        """Order a chain by walking from a degree-1 endpoint within `members`."""
        sub = {m: (adj[m] & members) for m in members}
        ends = sorted([m for m, nb in sub.items() if len(nb) <= 1])
        start = ends[0] if ends else sorted(members)[0]
        order, cur, prev = [start], start, None
        while True:
            nxt = [n for n in sorted(sub[cur]) if n != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            if cur in order:
                break
            order.append(cur)
        # any members not on the walked path (branches) — append sorted
        return order + sorted(members - set(order))

    chains = []
    seen: set[str] = set()
    for start in twoterm:
        if start in seen or not adj[start]:
            continue
        comp, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.add(n)
            stack += [m for m in adj[n] if m not in seen]
        # split the connected component by domain — no cross-domain chains
        by_dom: dict[str, set[str]] = defaultdict(set)
        for m in comp:
            by_dom[footprint_domain.get(m, "LV")].add(m)
        for dom, members in by_dom.items():
            if len(members) >= 2:
                chains.append({"members": _walk_ordered(members),
                               "domain": dom, "kind": "series"})

    # barrier_x — the isolation barrier line, = mean x of the isolation
    # devices' centres (they straddle it). Only meaningful AFTER placement:
    # on an unplaced board every footprint sits at the origin, so the mean
    # would be a garbage ~0. Gate on the board outline existing (init_layout
    # draws it) — before that, barrier_x is null and Phase A must read the
    # slot from the project CLAUDE.md / init_layout output instead.
    barrier_x = None
    if geo.get("board"):
        bx = [by_ref[b["ref"]]["center"][0] for b in barrier_devices
              if by_ref.get(b["ref"], {}).get("center")]
        if bx:
            barrier_x = round(sum(bx) / len(bx), 3)

    return {
        "ok": True,
        "pcb_path": pcb_path,
        "footprint_count": len(fps),
        "barrier_x": barrier_x,
        "domains": {k: sorted(v) for k, v in sorted(domains.items())},
        "footprint_domain": footprint_domain,
        "edge_devices": edge_devices,
        "barrier_devices": barrier_devices,
        "cap_ic_links": cap_ic_links,
        "chains": chains,
        "power_nets": power_nets,
        # why each net was called a rail — so a wrong call is auditable
        "power_net_why": {n: roles["why"][n] for n in power_nets},
        "ground_nets": ground_nets,
        "net_pads": {n: sorted(p) for n, p in sorted(net_pads.items())},
        "_note": ("Mechanical facts only. EMC-criticality, exact spacing and "
                  "loop priority are the AI's judgement, made with the "
                  "project CLAUDE.md placement intent."),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Circuit facts for placement → JSON")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    args = ap.parse_args()
    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1
    try:
        result = build_brief(args.pcb)
    except Exception as e:  # noqa: BLE001 - tool boundary
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
