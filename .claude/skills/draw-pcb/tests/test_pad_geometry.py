"""Rotated pad geometry — board-space extent, and the clearance gate on top.

`size` in a .kicad_pcb is always the unrotated library dimension, so a pad on a
90°-rotated footprint measures its library height across the board. Reading
`size` as if it were board-space silently mis-measures every rotated part: the
clearance gate then invents violations where the real gap is fine, and can miss
real ones on the other axis.

The pad angle in the board file is ALREADY absolute — pcbnew folds the
footprint rotation into every pad orientation when it saves — so these fixtures
carry the footprint angle on both the footprint and its pads, exactly as a
saved board does. Adding the two would rotate a 90° part by 180°.

Fixtures are constructed, not lifted from a layout: replaying a real board
proves only that its numbers were transcribed.
"""
import math
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "tools"))
sys.path.insert(0, str(_SCRIPTS.parents[1] / "check-pcb" / "scripts"))
from analyze_pcb import rotated_pad_extent  # noqa: E402
from check_placement import check_placement  # noqa: E402
from get_geometry import get_geometry  # noqa: E402

BOARD_W, BOARD_H = 40.0, 30.0

# A rect pad taller than it is wide — the shape that exposes an axis swap.
PAD_W, PAD_H = 2.7, 3.3


def _smd_pad(num: int, px: float, py: float, angle: float,
             w: float = PAD_W, h: float = PAD_H, shape: str = "rect",
             net: str = "") -> str:
    at = f"(at {px} {py}{'' if angle == 0 else ' ' + str(angle)})"
    net_s = f' (net "{net}")' if net else ""
    return (f'    (pad "{num}" smd {shape} {at} (size {w} {h}) '
            f'(layers F.Cu F.Paste F.Mask){net_s})')


def _fp(ref: str, x: float, y: float, angle: float, pads: list[str]) -> str:
    at = f"(at {x} {y}{'' if angle == 0 else ' ' + str(angle)})"
    return "\n".join(
        [f'  (footprint "TestLib:Sample" (layer "F.Cu") {at}',
         f'    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))',
         f'    (property "Value" "{ref}v" (at 0 1.5 0) (layer "F.Fab"))']
        + pads + ["  )"])


def _board(tmp_path: Path, parts: list[str], name="sample_project") -> str:
    edges = [(0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
             (BOARD_W, BOARD_H, 0, BOARD_H), (0, BOARD_H, 0, 0)]
    text = ['(kicad_pcb (version 20240108) (generator "test")']
    for (x1, y1, x2, y2) in edges:
        text.append(f'  (gr_line (start {x1} {y1}) (end {x2} {y2}) '
                    f'(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))')
    text += parts
    text.append(")")
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text("\n".join(text), encoding="utf-8")
    return str(p)


# ── extent formula ────────────────────────────────────────────────────────

@pytest.mark.parametrize("shape,w,h,angle,want", [
    ("rect", 2.7, 3.3, 0, (2.7, 3.3)),
    ("rect", 2.7, 3.3, 90, (3.3, 2.7)),
    ("rect", 2.7, 3.3, 270, (3.3, 2.7)),
    ("rect", 2.7, 3.3, 180, (2.7, 3.3)),         # 180 must NOT swap
    ("oval", 0.574, 2.038, 90, (2.038, 0.574)),  # stadium, long axis rotates
    ("circle", 1.0, 1.0, 45, (1.0, 1.0)),        # rotation-invariant
])
def test_extent(shape, w, h, angle, want):
    got = rotated_pad_extent(shape, w, h, angle)
    assert got == pytest.approx(want, abs=1e-3)


def test_rect_at_45_grows_to_its_diagonal_envelope():
    """A rect at 45° really does need a bigger axis-aligned box."""
    bw, bh = rotated_pad_extent("rect", 2.0, 1.0, 45)
    expect = (2.0 + 1.0) * math.cos(math.radians(45))
    assert bw == pytest.approx(expect, abs=1e-3)
    assert bh == pytest.approx(expect, abs=1e-3)


def test_hole_wider_than_its_annular_ring_still_counts(tmp_path):
    """Some library pads carry an oval drill wider than the copper. The hole is
    a physical obstruction, so the extent is copper union hole."""
    pad = ('    (pad "1" thru_hole oval (at 0 0) (size 2.0 4.6) '
           '(drill oval 3.7 1.2) (layers *.Cu *.Mask))')
    pcb = _board(tmp_path, [_fp("J1", 20, 15, 0, [pad])])
    p = get_geometry(pcb)["footprints"][0]["pads"][0]
    assert (p["w"], p["h"]) == pytest.approx((3.7, 4.6), abs=1e-3)


# ── the gate that reads those numbers ─────────────────────────────────────

def _stacked(tmp_path, gap: float):
    """Two rot-90 parts stacked on Y with a known board-space pad gap.

    Rotated, each pad is PAD_H wide by PAD_W high, so the vertical gap is set
    by PAD_W. Reading the unrotated size instead uses PAD_H on this axis and
    swallows 0.6 mm of clearance that is really there.
    """
    dy = PAD_W + gap
    return _board(tmp_path, [
        _fp("U1", 20, 15, 90, [_smd_pad(1, 0, 0, 90, net="NET_A")]),
        _fp("U2", 20, 15 + dy, 90, [_smd_pad(1, 0, 0, 90, net="NET_B")]),
    ])


def test_rotated_pads_with_real_clearance_pass(tmp_path):
    pcb = _stacked(tmp_path, gap=0.68)
    r = check_placement(pcb, min_clearance=0.2)
    assert r["metrics"]["pad_clearance_violations"] == 0, \
        "0.68mm of real clearance was read as a violation — axis swapped"


def test_rotated_pads_too_close_are_still_caught(tmp_path):
    pcb = _stacked(tmp_path, gap=0.1)
    r = check_placement(pcb, min_clearance=0.2)
    assert r["metrics"]["pad_clearance_violations"] == 1, \
        "a real 0.1mm gap slipped past the gate"


def test_gate_measures_the_axis_the_rotation_moved_copper_onto(tmp_path):
    """Same parts side by side on X: rotation puts the LONG side there, so a
    gap that would pass unrotated must now fail."""
    dx = PAD_H + 0.1
    pcb = _board(tmp_path, [
        _fp("U1", 15, 15, 90, [_smd_pad(1, 0, 0, 90, net="NET_A")]),
        _fp("U2", 15 + dx, 15, 90, [_smd_pad(1, 0, 0, 90, net="NET_B")]),
    ])
    assert check_placement(pcb, min_clearance=0.2)["metrics"][
        "pad_clearance_violations"] == 1
