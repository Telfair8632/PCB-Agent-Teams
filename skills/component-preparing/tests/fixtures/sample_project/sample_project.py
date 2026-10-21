"""Minimal sample for inject_mpn_props.py regression test."""
from circuit_synth import Component, Net, circuit


@circuit(name="sample_project")
def sample_project():
    VIN = Net("VIN"); VOUT = Net("VOUT"); GND = Net("GND")
    u1 = Component(symbol="Regulator_Linear:TLV70033DDCR", ref="U1",
                   value="TLV70033DDCR",
                   footprint="Package_TO_SOT_SMD:SOT-23-5")
    u1[1] += VIN; u1[2] += GND; u1[3] += VOUT
    r1 = Component(symbol="Device:R", ref="R1", value="10k",
                   footprint="Resistor_SMD:R_0805_2012Metric")
    r1[1] += VIN; r1[2] += GND
    r2 = Component(symbol="Device:R", ref="R2", value="100R",
                   footprint="Resistor_SMD:R_0805_2012Metric")
    r2[1] += VOUT; r2[2] += GND
