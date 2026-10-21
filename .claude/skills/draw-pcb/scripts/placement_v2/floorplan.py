"""Phase B — region floorplan: 3 rectangles + Edge.Cuts + isolation slots.

Closed-form geometry (no SAT solver). Inputs: per-region courtyard area
sums + isolation gap requirements from project CLAUDE.md. Outputs: each
region's bounding rect, board outline polyline, and Edge.Cuts slot
segments.

Critical design point: the isolation slot is the *gap between regions*,
chosen at floorplan time. Placement runs INSIDE its region rect (Phase
C's restricted move generator never lets a footprint cross the gap), so
the slot's geometric guarantee never depends on placement converging
correctly. This structurally eliminates the "slot 漏画" failure mode of
v1.

Layout strategy:
    HV-left, ISO-middle, LV-right (when ISO present)
    HV-left, LV-right            (when no ISO)
    LV-only                       (when no HV/ISO)

If a project wants top-bottom instead of left-right, set
floorplan.orientation: "vertical" in CLAUDE.md.
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
from typing import Dict, List, Mapping, Optional, Tuple


@dataclass
class Rect:
    """Axis-aligned rect, mm units, origin top-left."""
    x: float       # left edge
    y: float       # top edge
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def to_dict(self) -> Dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass
class Slot:
    """Isolation milling slot, drawn on Edge.Cuts."""
    x_mm: float          # slot center x
    y_start: float       # slot top y
    y_end: float         # slot bottom y
    width_mm: float      # slot width
    reason: str = ""

    def to_dict(self) -> Dict:
        return {"x_mm": self.x_mm, "y_start": self.y_start,
                "y_end": self.y_end, "width_mm": self.width_mm,
                "reason": self.reason}


@dataclass
class Floorplan:
    board: Rect
    regions: Dict[str, Rect]
    slots: List[Slot] = field(default_factory=list)
    edge_cuts: List[Tuple[float, float, float, float]] = field(default_factory=list)
    """edge_cuts: list of (x1,y1,x2,y2) line segments forming board outline."""

    def to_dict(self) -> Dict:
        return {
            "board": self.board.to_dict(),
            "regions": {r: rect.to_dict() for r, rect in self.regions.items()},
            "slots": [s.to_dict() for s in self.slots],
            "edge_cuts": [list(seg) for seg in self.edge_cuts],
        }


# Layout heuristics --------------------------------------------------------

# Aspect ratio target for each region's rect: w/h. 1.4 ≈ "slightly wide,"
# typical for SMD boards. Caller can override via floorplan.aspect_ratio.
DEFAULT_ASPECT_RATIO = 1.4

# Density: how much board area is footprints vs whitespace. 0.55 leaves
# ~45% for routing channels — generous. Tighter (0.7) gives smaller
# board but harder routing.
DEFAULT_PACK_DENSITY = 0.55

# Margin from board edge to nearest footprint courtyard.
DEFAULT_BOARD_MARGIN = 2.5

# Default slot width when isolation is requested but width unspecified.
# 4mm covers IPC creepage at ~1500V working in pollution degree 2.
DEFAULT_SLOT_WIDTH = 4.0


def _region_min_size(area_mm2: float, aspect: float, density: float) -> Tuple[float, float]:
    """Compute region rect width × height that fits area at target aspect."""
    if area_mm2 <= 0:
        return 0.0, 0.0
    needed = area_mm2 / max(0.05, density)
    h = math.sqrt(needed / aspect)
    w = h * aspect
    return w, h


def plan_floorplan(
    region_areas: Mapping[str, float],
    isolation_gaps: Optional[List[Mapping]] = None,
    orientation: str = "horizontal",
    aspect_ratio: float = DEFAULT_ASPECT_RATIO,
    pack_density: float = DEFAULT_PACK_DENSITY,
    board_margin: float = DEFAULT_BOARD_MARGIN,
    region_order: Optional[List[str]] = None,
    min_w: Optional[float] = None,
    min_h: Optional[float] = None,
) -> Floorplan:
    """Compute board outline + region rects + slots.

    Args:
        region_areas: {region_label → total courtyard area in mm²} from
                      Phase A partition + footprint dimensions.
        isolation_gaps: list of {between: [region_a, region_b], width_mm,
                                 reason}. Each entry creates a slot drawn
                                 on Edge.Cuts at the gap between those
                                 two regions.
        orientation: 'horizontal' (regions left-to-right) or 'vertical'
                     (top-to-bottom).
        aspect_ratio: target w/h for each region rect.
        pack_density: footprint area / region area (0..1).
        board_margin: mm padding from rect edges to board outline.
        region_order: explicit ordering, e.g. ['HV','ISO','LV']. If None
                      and 'ISO' present, defaults to HV→ISO→LV.
        min_w / min_h: lower bounds for board (e.g. for hand-soldering or
                       chassis fit). Skill itself does not impose any.

    Returns:
        Floorplan with board, region rects, slots, and edge_cuts segments.
    """
    isolation_gaps = list(isolation_gaps or [])
    regions_present = [r for r, a in region_areas.items() if a > 0]
    if not regions_present:
        # Empty board fallback — single 20×20 placeholder.
        return Floorplan(
            board=Rect(0, 0, 20, 20),
            regions={},
            edge_cuts=_rect_segments(Rect(0, 0, 20, 20)),
        )

    # Decide region order.
    if region_order:
        order = [r for r in region_order if r in regions_present]
    else:
        # Conventional power-flow ordering when these labels appear.
        canonical = ["HV", "ISO", "LV"]
        ordered = [r for r in canonical if r in regions_present]
        # Append any project-custom labels at the end, deterministic.
        ordered += sorted(r for r in regions_present if r not in canonical)
        order = ordered

    # Size each region rect.
    region_sizes: Dict[str, Tuple[float, float]] = {}
    for r in order:
        w, h = _region_min_size(region_areas[r], aspect_ratio, pack_density)
        # Floor at 8mm to avoid pathologically thin strips.
        region_sizes[r] = (max(w, 8.0), max(h, 8.0))

    # Tallest region's height drives all heights when orientation is
    # horizontal so they share a baseline; same logic flipped for vertical.
    if orientation == "horizontal":
        common_h = max(h for w, h in region_sizes.values())
        region_sizes = {r: (w, common_h) for r, (w, _) in region_sizes.items()}
    else:
        common_w = max(w for w, h in region_sizes.values())
        region_sizes = {r: (common_w, h) for r, (_, h) in region_sizes.items()}

    # Map gap widths to (region_a, region_b) pairs for quick lookup.
    gap_widths: Dict[Tuple[str, str], Tuple[float, str]] = {}
    for g in isolation_gaps:
        a, b = g.get("between", [None, None])
        if not a or not b:
            continue
        key = tuple(sorted([a, b]))
        gap_widths[key] = (
            float(g.get("width_mm", DEFAULT_SLOT_WIDTH)),
            str(g.get("reason", "")),
        )

    # Lay out regions along orientation axis with gaps between adjacent ones.
    regions_out: Dict[str, Rect] = {}
    slots_out: List[Slot] = []
    if orientation == "horizontal":
        cursor_x = board_margin
        baseline_y = board_margin
        for i, r in enumerate(order):
            w, h = region_sizes[r]
            regions_out[r] = Rect(cursor_x, baseline_y, w, h)
            cursor_x += w
            if i < len(order) - 1:
                next_r = order[i + 1]
                gap_w, reason = gap_widths.get(tuple(sorted([r, next_r])),
                                                (0.0, ""))
                if gap_w > 0:
                    slots_out.append(Slot(
                        x_mm=cursor_x + gap_w / 2,
                        y_start=baseline_y - 0.5,
                        y_end=baseline_y + h + 0.5,
                        width_mm=gap_w,
                        reason=reason or f"isolation between {r} and {next_r}",
                    ))
                    cursor_x += gap_w
        board_w = cursor_x + board_margin
        board_h = baseline_y + max(h for _, h in region_sizes.values()) + board_margin
    else:  # vertical
        cursor_y = board_margin
        baseline_x = board_margin
        for i, r in enumerate(order):
            w, h = region_sizes[r]
            regions_out[r] = Rect(baseline_x, cursor_y, w, h)
            cursor_y += h
            if i < len(order) - 1:
                next_r = order[i + 1]
                gap_w, reason = gap_widths.get(tuple(sorted([r, next_r])),
                                                (0.0, ""))
                if gap_w > 0:
                    # Vertical orientation → horizontal slot. Encode as
                    # rotated slot using y_start/y_end as x_start/x_end
                    # by callers; keep schema simple — caller resolves.
                    slots_out.append(Slot(
                        x_mm=cursor_y + gap_w / 2,
                        y_start=baseline_x - 0.5,
                        y_end=baseline_x + w + 0.5,
                        width_mm=gap_w,
                        reason=(reason or f"isolation between {r} and {next_r}")
                                + " (horizontal)",
                    ))
                    cursor_y += gap_w
        board_w = baseline_x + max(w for w, _ in region_sizes.values()) + board_margin
        board_h = cursor_y + board_margin

    # Honor min_w/min_h by expanding the BOARD AND the regions inside.
    # Without this, a small footprint set in a min-sized board leaves
    # huge whitespace at the bottom (regions stay tiny, board outline
    # alone grows). Expand regions proportionally to fill min dimensions.
    if min_w and min_w > board_w:
        slack = min_w - board_w
        # Distribute slack proportionally to current widths so layout
        # remains proportionate.
        total_w = sum(rect.w for rect in regions_out.values())
        if total_w > 0:
            for r, rect in regions_out.items():
                share = rect.w / total_w
                regions_out[r] = Rect(rect.x, rect.y,
                                       rect.w + slack * share, rect.h)
            # Re-thread x positions after expansion.
            cur_x = board_margin
            new_regions = {}
            slot_iter = iter(slots_out)
            new_slots = []
            for i, r in enumerate(order):
                rect = regions_out[r]
                new_regions[r] = Rect(cur_x, rect.y, rect.w, rect.h)
                cur_x += rect.w
                if i < len(order) - 1:
                    next_r = order[i + 1]
                    gap_w, reason = gap_widths.get(tuple(sorted([r, next_r])),
                                                    (0.0, ""))
                    if gap_w > 0:
                        new_slots.append(Slot(
                            x_mm=cur_x + gap_w / 2,
                            y_start=rect.y - 0.5,
                            y_end=rect.y + rect.h + 0.5,
                            width_mm=gap_w,
                            reason=reason or f"isolation between {r} and {next_r}",
                        ))
                        cur_x += gap_w
            regions_out = new_regions
            slots_out = new_slots
            board_w = min_w
        else:
            board_w = min_w
    if min_h and min_h > board_h:
        # Stretch every region's height to fill board height.
        slack_per = (min_h - board_h)
        for r, rect in regions_out.items():
            regions_out[r] = Rect(rect.x, rect.y, rect.w, rect.h + slack_per)
        # Slots span full new region height.
        new_slots = []
        for s in slots_out:
            new_slots.append(Slot(
                x_mm=s.x_mm,
                y_start=board_margin - 0.5,
                y_end=board_margin + (regions_out[order[0]].h) + 0.5,
                width_mm=s.width_mm,
                reason=s.reason,
            ))
        slots_out = new_slots
        board_h = min_h

    board = Rect(0, 0, board_w, board_h)
    return Floorplan(
        board=board,
        regions=regions_out,
        slots=slots_out,
        edge_cuts=_rect_segments(board),
    )


def _rect_segments(r: Rect) -> List[Tuple[float, float, float, float]]:
    """Return 4 line segments (x1,y1,x2,y2) forming the rect outline."""
    return [
        (r.x, r.y, r.right, r.y),               # top
        (r.right, r.y, r.right, r.bottom),       # right
        (r.right, r.bottom, r.x, r.bottom),      # bottom
        (r.x, r.bottom, r.x, r.y),               # left
    ]


def courtyard_area_by_region(
    assignments: Mapping[str, str],
    footprint_sizes: Mapping[str, Tuple[float, float]],
    overhead: float = 1.3,
) -> Dict[str, float]:
    """Sum (w × h) per region with overhead for spacing.

    overhead=1.3 means each footprint reserves 30% extra around its
    courtyard for routing breakout. This compounds with pack_density
    in plan_floorplan, so don't double-count: use overhead 1.0 there.
    """
    out: Dict[str, float] = {}
    for ref, region in assignments.items():
        w, h = footprint_sizes.get(ref, (3.0, 3.0))
        out[region] = out.get(region, 0.0) + w * h * overhead
    return out
