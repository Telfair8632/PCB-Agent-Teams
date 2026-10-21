"""draw-pcb placement v2 — generic deterministic 4-phase pipeline.

Phases (each independently testable):
    A. partition  — netlist → {ref: region}        (regex + multi-net vote)
    B. floorplan  — regions → board rect + slots    (closed-form geometry)
    C. layout     — per-region grid lay             (deterministic; pairs/chains pre-arranged)
    D. writeback  — apply (x,y,rot) to .kicad_pcb   (existing helper handles this)

Skill code stays generic. All project-specific knowledge (which nets count
as which region, which chains exist, where the isolation slots go) is
read from project CLAUDE.md by the orchestrator and passed in as data.
"""

from .partition import partition_components, DEFAULT_REGION_REGEX

__all__ = ["partition_components", "DEFAULT_REGION_REGEX"]
