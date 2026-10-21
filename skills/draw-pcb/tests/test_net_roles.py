"""net_roles — supply rails must be found by structure, not by a name list.

The defect this replaces: a hardcoded whitelist that listed `vdd1`/`vdd2` but
not bare `vdd`, so on a real board it found GND and missed every actual rail.
The tests below therefore lead with the name-independent case — a rail whose
name matches nothing at all still has to be detected.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "tools"))
from net_roles import BYPASS_CAP_MIN, classify_nets, is_ground  # noqa: E402


def test_unnamed_rail_is_found_by_its_bypass_caps():
    """The whole point: a rail called something the rule never heard of.

    `RAIL_A` matches no name pattern. Two caps bridging it to ground are what
    make it a supply, and that evidence is independent of what anyone called it.
    """
    fn = {"U1": {"RAIL_A", "GND", "SDA"},
          "C1": {"RAIL_A", "GND"},
          "C2": {"RAIL_A", "GND"},
          "R1": {"SDA", "SCL"}}
    r = classify_nets(fn)
    assert "RAIL_A" in r["power"]
    assert r["why"]["RAIL_A"] == [f"bypassed_by_{BYPASS_CAP_MIN}_caps"]
    assert "SDA" not in r["power"]


def test_one_filter_cap_is_not_a_rail():
    """A signal with a single filter cap to ground must not become a supply.

    Measured on a real board: EN, ADC1, ADC2, IO22, IO23 each have exactly one
    cap to ground. Lowering the bar to 1 would sweep all of them in.
    """
    fn = {"U1": {"ADC1", "GND"}, "C1": {"ADC1", "GND"}}
    assert "ADC1" not in classify_nets(fn)["power"]


def test_bare_vdd_and_friends_match_by_name():
    """The exact names the old whitelist missed."""
    fn = {f"U{i}": {n, "GND"} for i, n in enumerate(
        ["VDD", "VEXT", "VBUS", "VBAT", "VDC", "VOUT_XDC", "V_EXT",
         "3V3", "+5V", "-12V", "VCC"])}
    power = set(classify_nets(fn)["power"])
    for n in ["VDD", "VEXT", "VBUS", "VBAT", "VDC", "VOUT_XDC", "V_EXT",
              "3V3", "+5V", "-12V", "VCC"]:
        assert n in power, f"{n} must be recognised as a rail"


def test_reference_and_feedback_nets_are_not_rails():
    """V-names that carry no supply current — widening these would be wrong."""
    fn = {f"U{i}": {n, "GND"} for i, n in enumerate(
        ["VREF", "VSENSE", "VFB", "VADJ", "VMON", "VDIV"])}
    assert classify_nets(fn)["power"] == []


def test_ground_variants():
    for n in ["GND", "AGND", "DGND", "PGND", "GND_1", "HV_GND", "VSS", "0V"]:
        assert is_ground(n), n
    for n in ["GNDX", "GROUNDING_LUG", "SIGNAL"]:
        assert not is_ground(n), n


def test_ground_is_not_double_counted_as_power():
    fn = {"U1": {"GND", "VDD"}, "C1": {"VDD", "GND"}, "C2": {"VDD", "GND"}}
    r = classify_nets(fn)
    assert r["ground"] == ["GND"]
    assert "GND" not in r["power"]
    assert set(r["topology_noise"]) == {"GND", "VDD"}


def test_topology_noise_covers_rails_and_grounds():
    """Adjacency / chain inference must ignore both — a shared rail says
    nothing about whether two parts belong together."""
    fn = {"U1": {"VDD", "GND", "NET1"}, "R1": {"NET1", "NET2"},
          "C1": {"VDD", "GND"}, "C2": {"VDD", "GND"}}
    noise = set(classify_nets(fn)["topology_noise"])
    assert {"VDD", "GND"} <= noise
    assert "NET1" not in noise and "NET2" not in noise
