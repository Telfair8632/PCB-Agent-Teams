"""Pure geometry of the silk designator placer — candidates and cost order.

The helper module imports pcbnew at module level; a stub satisfies that here
because everything under test is plain rectangle arithmetic. Fixtures are
constructed, not lifted from a layout: replaying a real board proves only
that its numbers were transcribed.
"""

# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import importlib.util
import sys
import types
from pathlib import Path

_HELPER = (Path(__file__).resolve().parents[1] / "scripts"
           / "_kicad_python_helper.py")
sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
_spec = importlib.util.spec_from_file_location("_silk_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)

BOARD = (0.0, 0.0, 100.0, 100.0)


def cost(r, dist=0.0, w_dist=3.0, board=BOARD, edges=(), pads=(), silks=(),
         refs=(), bodies=(), own=-1):
    return helper.silk_cost(r, dist, w_dist, board, list(edges), list(pads),
                            list(silks), list(refs), list(bodies), own)


# ---------------------------------------------------------------- overlap

def test_overlap_disjoint_and_touching_are_zero():
    a = (0, 0, 1, 1)
    assert helper.silk_rect_overlap(a, (2, 2, 3, 3)) == 0.0
    assert helper.silk_rect_overlap(a, (1, 0, 2, 1)) == 0.0  # shared edge


def test_overlap_partial_and_full():
    a = (0, 0, 2, 2)
    assert helper.silk_rect_overlap(a, (1, 0, 3, 2)) == 2.0
    assert helper.silk_rect_overlap(a, a) == 4.0


# ------------------------------------------------------------- candidates

def test_candidate_ring_count_and_rotated_form():
    # body 10×6, text 2×1, one gap: 8 spots per form. Centre qualifies only
    # for the horizontal form (rotated needs body height > 3×2=6, it is =6).
    cands = helper.silk_candidates((0, 0, 10, 6), 2, 1, [0.5], True)
    assert len(cands) == 8 + 8 + 1
    assert {(w, h) for _, w, h, _, _ in cands} == {(2, 1), (1, 2)}
    # north spots sit clear of the body by exactly the gap: horizontal text
    # (h=1) centres at y=-1, rotated (h=2) at y=-1.5
    assert (0.0, 2, 1, 5.0, -1.0) in cands
    assert (90.0, 1, 2, 5.0, -1.5) in cands


def test_no_rotate_and_no_centre_on_small_body():
    cands = helper.silk_candidates((0, 0, 3, 3), 2, 1, [0.5, 1.0], False)
    assert len(cands) == 16                      # 8 × 2 gaps, one form
    assert all(ang == 0.0 for ang, *_ in cands)


# ------------------------------------------------------------------ cost

def test_off_board_is_fatal():
    inside = cost((1, 1, 2, 2))
    outside = cost((-1, 1, 0, 2))
    assert inside == 0.0
    assert outside >= 1e6


def test_weight_ordering_pad_ref_silk_body():
    r = (0, 0, 1, 1)                              # 1 mm² over each obstacle
    c_pad = cost(r, pads=[r])
    c_ref = cost(r, refs=[r], own=1)
    c_silk = cost(r, silks=[r])
    c_body = cost(r, bodies=[r], own=1)
    assert c_pad > c_ref > c_silk > c_body > 0.0


def test_own_body_costs_triple_and_own_ref_is_free():
    r = (0, 0, 1, 1)
    assert cost(r, bodies=[r], own=0) == 3 * cost(r, bodies=[r], own=1)
    # slot own=0: a designator never scores against its own previous spot
    assert cost(r, refs=[r], own=0) == 0.0


def test_distance_term_is_linear():
    assert cost((1, 1, 2, 2), dist=2.0, w_dist=3.0) == 6.0


# ---------------------------------------------------------- edge keepouts

def test_segment_becomes_one_band_and_slot_is_fatal():
    # A horizontal slot line across mid-board: thin bbox → one inflated band.
    bands = helper.silk_edge_keepouts((10, 50, 90, 50.1), 0.5)
    assert bands == [(9.5, 49.5, 90.5, 50.6)]
    # Text overlapping the band scores as fatal even though it is on-board.
    assert cost((40, 49.8, 42, 50.8), edges=bands) >= 1e6
    assert cost((40, 60, 42, 61), edges=bands) == 0.0


def test_closed_outline_keeps_interior_usable():
    # Whole outline drawn as one gr_rect: bbox = the board. Only the four
    # perimeter bands may block, or every candidate would be fatal.
    bands = helper.silk_edge_keepouts((0, 0, 100, 100), 0.5)
    assert len(bands) == 4
    assert cost((50, 50, 52, 51), edges=bands) == 0.0     # interior fine
    # the fatal weight scales with overlap area (here 0.6 mm²) — what matters
    # is that it dwarfs every non-edge cost class
    assert cost((50, 0.2, 52, 1.2), edges=bands) >= 1e5   # hugging the edge
