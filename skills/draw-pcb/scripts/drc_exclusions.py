"""DRC exclusion handling — honor user-set exclusions in .kicad_pro.

Adapted from KiKit's drc.py (lib_external/_borrowed/from_kikit/drc.py),
keeping only the parts that don't need pcbnew, so this module runs under
the workspace .venv python (not just the KiCad-bundled python).

The logic comes from KiKit's `CliDrcExclusion` + `_readExclusionsFromProjectFile`
(MIT, Jan Mrázek). Exclusion UUIDs in `.kicad_pro` are matched against
`uuid` fields in kicad-cli's DRC JSON output.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Tuple, Union

NULL_UUID = "00000000-0000-0000-0000-000000000000"

ExclusionKey = Tuple[str, Union[Tuple[()], str, FrozenSet[str]]]


@dataclass
class Exclusion:
    """A DRC exclusion parsed from .kicad_pro (no pcbnew needed)."""
    type: str
    uuids: List[str] = field(default_factory=list)

    def key(self) -> ExclusionKey:
        if not self.uuids:
            return (self.type, ())
        if len(self.uuids) == 1:
            return (self.type, self.uuids[0])
        return (self.type, frozenset(self.uuids))


def read_exclusions_from_pro(board_path: str) -> List[Exclusion]:
    """Read DRC exclusions from the .kicad_pro next to the given board.

    Returns [] when the project has no exclusions or no project file.
    Does NOT raise on missing project — DRC should still report
    everything when the user just hasn't created exclusions yet.
    """
    pro_path = os.path.splitext(board_path)[0] + ".kicad_pro"
    if not os.path.isfile(pro_path):
        return []
    try:
        with open(pro_path, encoding="utf-8") as f:
            project = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    raw = project.get("board", {}).get("design_settings", {}).get("drc_exclusions", [])
    result: List[Exclusion] = []
    for entry in raw:
        # Modern: "type|x|y|uuid|uuid"
        # Legacy: ["type|x|y|uuid|uuid", "comment"]
        if isinstance(entry, list):
            entry = entry[0] if entry else ""
        if not isinstance(entry, str) or "|" not in entry:
            continue
        parts = entry.split("|")
        uuids = [u for u in parts[3:] if u != NULL_UUID]
        result.append(Exclusion(parts[0], uuids))
    return result


def _violation_key(v: Dict) -> ExclusionKey:
    """Build a comparable key from a kicad-cli DRC JSON violation."""
    items = v.get("items", []) or []
    uuids = [it["uuid"] for it in items if isinstance(it, dict) and it.get("uuid")]
    if not uuids:
        return (v.get("type", ""), v.get("description", ""))
    if len(uuids) == 1:
        return (v.get("type", ""), uuids[0])
    return (v.get("type", ""), frozenset(uuids))


def prune(drc_data: Dict, exclusions: List[Exclusion]) -> Dict[str, int]:
    """Remove excluded violations from kicad-cli DRC JSON in-place.

    Returns a counts dict: {removed_violations, removed_unconnected,
    removed_parity, total_excluded}. The input dict's `violations`,
    `unconnected_items`, and `schematic_parity` lists are mutated.
    """
    if not exclusions:
        return {"removed_violations": 0, "removed_unconnected": 0,
                "removed_parity": 0, "total_excluded": 0}
    excluded_keys = {e.key() for e in exclusions}
    counts = {"removed_violations": 0, "removed_unconnected": 0,
              "removed_parity": 0}
    for field_name, counter in (("violations", "removed_violations"),
                                 ("unconnected_items", "removed_unconnected"),
                                 ("schematic_parity", "removed_parity")):
        original = drc_data.get(field_name, [])
        kept = [v for v in original if _violation_key(v) not in excluded_keys]
        counts[counter] = len(original) - len(kept)
        drc_data[field_name] = kept
    counts["total_excluded"] = sum(counts.values())
    return counts
