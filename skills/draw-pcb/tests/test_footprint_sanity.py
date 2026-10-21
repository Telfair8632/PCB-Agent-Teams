"""check_footprint_sanity — padstack + courtyard library-data checks.

Fixtures are constructed, not lifted from a layout: each one isolates exactly
one defect class a converted library can carry (drill wider than the copper,
zero-annular PTH, missing / degenerate courtyard) plus the healthy shapes that
must NOT be flagged (NPTH where size == drill is the standard drawing, normal
ringed PTH, SMD pads with no drill at all).
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "tools"))
from check_footprint_sanity import check_footprint  # noqa: E402


def _fp(*pads: str, courtyard: str = "") -> str:
    return f'(footprint "test" (layer "F.Cu")\n' + "\n".join(pads) + courtyard + "\n)"


CRTYD_RECT = ('\n(fp_rect (start -2 -2) (end 2 2) '
              '(stroke (width 0.05) (type default)) (layer "F.CrtYd"))')
CRTYD_LINE = ('\n(fp_line (start -2 0) (end 2 0) '
              '(stroke (width 0.05) (type default)) (layer "F.CrtYd"))')

PTH_OK = ('(pad "1" thru_hole circle (at 0 0) (size 2.0 2.0) '
          '(drill 1.0) (layers "*.Cu"))')
PTH_ZERO_RING = ('(pad "1" thru_hole circle (at 0 0) (size 2.000001 2.000001) '
                 '(drill 2.000001) (layers "*.Cu"))')
NPTH_SAME = ('(pad "" np_thru_hole circle (at 0 0) (size 2.0 2.0) '
             '(drill 2.0) (layers "*.Cu"))')
OVAL_DRILL_WIDER = ('(pad "3" thru_hole oval (at 0 0) (size 1.2 1.8) '
                    '(drill oval 1.7 0.7) (layers "*.Cu"))')
SMD = '(pad "2" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu"))'


def test_healthy_pth_and_smd_are_clean():
    r = check_footprint(_fp(PTH_OK, SMD, courtyard=CRTYD_RECT))
    assert r["drill_exceeds_pad"] == []
    assert r["zero_annular"] == []
    assert r["courtyard"] == "ok"


def test_pth_with_size_equal_drill_is_zero_annular():
    r = check_footprint(_fp(PTH_ZERO_RING, courtyard=CRTYD_RECT))
    assert [p["pad"] for p in r["zero_annular"]] == ["1"]
    assert r["drill_exceeds_pad"] == []


def test_npth_size_equal_drill_is_the_standard_drawing_not_a_defect():
    r = check_footprint(_fp(NPTH_SAME, courtyard=CRTYD_RECT))
    assert r["zero_annular"] == []
    assert r["drill_exceeds_pad"] == []


def test_oval_drill_wider_than_pad_is_negative_ring():
    r = check_footprint(_fp(OVAL_DRILL_WIDER, courtyard=CRTYD_RECT))
    assert [p["pad"] for p in r["drill_exceeds_pad"]] == ["3"]
    # ring on x: (1.2 - 1.7) / 2 = -0.25
    assert r["drill_exceeds_pad"][0]["ring_mm"] == -0.25


def test_missing_courtyard_is_reported():
    assert check_footprint(_fp(PTH_OK))["courtyard"] == "missing"


def test_line_courtyard_is_degenerate():
    assert check_footprint(_fp(PTH_OK, courtyard=CRTYD_LINE))["courtyard"] == "degenerate"
