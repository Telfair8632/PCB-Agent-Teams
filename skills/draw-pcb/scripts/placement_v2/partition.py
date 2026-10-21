"""Phase A — partition components into regions by net-regex majority vote.

Generic mechanism, zero project-specific keywords baked in. Default regex
sets cover the common HV/LV/ISO galvanic-isolation case but a project's
CLAUDE.md can override entirely (placement.regex.<region> = "<pattern>").

Algorithm:
    1. For each footprint, walk its pad nets, count regex hits per region.
    2. Majority vote → assigned region. Ties or zero hits go to fallback.
    3. Fallback: graph adjacency. Build undirected graph (nodes=footprints,
       edges=shared non-power net), assign each unresolved node to the
       region with the most votes among its neighbors. Iterates until
       stable (typically 1-2 passes).
    4. Manual anchors override everything (CLAUDE.md placement.anchors).

Pure stdlib + networkx (already in venv). Runs under .venv python — does
NOT call pcbnew. Caller must pass extracted netlist data.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

# Default value/MPN regex — components whose part name or value is a
# strong region signal regardless of which nets touch them. Iso DC-DCs
# (TMA 0505S, B0505, IB0505) straddle the barrier with +5V on both
# sides, so net-vote alone classifies them LV; value match anchors them
# to ISO. Same for analog isolators (AMC, ACPL, ADuM).
DEFAULT_VALUE_REGEX: Dict[str, str] = {
    "ISO": r"(?i)(amc\d|isow|iso\d|acpl|hcpl|si8[0-9]|adum\d|tma\s*\d|b\d{4}s|ib\d{4}|nve)",
}

# Vote weight for a value/MPN hit relative to one net hit. Set to 5 so
# a single value match outvotes 4 net hits — iso DC-DC's case.
VALUE_VOTE_WEIGHT = 5

# Default regex set — projects without isolation barriers will only use
# 'LV' (everything maps there) which is fine. Galvanic-isolation projects
# get HV/ISO out of the box; everything else can override via CLAUDE.md.
#
# Token-based matching (no \b): KiCad nets routinely use '_' as a
# separator (HV_GND, AMC_IN, 5V_ISO). Python's \b doesn't break on '_'
# because '_' is a word char, so \b-anchored patterns silently miss
# almost every typed net. Substring match is acceptable here — false
# positives like 'gnd' matching '/GROUND_RETURN' are harmless because
# the net IS a ground.
DEFAULT_REGION_REGEX: Dict[str, str] = {
    "HV": r"(?i)(hv_gnd|hv_div|hv_sense|hv\+|hv\-|vinp|vinn|vdd1|primary)|(?:^|[/_+\-])hv(?:[/_+\-]|$)",
    "ISO": r"(?i)(iso|isow|amc|acpl|hcpl|si86|adum)",
    "LV": r"(?i)(3v3|3\.3v|vcc|vdda|vdd2|secondary|lv_gnd|agnd|dgnd|gnda)|(?:^|[/_+\-])(?:\+?5v|gnd)(?:[/_+\-]|$)",
}

# Nets that are universal and should NOT count as adjacency for graph
# fallback — every footprint touches GND, so GND-based adjacency is noise.
POWER_NET_REGEX = re.compile(
    r"(?i)(hv_gnd|lv_gnd|agnd|dgnd|gnda|vcc|vdd1|vdd2)|"
    r"(?:^|[/_+\-])(gnd|\+?5v|3v3|3\.3v)(?:[/_+\-]|$)"
)


def _vote_for_footprint(nets: Iterable[str],
                        compiled_regex: Mapping[str, re.Pattern]) -> Counter:
    """Count regex hits across all nets a footprint touches.

    Each (net, region) match contributes one vote. A net hitting two
    regions counts for both — caller's regex set is responsible for being
    mutually exclusive if that matters.
    """
    votes: Counter = Counter()
    for net in nets:
        if not net:
            continue
        for region, pat in compiled_regex.items():
            if pat.search(net):
                votes[region] += 1
    return votes


def _build_adjacency(footprint_nets: Mapping[str, Set[str]]) -> Dict[str, Set[str]]:
    """Graph adjacency: footprint→neighbors (sharing any non-power net)."""
    # Reverse map: net → set of footprints touching it
    net_to_fps: Dict[str, Set[str]] = defaultdict(set)
    for ref, nets in footprint_nets.items():
        for n in nets:
            if n and not POWER_NET_REGEX.search(n):
                net_to_fps[n].add(ref)
    # Adjacency
    adj: Dict[str, Set[str]] = defaultdict(set)
    for fps in net_to_fps.values():
        if len(fps) < 2:
            continue
        fps_list = list(fps)
        for i, a in enumerate(fps_list):
            for b in fps_list[i + 1:]:
                adj[a].add(b)
                adj[b].add(a)
    return adj


def partition_components(
    footprint_nets: Mapping[str, Set[str]],
    footprint_values: Optional[Mapping[str, str]] = None,
    region_regex: Optional[Mapping[str, str]] = None,
    value_regex: Optional[Mapping[str, str]] = None,
    anchors: Optional[Mapping[str, str]] = None,
    fallback_region: str = "LV",
) -> Tuple[Dict[str, str], Dict[str, Dict]]:
    """Assign each footprint a region label.

    Args:
        footprint_nets: {ref → set(net_name)}
        footprint_values: {ref → value_string} — footprint Value field
                          (e.g. 'TMA 0505S'). Optional; enables value-
                          based voting. Highly recommended for boards
                          with iso DC-DCs / opto-isolators that straddle
                          the barrier and net-vote alone misclassifies.
        region_regex: {region → pattern} for net matching; defaults
                      DEFAULT_REGION_REGEX.
        value_regex: {region → pattern} for value matching; defaults
                     DEFAULT_VALUE_REGEX. Each value match contributes
                     VALUE_VOTE_WEIGHT votes (5x) to outweigh net votes
                     for barrier-straddling parts.
        anchors: {ref → region_label} — manual overrides, win unconditionally.
        fallback_region: assigned to footprints that vote zero AND have
                         no adjacency (e.g. orphan test points). Defaults
                         to 'LV' which is a safe place for unknowns.

    Returns:
        (assignments, diagnostics)
        assignments: {ref → region}
        diagnostics: {ref → {'votes': dict, 'method': str, 'tied': bool}}
        method ∈ {'anchor', 'majority', 'tie_broken_by_adjacency',
                  'adjacency_fallback', 'fallback_default'}
    """
    rules = dict(region_regex) if region_regex else dict(DEFAULT_REGION_REGEX)
    compiled = {r: re.compile(p) for r, p in rules.items()}
    val_rules = dict(value_regex) if value_regex else dict(DEFAULT_VALUE_REGEX)
    val_compiled = {r: re.compile(p) for r, p in val_rules.items()}
    values_map = dict(footprint_values or {})
    anchors = dict(anchors or {})

    assignments: Dict[str, str] = {}
    diagnostics: Dict[str, Dict] = {}

    # Step 1: explicit anchors win.
    for ref, region in anchors.items():
        if ref in footprint_nets:
            assignments[ref] = region
            diagnostics[ref] = {"votes": {}, "method": "anchor", "tied": False}

    # Step 2: vote per remaining footprint.
    pending_ties: List[str] = []
    pending_zero: List[str] = []
    for ref, nets in footprint_nets.items():
        if ref in assignments:
            continue
        votes = _vote_for_footprint(nets, compiled)
        # Value/MPN votes weigh VALUE_VOTE_WEIGHT × a single net vote so
        # one value match (e.g. 'TMA 0505S' → ISO) outranks several net
        # hits — needed for iso DC-DCs that have +5V on both sides.
        val = values_map.get(ref, "")
        if val:
            for region, pat in val_compiled.items():
                if pat.search(val):
                    votes[region] = votes.get(region, 0) + VALUE_VOTE_WEIGHT
        if not votes:
            pending_zero.append(ref)
            diagnostics[ref] = {"votes": {}, "method": None, "tied": False}
            continue
        ranked = votes.most_common()
        top_count = ranked[0][1]
        winners = [r for r, c in ranked if c == top_count]
        if len(winners) == 1:
            assignments[ref] = winners[0]
            diagnostics[ref] = {"votes": dict(votes),
                                "method": "majority", "tied": False}
        else:
            pending_ties.append(ref)
            diagnostics[ref] = {"votes": dict(votes),
                                "method": None, "tied": True}

    # Step 3: graph adjacency for ties + zero-vote.
    if pending_ties or pending_zero:
        adj = _build_adjacency({ref: set(n) for ref, n in footprint_nets.items()})
        # Iterate up to 3 passes — each pass uses already-resolved neighbors.
        for _ in range(3):
            still_pending = []
            for ref in pending_zero + pending_ties:
                if ref in assignments:
                    continue
                neighbor_votes: Counter = Counter()
                for nb in adj.get(ref, ()):
                    if nb in assignments:
                        neighbor_votes[assignments[nb]] += 1
                if not neighbor_votes:
                    still_pending.append(ref)
                    continue
                ranked = neighbor_votes.most_common()
                top = ranked[0][1]
                winners = [r for r, c in ranked if c == top]
                # Among ties, prefer the region with original regex votes.
                if len(winners) > 1 and ref in pending_ties:
                    orig_votes = diagnostics[ref]["votes"]
                    winners = [w for w in winners if w in orig_votes] or winners
                if len(winners) == 1:
                    assignments[ref] = winners[0]
                    method = ("tie_broken_by_adjacency" if ref in pending_ties
                              else "adjacency_fallback")
                    diagnostics[ref]["method"] = method
                    diagnostics[ref]["adjacency_votes"] = dict(neighbor_votes)
                else:
                    still_pending.append(ref)
            if not still_pending:
                break
            pending_zero = [r for r in pending_zero if r in still_pending]
            pending_ties = [r for r in pending_ties if r in still_pending]

    # Step 4: anything still unresolved → fallback default.
    for ref in footprint_nets:
        if ref not in assignments:
            assignments[ref] = fallback_region
            if ref not in diagnostics:
                diagnostics[ref] = {"votes": {}, "method": "fallback_default",
                                    "tied": False}
            else:
                diagnostics[ref]["method"] = "fallback_default"

    return assignments, diagnostics


def summarize(assignments: Mapping[str, str]) -> Dict[str, int]:
    """Quick {region: count} histogram for logging."""
    return dict(Counter(assignments.values()))
