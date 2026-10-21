#!/usr/bin/env python3
"""draw-pcb toolbox helper: which nets are ground / power rails.

Shared by `placement_brief` (power_nets, chain detection) and
`placement_v2/partition` (adjacency voting) so they never disagree.

Why not a name whitelist. The previous rule was a hardcoded list of net names
— and it listed `vdd1`/`vdd2` but not bare `vdd`, which is about the most
common supply-net name in existence. Measured on a real 77-net board it found
GND and nothing else: VDD, VEXT, VBUS, VBAT, VDC, VOUT_* all slipped through.
Nothing errors when that happens; the rails just quietly stop being treated as
rails, so they get routed at signal width and they pollute adjacency voting.

Why not fan-out either. "A rail touches many parts" is true but does not
separate: on that same board VBAT touched 5 footprints and so did the signal
IO0. Ranking by fan-out cannot split them.

What actually works is the electrical definition: **a supply rail is the net a
decoupling capacitor bypasses to ground**. Two or more 2-pin caps from net N to
a ground net means N carries supply current, whatever it is called. That is
name-independent, so it survives any naming convention. A broadened name rule
runs alongside it to catch rails that happen to have no local bypass cap
(a USB VBUS pin, an unloaded connector rail).

Two questions, two answers — they are NOT the same set:
  power_nets     rails that should be routed wide      → name OR bypassed
  topology_noise nets too promiscuous to imply         → power ∪ ground
                 adjacency or a series chain
"""
import re
from collections import Counter
from typing import Iterable, Mapping

# Ground. Matched as a whole token so `GND_SENSE` counts and `AGNDX` does not.
GROUND_RE = re.compile(
    r"(?i)(^|[_/+\-.])(gnd|vss|agnd|dgnd|pgnd|gnda|gndd|gndpwr|0v|vee)"
    r"([_/+\-.$]|\d|$)")

# Numeric rails: 3V3, +5V, -12V, 1V8, 3.3V.
_RAIL_NUMERIC = re.compile(r"(?i)(^|[_/+\-])[+\-]?\d+[v.]\d*(v)?([_/+\-]|$)")

# Named rails: a short V-word, optionally suffixed. Length-capped so `VSENSE`
# and `VOLTAGE_MON` do not qualify; ambiguous low-current V-names are vetoed
# below rather than being matched and then hand-waved.
_RAIL_WORD = re.compile(
    r"(?i)(^|[_/+\-])[+\-]?v(cc|dd|dda|ddio|ee|bat|bus|in|out|dc|aa|sys|pp|"
    r"core|ext|hv|lv|mot|logic|ana|io|s|_)?([_/+\-]|\d|$)")

# V-names that are references or feedback taps, not distribution rails.
_RAIL_VETO = re.compile(r"(?i)(^|[_/+\-])v(ref|sense|fb|adj|set|mon|div|"
                        r"comp|osc|peak|rms)")

# Caps bypassing a net to ground before it counts as a rail on structure alone.
BYPASS_CAP_MIN = 2


def is_ground(net: str) -> bool:
    return bool(net) and bool(GROUND_RE.search(net))


def _named_rail(net: str) -> bool:
    if not net or _RAIL_VETO.search(net):
        return False
    return bool(_RAIL_NUMERIC.search(net) or _RAIL_WORD.search(net))


def classify_nets(footprint_nets: Mapping[str, Iterable[str]],
                  cap_prefix: str = "C") -> dict:
    """→ {ground, power, topology_noise, why{net: [signals]}}.

    footprint_nets: ref → nets it touches. Available identically in
    placement_brief (from get_geometry) and partition (its own input), so the
    two cannot drift apart.
    """
    nets = {n for ns in footprint_nets.values() for n in ns if n}
    ground = {n for n in nets if is_ground(n)}

    # Structural: count 2-pin caps sitting between a net and ground.
    bypass: Counter = Counter()
    for ref, ns in footprint_nets.items():
        if not ref.startswith(cap_prefix):
            continue
        ns = {n for n in ns if n}
        if len(ns) != 2:
            continue
        g = ns & ground
        rest = ns - ground
        if len(g) == 1 and len(rest) == 1:
            bypass[rest.pop()] += 1

    why: dict[str, list[str]] = {}
    power = set()
    for n in nets - ground:
        sig = []
        if bypass[n] >= BYPASS_CAP_MIN:
            sig.append(f"bypassed_by_{bypass[n]}_caps")
        if _named_rail(n):
            sig.append("rail_name")
        if sig:
            power.add(n)
            why[n] = sig
    for n in ground:
        why[n] = ["ground_name"]

    return {"ground": sorted(ground), "power": sorted(power),
            # Rails and grounds reach across the whole board, so two parts
            # sharing one says nothing about whether they belong together.
            "topology_noise": sorted(power | ground),
            "why": why}
