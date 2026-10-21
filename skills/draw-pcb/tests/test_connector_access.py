"""check_connector_access — synthetic board, every verdict exercised.

The fixture is built from scratch rather than taken from any real layout: the
mating-side rule was derived by looking at real footprints, so a test that
replays those same footprints proves only that the rule was transcribed
correctly. A constructed board with known-correct and known-broken placements
is what shows the rule generalises.

Board is 60 x 40 mm, origin top-left, Y down (KiCad convention).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "tools"))
from check_connector_access import check_connector_access  # noqa: E402

BOARD_W, BOARD_H = 60.0, 40.0

# Body local x -5..+5, y -4..+6 — protrudes 6mm past the pads on +Y and only
# 4mm on -Y, i.e. a mating cavity on the local +Y side.
_TERMINAL_BODY = [(-5, -4, 5, -4), (5, -4, 5, 6), (5, 6, -5, 6), (-5, 6, -5, -4)]


def _fp(ref: str, lib: str, x: float, y: float, angle: float = 0.0,
        body=None, pads=((-2.5, 0), (2.5, 0)), pad_size=2.0,
        layer: str = "F.Cu", circle=None, pad_type: str = "thru_hole") -> str:
    at = f"(at {x} {y}{'' if angle == 0 else ' ' + str(angle)})"
    out = [f'  (footprint "{lib}" (layer "{layer}") {at}',
           f'    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))',
           f'    (property "Value" "{ref}v" (at 0 1.5 0) (layer "F.Fab"))']
    for i, (px, py) in enumerate(pads, start=1):
        if pad_type == "smd":
            out.append(f'    (pad "{i}" smd rect (at {px} {py}) '
                       f'(size {pad_size} {pad_size}) '
                       f'(layers {layer} {layer[0]}.Paste {layer[0]}.Mask))')
        else:
            out.append(f'    (pad "{i}" thru_hole circle (at {px} {py}) '
                       f'(size {pad_size} {pad_size}) (drill 1) '
                       f'(layers *.Cu *.Mask))')
    for (x1, y1, x2, y2) in (body or []):
        out.append(f'    (fp_line (start {x1} {y1}) (end {x2} {y2}) '
                   f'(stroke (width 0.12) (type solid)) (layer "F.SilkS"))')
    if circle:
        cx, cy, r = circle
        out.append(f'    (fp_circle (center {cx} {cy}) (end {cx + r} {cy}) '
                   f'(stroke (width 0.12) (type solid)) (layer "F.SilkS"))')
    out.append("  )")
    return "\n".join(out)


def _board(tmp_path: Path, parts, edges=None, name="sample_project"):
    edges = edges or [(0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
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


@pytest.fixture()
def board(tmp_path: Path) -> str:
    edges = [(0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
             (BOARD_W, BOARD_H, 0, BOARD_H), (0, BOARD_H, 0, 0)]
    parts = [
        # rot 270 maps local +Y to board -X (KiCad rotates CCW on screen).
        # body x spans x0-6 .. x0+4, so the mating face lands at x0-6.
        _fp("J1", "TestLib:TerminalBlock_2P_5.08mm", 6.5, 10, 270,
            body=_TERMINAL_BODY),                     # face at x=0.5  → ok
        # rot 90 maps local +Y to board +X — opening aimed at the interior.
        _fp("J2", "TestLib:TerminalBlock_2P_5.08mm", 4.5, 25, 90,
            body=_TERMINAL_BODY),                     # → faces_inward
        # correctly oriented, but parked mid-board.
        _fp("J3", "TestLib:TerminalBlock_2P_5.08mm", 30.0, 10, 270,
            body=_TERMINAL_BODY),                     # face at x=24  → not_at_edge
        # at the right edge, facing out, with a part in the way.
        _fp("J4", "TestLib:TerminalBlock_2P_5.08mm", 52.5, 32, 90,
            body=_TERMINAL_BODY),                     # face at x=58.5
        _fp("R2", "TestLib:R_0805_2012Metric", 59.2, 32,
            body=[(-0.5, -0.5, 0.5, -0.5), (0.5, -0.5, 0.5, 0.5),
                  (0.5, 0.5, -0.5, 0.5), (-0.5, 0.5, -0.5, -0.5)],
            pads=((-0.5, 0), (0.5, 0)), pad_size=0.6),   # sits in J4's corridor
        # a connector footprint that ships no body outline at all.
        _fp("J5", "TestLib:Conn_NoOutline_2P", 30.0, 35),   # → undetermined
        # geometry says -X, but it is really a vertical header.
        _fp("J6", "TestLib:PinHeader_2x05_P2.54mm", 30.0, 20, 270,
            body=_TERMINAL_BODY),                     # declared +Z → ok
        # ordinary parts that must not be dragged into the check.
        _fp("R1", "TestLib:R_0805_2012Metric", 20.0, 20,
            pads=((-0.5, 0), (0.5, 0)), pad_size=0.6),
        _fp("SW1", "TestLib:SW_Push_Tact_SMD_3x4mm", 45.0, 5,
            body=_TERMINAL_BODY),                     # → skipped (switch)
    ]
    text = ['(kicad_pcb (version 20240108) (generator "test")']
    for (x1, y1, x2, y2) in edges:
        text.append(f'  (gr_line (start {x1} {y1}) (end {x2} {y2}) '
                    f'(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))')
    text += parts
    text.append(")")
    p = tmp_path / "sample_project.kicad_pcb"
    p.write_text("\n".join(text), encoding="utf-8")
    return str(p)


def _by_ref(result: dict) -> dict:
    return {e["ref"]: e for e in result["connectors"]}


def test_verdicts(board):
    res = check_connector_access(board, declare={"J6": {"mating_dir": "+Z"}})
    got = _by_ref(res)

    assert got["J1"]["status"] == "ok"
    assert got["J1"]["mating_dir"] == "-X"
    assert got["J1"]["gap_mm"] == pytest.approx(0.5, abs=0.01)

    assert got["J2"]["status"] == "faces_inward"
    assert got["J2"]["mating_dir"] == "+X"
    assert "180" in got["J2"]["fix_hint"]

    assert got["J3"]["status"] == "not_at_edge"
    assert got["J3"]["gap_mm"] == pytest.approx(24.0, abs=0.01)

    assert got["J4"]["status"] == "blocked"
    assert got["J4"]["blockers"] == ["R2"]

    assert got["J5"]["status"] == "undetermined"
    assert "no CrtYd / Fab / SilkS body outline" in got["J5"]["detail"]

    assert got["J6"]["status"] == "ok"
    assert got["J6"]["source"] == "declared"

    assert got["SW1"]["kind"] == "switch"
    assert got["SW1"]["status"] == "skipped"

    assert res["hard_fail"] is True


def test_plain_parts_are_not_connectors(board):
    got = _by_ref(check_connector_access(board))
    assert "R1" not in got, "a chip resistor must not enter the connector gate"


def test_declaration_overrides_geometry(board):
    """J5 has no derivable mating side; declaring it clears the gate.

    J5 sits at (30, 35) unrotated, so a local -Y declaration points at the
    y=0 edge 35mm away → still caught, just no longer 'undetermined'.
    """
    res = check_connector_access(board, declare={
        "J5": {"mating_dir": "-Y", "note": "datasheet fig 3"},
        "J6": {"mating_dir": "+Z"},
    })
    j5 = _by_ref(res)["J5"]
    assert j5["source"] == "declared"
    assert j5["mating_side_local"] == "-Y"
    assert j5["status"] != "undetermined"
    assert j5["note"] == "datasheet fig 3"


def test_declaration_is_footprint_local_and_still_rotation_checked(board):
    """A declaration states a footprint fact, so rotation is STILL checked.

    Regression for the escape hatch disabling the gate: J2's opening really is
    on the footprint's local +Y, and J2 is rotated so that lands pointing into
    the board. Declaring the true local side must NOT turn that into a pass —
    if it did, every part with an unreadable footprint could be waved through.
    """
    res = check_connector_access(board, declare={
        "J2": {"mating_dir": "+Y", "note": "datasheet — cage opens on +Y"},
        "J6": {"mating_dir": "+Z"},
    })
    j2 = _by_ref(res)["J2"]
    assert j2["source"] == "declared"
    assert j2["mating_side_local"] == "+Y"
    assert j2["mating_dir"] == "+X", "local side must be rotated into board frame"
    assert j2["status"] == "faces_inward"
    assert res["hard_fail"] is True


def test_silenced_parts_are_named_and_counted(board):
    """`none`, --exclude and switch-skips must leave a trace, not vanish."""
    res = check_connector_access(
        board, declare={"J2": {"mating_dir": "none", "note": "not fitted"},
                        "J6": {"mating_dir": "+Z", "note": "vertical header"}},
        exclude={"J3"})
    how = {s["ref"]: s["how"] for s in res["silenced"]}
    assert how["J2"] == "declared_none"
    assert how["J3"] == "excluded"
    assert how["J6"] == "declared_vertical"
    assert how["SW1"] == "switch_skipped"
    assert res["silenced_count"] == len(res["silenced"]) >= 4
    assert all(s.get("note") for s in res["silenced"]), "each needs a reason"


def test_broken_outline_is_not_a_datasheet_problem(tmp_path):
    """No Edge.Cuts hit → tell the operator to fix the outline, not to declare."""
    text = ['(kicad_pcb (version 20240108) (generator "test")',
            # deliberately open outline: one wall missing
            '  (gr_line (start 0 0) (end 60 0) (stroke (width 0.05) '
            '(type solid)) (layer "Edge.Cuts"))',
            _fp("J1", "TestLib:TerminalBlock_2P_5.08mm", 6.5, 10, 90,
                body=_TERMINAL_BODY),
            ")"]
    p = tmp_path / "sample_project.kicad_pcb"
    p.write_text("\n".join(text), encoding="utf-8")

    j1 = _by_ref(check_connector_access(str(p)))["J1"]
    assert j1["status"] == "undetermined"
    assert "Edge.Cuts" in j1["detail"]
    assert "declare will NOT clear" in j1["fix_hint"].replace("--", "")


def test_keystone_parts_are_not_interface_parts():
    """`key` as a bare substring captures Keystone test points / holders."""
    from connector_id import classify  # noqa: PLC0415

    assert classify("TP1", "TestPoint:Keystone_5015_Micro-Miniature",
                    body_overhang_mm=2.0) is None
    assert classify("BT1", "Battery:BatteryHolder_Keystone_1042_1x18650",
                    body_overhang_mm=2.0) == "connector"
    assert classify("SW1", "Button_Switch_SMD:SW_Push_1P1T",
                    body_overhang_mm=2.0) == "switch"


def test_gate_clears_when_everything_is_right(tmp_path):
    """A board whose only interface part is correctly placed must pass."""
    text = ['(kicad_pcb (version 20240108) (generator "test")']
    for (x1, y1, x2, y2) in [(0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
                             (BOARD_W, BOARD_H, 0, BOARD_H), (0, BOARD_H, 0, 0)]:
        text.append(f'  (gr_line (start {x1} {y1}) (end {x2} {y2}) '
                    f'(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))')
    text.append(_fp("J1", "TestLib:TerminalBlock_2P_5.08mm", 6.5, 10, 270,
                    body=_TERMINAL_BODY))
    text.append(")")
    p = tmp_path / "sample_project.kicad_pcb"
    p.write_text("\n".join(text), encoding="utf-8")

    res = check_connector_access(str(p))
    assert res["hard_fail"] is False
    assert res["violation_count"] == 0


def test_edge_check_measures_the_body_not_the_pads(board):
    """A correctly edge-mounted connector must not read as 'mid-board'.

    These footprints ship no CrtYd, so get_geometry's courtyard falls back to
    the pad bbox — which for J1 sits 5.5mm inside the left edge while the shell
    is 0.5mm from it. Measuring pads would flag every real edge connector and
    turn the warning into noise.
    """
    from check_placement import check_placement  # noqa: PLC0415

    res = check_placement(board)
    flagged = {r for w in res["warnings"]
               if w["type"] == "connector_not_on_edge" for r in w["refs"]}
    assert "J1" not in flagged
    assert "J3" in flagged, "the genuinely mid-board connector must still flag"


def test_rotating_the_broken_part_fixes_it(tmp_path):
    """The fix the tool prescribes (rotate 180°) must actually clear it."""
    verdicts = {}
    for angle in (90, 270):
        text = ['(kicad_pcb (version 20240108) (generator "test")']
        for (x1, y1, x2, y2) in [(0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
                                 (BOARD_W, BOARD_H, 0, BOARD_H), (0, BOARD_H, 0, 0)]:
            text.append(f'  (gr_line (start {x1} {y1}) (end {x2} {y2}) '
                        f'(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))')
        # centre chosen so the body straddles the same footprint either way
        text.append(_fp("J1", "TestLib:TerminalBlock_2P_5.08mm", 5.5, 10, angle,
                        body=_TERMINAL_BODY))
        text.append(")")
        p = tmp_path / f"sample_{angle}.kicad_pcb"
        p.write_text("\n".join(text), encoding="utf-8")
        verdicts[angle] = _by_ref(check_connector_access(str(p)))["J1"]["status"]

    assert verdicts[90] == "faces_inward"
    assert verdicts[270] == "ok"


# --- regressions for defects found by adversarial audit ------------------

def test_side_entry_with_central_pins_still_resolves(tmp_path):
    """Recall guard. A THT screw terminal has its pins in the MIDDLE of the
    block, so its body encloses the pads on all four sides — the same
    signature a top-entry housing has. An enclosure-based veto therefore eats
    the whole horizontal terminal-block family (measured: -19pp of side-entry
    recall across KiCad's library, and every true positive on a real board).
    Vertical parts are caught by name, so enclosure must stay informational.
    """
    pcb = _board(tmp_path, [_fp("J1", "TestLib:TerminalBlock_2P_5.08mm",
                                6.5, 10, 270, body=_TERMINAL_BODY)])
    j1 = _by_ref(check_connector_access(pcb))["J1"]
    assert j1["status"] != "undetermined", "enclosed pads must not veto"
    assert j1["mating_side_local"] == "+Y"


def test_vertical_footprint_name_forces_undetermined(tmp_path):
    """A top-entry housing has no in-plane mating side; its widest wall must
    not be reported as one."""
    pcb = _board(tmp_path, [_fp("J1", "Connector_JST:JST_XH_B2B-XH-A_1x02_"
                                "P2.50mm_Vertical", 6.5, 10, 270,
                                body=_TERMINAL_BODY)])
    j1 = _by_ref(check_connector_access(pcb))["J1"]
    assert j1["status"] == "undetermined"
    assert "vertical" in j1["detail"].lower()


def test_circular_body_has_no_mating_side(tmp_path):
    """A circle is stored as centre+rim point; reading those as bbox corners
    invents asymmetry in a perfectly round shell (DIN / XLR / barrel jack)."""
    pcb = _board(tmp_path, [_fp("J1", "TestLib:Jack_Barrel_Conn", 30, 20,
                                circle=(0, 0, 6))])
    j1 = _by_ref(check_connector_access(pcb))["J1"]
    assert j1["status"] == "undetermined", "a circle has no mating side"


def test_bottom_layer_part_does_not_block(tmp_path):
    """An SMD part on the far copper layer obstructs nothing — check_placement
    already skips cross-layer pairs and the two gates must agree. (A THT part
    pierces the board, so it still blocks from either side.)"""
    parts = [_fp("J1", "TestLib:TerminalBlock_2P_5.08mm", 52.5, 32, 90,
                 body=_TERMINAL_BODY),
             _fp("C9", "TestLib:C_0805_2012Metric", 59.2, 32, layer="B.Cu",
                 pad_type="smd",
                 body=[(-0.5, -0.5, 0.5, -0.5), (0.5, -0.5, 0.5, 0.5),
                       (0.5, 0.5, -0.5, 0.5), (-0.5, 0.5, -0.5, -0.5)],
                 pads=((-0.5, 0), (0.5, 0)), pad_size=0.6)]
    j1 = _by_ref(check_connector_access(_board(tmp_path, parts)))["J1"]
    assert j1["status"] != "blocked"
    assert "C9" not in (j1.get("blockers") or [])


def test_internal_cutout_is_not_a_board_edge(tmp_path):
    """Opening into a milled slot is not opening off the board."""
    cutout = [(20, 5, 26, 5), (26, 5, 26, 35), (26, 35, 20, 35), (20, 35, 20, 5)]
    edges = [(0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
             (BOARD_W, BOARD_H, 0, BOARD_H), (0, BOARD_H, 0, 0)] + cutout
    # mating face at x=19.5 pointing +X — 0.5mm from the cutout wall, 40mm
    # from the real perimeter.
    pcb = _board(tmp_path, [_fp("J1", "TestLib:TerminalBlock_2P_5.08mm",
                                13.5, 20, 90, body=_TERMINAL_BODY)],
                 edges=edges)
    j1 = _by_ref(check_connector_access(pcb))["J1"]
    assert j1["status"] != "ok", "must not read a cutout wall as the edge"


def test_invalid_declaration_does_not_kill_the_run(tmp_path):
    """One typo used to raise KeyError and void the verdict for every part."""
    pcb = _board(tmp_path, [_fp("J1", "TestLib:TerminalBlock_2P_5.08mm",
                                6.5, 10, 270, body=_TERMINAL_BODY)])
    res = check_connector_access(pcb, declare={"J1": {"mating_dir": "up"}})
    assert res["ok"] is True
    assert any(v["type"] == "bad_declaration" for v in res["violations"])
    assert _by_ref(res)["J1"]["source"] == "geometry", "falls back to geometry"
