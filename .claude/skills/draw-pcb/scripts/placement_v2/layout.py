"""Phase C — deterministic region-based layout (no SA, no cost function).

Each region rect from Phase B becomes a grid. Footprints are laid out
in-order on the grid with two pre-arrangement rules:

  • decoupling_pairs [(a, b)]: cap is snapped immediately east of its IC
    so the routed trace is short (declared "place these close").
  • chains [a, b, c, ...]: members occupy consecutive grid cells in the
    declared order.

No cost function, no annealing — the goal is "good-enough zoning", with
final touch-up done by hand in the KiCad GUI. Slot avoidance is handled
at the floorplan level (region rect excludes the slot) plus a runtime
keepout check that skips any grid cell overlapping a keepout rect.
"""

from __future__ import annotations


# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

# Tight offset used when snapping a declared cap to its IC. Leaves enough
# room for the cap body without overlapping the IC courtyard.
ADJACENCY_GAP_MM = 0.5


@dataclass
class PlacementProblem:
    """Inputs for the region layout."""
    refs: List[str]
    fp_sizes: Dict[str, Tuple[float, float]]
    region_rect: Tuple[float, float, float, float]   # x, y, w, h
    chains: List[Sequence[str]] = field(default_factory=list)
    decoupling_pairs: List[Tuple[str, str]] = field(default_factory=list)
    keepouts: List[Tuple[float, float, float, float]] = field(default_factory=list)


def _cell_hits_keepout(cx: float, cy: float, w: float, h: float,
                       keepouts: Sequence[Tuple[float, float, float, float]]) -> bool:
    if not keepouts:
        return False
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    for kx, ky, kw, kh in keepouts:
        if x1 <= kx or x0 >= kx + kw or y1 <= ky or y0 >= ky + kh:
            continue
        return True
    return False


def _is_passive(ref: str) -> bool:
    return ref[:1] in ("C", "R", "L", "D")


def run_layout(problem: PlacementProblem,
               seed: int = 42) -> Tuple[Dict[str, Tuple[float, float, float]], float]:
    """Grid-lay footprints inside the region rect. Pair / chain hints are
    honored after the grid pass via a tight snap.

    Returns ({ref → (cx, cy, rot)}, score). Score is always 0.0 — kept in
    the signature for backwards compatibility with orchestrator.
    """
    refs = list(problem.refs)
    if not refs:
        return {}, 0.0

    rx, ry, rw, rh = problem.region_rect

    # ----- pick cell size from largest footprint in region -----
    max_w = max(problem.fp_sizes[r][0] for r in refs)
    max_h = max(problem.fp_sizes[r][1] for r in refs)
    cell_w = max(max_w * 1.2, 4.0)
    cell_h = max(max_h * 1.2, 4.0)

    cols = max(1, int(rw // cell_w))
    rows = max(1, int(rh // cell_h))
    # Re-pack if grid is too small for the count.
    if cols * rows < len(refs):
        cols = max(1, int(math.ceil(math.sqrt(len(refs) * rw / max(rh, 1.0)))))
        rows = max(1, int(math.ceil(len(refs) / cols)))
        cell_w = rw / cols
        cell_h = rh / rows

    # ----- group order: chains first, then declared pairs, then singletons -----
    used: Set[str] = set()
    groups: List[List[str]] = []

    for chain in problem.chains:
        members = [r for r in chain if r in refs and r not in used]
        if len(members) >= 2:
            groups.append(members)
            used.update(members)

    for pair in problem.decoupling_pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        a, b = pair[0], pair[1]
        if a not in refs or b not in refs or a in used or b in used:
            continue
        # Put non-passive (IC) first, passive (cap) second, so snap math
        # places the cap east of the IC.
        if _is_passive(a) and not _is_passive(b):
            ic, cap = b, a
        else:
            ic, cap = a, b
        groups.append([ic, cap])
        used.update([ic, cap])

    for r in refs:
        if r not in used:
            groups.append([r])
            used.add(r)

    # ----- snake-fill grid, advancing one cell per ref -----
    placed: Dict[str, Tuple[float, float, float]] = {}
    cell_idx = 0
    total_cells = cols * rows

    def cell_center(i: int) -> Tuple[float, float]:
        r_idx, c_idx = divmod(i, cols)
        return rx + (c_idx + 0.5) * cell_w, ry + (r_idx + 0.5) * cell_h

    for group in groups:
        for ref in group:
            while cell_idx < total_cells:
                cx, cy = cell_center(cell_idx)
                if _cell_hits_keepout(cx, cy, cell_w, cell_h, problem.keepouts):
                    cell_idx += 1
                    continue
                break
            if cell_idx >= total_cells:
                # Out of grid cells — append at region top-left corner; the
                # in-board / pad-conflict sweeps in pipeline.py will resolve.
                cx, cy = rx + cell_w / 2, ry + cell_h / 2
            placed[ref] = (cx, cy, 0.0)
            cell_idx += 1

    # ----- tight snap: each declared pair → cap immediately east of IC -----
    for pair in problem.decoupling_pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        a, b = pair[0], pair[1]
        if a not in placed or b not in placed:
            continue
        if _is_passive(a) and not _is_passive(b):
            ic, cap = b, a
        else:
            ic, cap = a, b
        ic_x, ic_y, _ = placed[ic]
        ic_w = problem.fp_sizes[ic][0]
        cap_w = problem.fp_sizes[cap][0]
        cap_x = ic_x + ic_w / 2 + cap_w / 2 + ADJACENCY_GAP_MM
        cap_y = ic_y
        # Clamp inside region rect so we don't push the cap off-board.
        cap_x = max(rx + cap_w / 2, min(cap_x, rx + rw - cap_w / 2))
        placed[cap] = (cap_x, cap_y, 0.0)

    # ----- tight snap: chain members lined up along longer axis from member 0 -----
    horizontal = rw >= rh
    for chain in problem.chains:
        members = [r for r in chain if r in placed]
        if len(members) < 2:
            continue
        anchor_x, anchor_y, _ = placed[members[0]]
        if horizontal:
            step = max(problem.fp_sizes[m][0] for m in members) + ADJACENCY_GAP_MM
            for i, m in enumerate(members):
                x = anchor_x + i * step
                x = max(rx + cell_w / 2, min(x, rx + rw - cell_w / 2))
                placed[m] = (x, anchor_y, 0.0)
        else:
            step = max(problem.fp_sizes[m][1] for m in members) + ADJACENCY_GAP_MM
            for i, m in enumerate(members):
                y = anchor_y + i * step
                y = max(ry + cell_h / 2, min(y, ry + rh - cell_h / 2))
                placed[m] = (anchor_x, y, 0.0)

    return placed, 0.0


# Back-compat shim — orchestrator currently imports `run_anneal` /
# `DEFAULT_WEIGHTS` / `PlacementProblem` from this module. New code
# should prefer `run_layout` directly.
run_anneal = run_layout
DEFAULT_WEIGHTS: Dict[str, float] = {}
