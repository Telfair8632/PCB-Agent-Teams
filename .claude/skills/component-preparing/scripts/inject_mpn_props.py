#!/usr/bin/env python3
"""
Inject MPN + Datasheet into circuit-synth .py Component() calls.

Why: analyze_schematic.py emits SS-001 ("BOM has <50% MPN coverage") and
DS-001 ("can't claim verified") whenever symbols in the .kicad_sch lack a
populated MPN field. evidence already lives in `.bom_readiness.json` +
per-MPN JSON files, but it never makes it into the .py → .kicad_sch chain.

This script bridges that gap:
  1. Read sentinel components[] (ref → mpn → datasheet)
  2. Parse .py with stdlib ast; find every `Component(...)` call
  3. For each call where:
       - ref matches a sentinel entry with a real MPN (mpn != value)
       - datasheet evidence file exists
       - no `properties=` kwarg already present
     inject `properties={"MPN": "<mpn>", "Datasheet": "datasheets/<file>"}`
     before the call's closing paren.
  4. Apply edits in reverse byte-offset order so positions don't shift.

Skipped silently:
  - Generic passives where mpn == value (e.g., R "1M", C "390pF")
  - Components whose evidence has no datasheet
  - Calls that already declare `properties=`

Usage:
    python3 inject_mpn_props.py <project.py>
    python3 inject_mpn_props.py <project.py> --dry-run  # preview only

Exit codes:
  0   success (or nothing to inject)
  1   sentinel missing / .py unparseable / write failed
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
import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _load_sentinel(py_file: Path) -> Optional[dict]:
    """Resolve sentinel path from .py location: <project>/datasheets/.bom_readiness.json."""
    candidates = [
        py_file.parent / "datasheets" / ".bom_readiness.json",         # .py in project root
        py_file.parent.parent / "datasheets" / ".bom_readiness.json",  # .py in kicad/
    ]
    for c in candidates:
        if c.exists():
            return json.loads(c.read_text(encoding="utf-8"))
    return None


def _resolve_evidence(pdf_path: Path, sentinel_mpn: str,
                       evidence_dir: Path) -> Dict[str, str]:
    """
    Resolve canonical {mpn, manufacturer} from per-MPN evidence JSON.

    Priority:
      1. Evidence JSON for PDF stem (e.g., RC0805FR-07100RL.json)
         — its `mpn` and `manufacturer` are the source of truth.
      2. PDF stem as MPN, manufacturer empty.
      3. Sentinel `mpn` (last resort — may be value-as-mpn).

    Sentinel.mpn is unreliable for refs where component-preparing categorised
    a Yageo R "100R" with datasheet RC0805FR-07100RL.pdf as connector (sentinel
    stores mpn="100R" but the canonical Yageo MPN is RC0805FR-07100RL).
    """
    if not pdf_path:
        return {"mpn": sentinel_mpn, "manufacturer": ""}
    stem = pdf_path.stem
    safe_stem = stem.replace('.', '_')
    for candidate in (stem, safe_stem):
        ev_file = evidence_dir / f"{candidate}.json"
        if ev_file.exists():
            try:
                ev = json.loads(ev_file.read_text(encoding="utf-8"))
                ev_mpn = ev.get("mpn")
                if ev_mpn:
                    return {
                        "mpn": ev_mpn,
                        "manufacturer": ev.get("manufacturer", "") or "",
                    }
            except (json.JSONDecodeError, OSError):
                pass
    if stem and stem.lower() not in ("none", ""):
        return {"mpn": stem, "manufacturer": ""}
    return {"mpn": sentinel_mpn, "manufacturer": ""}


def _evidence_by_ref(sentinel: dict, project_root: Path) -> Dict[str, Dict]:
    """
    Build {ref: {mpn, manufacturer, datasheet_rel}} from sentinel + per-MPN
    evidence JSONs. Skip refs without a datasheet file (generic passives).

    Each field is resolved canonically (evidence JSON > PDF stem > sentinel)
    to avoid the resistor-as-connector value-as-mpn bug where sentinel.mpn="100R"
    instead of "RC0805FR-07100RL".
    """
    evidence_dir = project_root / "datasheets" / "component_selecting"
    out = {}
    for c in sentinel.get("components", []):
        ref = c.get("ref")
        sentinel_mpn = c.get("mpn", "") or ""
        ds_abs = c.get("datasheet")
        if not ref or not ds_abs:
            continue
        ds_path = Path(ds_abs)
        ev = _resolve_evidence(ds_path, sentinel_mpn, evidence_dir)
        if not ev["mpn"]:
            continue
        try:
            ds_rel = ds_path.resolve().relative_to(project_root.resolve())
            ds_str = str(ds_rel)
        except ValueError:
            ds_str = ds_abs
        out[ref] = {
            "mpn": ev["mpn"],
            "manufacturer": ev["manufacturer"],
            "datasheet": ds_str,
            "sentinel_mpn": sentinel_mpn,
        }
    return out


def _component_call_meta(node: ast.Call) -> Optional[Dict]:
    """
    Return {ref, value, has_mpn, end_lineno, end_col_offset} for a
    Component(...) call. Returns None if it isn't a Component call or lacks ref.

    has_mpn is True when an `MPN` direct-kwarg is already present (means we
    already injected, idempotent skip).
    """
    if not isinstance(node.func, ast.Name) or node.func.id != "Component":
        return None
    ref = value = None
    has_mpn = False
    for kw in node.keywords:
        if kw.arg == "ref" and isinstance(kw.value, ast.Constant):
            ref = kw.value.value
        elif kw.arg == "value" and isinstance(kw.value, ast.Constant):
            value = kw.value.value
        elif kw.arg in ("MPN", "Mpn", "mpn"):
            has_mpn = True
    if ref is None:
        return None
    return {
        "ref": ref,
        "value": value,
        "has_mpn": has_mpn,
        "end_lineno": node.end_lineno,
        "end_col_offset": node.end_col_offset,
    }


def _line_col_to_offset(text: str, line: int, col: int) -> int:
    """Convert (1-based line, ast utf-8 *byte* col) to absolute char offset.

    ast `col_offset` / `end_col_offset` are UTF-8 byte offsets, not character
    indices. Earlier whole lines are summed in characters (correct for the
    str slicing in inject()); `col` must be converted bytes->chars within its
    own line, else non-ASCII before the injection point (e.g. a Chinese
    description on the closing-paren line) shifts the offset and corrupts the
    .py source.
    """
    lines = text.splitlines(keepends=True)
    prefix_chars = sum(len(l) for l in lines[: line - 1])
    target_line = lines[line - 1] if 0 <= line - 1 < len(lines) else ""
    col_chars = len(target_line.encode("utf-8")[:col].decode("utf-8", errors="ignore"))
    return prefix_chars + col_chars


def _build_props_snippet(mpn: str, datasheet: str, manufacturer: str = "") -> str:
    """The text to inject before the closing paren.

    Uses circuit-synth direct kwargs (MPN=, Datasheet=, Manufacturer=) — these
    propagate as instance-level properties in the generated .kicad_sch. The
    dict-style `properties={...}` kwarg gets serialized as a single
    JSON-stringified property named "properties" (not what KiCad analyzers
    want).
    """
    mpn_q = mpn.replace('"', '\\"')
    ds_q = datasheet.replace('"', '\\"')
    parts = [f'MPN="{mpn_q}"', f'Datasheet="{ds_q}"']
    if manufacturer:
        mfg_q = manufacturer.replace('"', '\\"')
        parts.append(f'Manufacturer="{mfg_q}"')
    return ', ' + ', '.join(parts)


def inject(py_file: Path, sentinel: dict, dry_run: bool = False) -> Tuple[int, List[str]]:
    """
    Inject properties into .py. Returns (n_injected, log_lines).
    """
    project_root = py_file.parent.parent if py_file.parent.name == "kicad" else py_file.parent
    ev_by_ref = _evidence_by_ref(sentinel, project_root)

    src = py_file.read_text(encoding="utf-8")
    tree = ast.parse(src)

    edits: List[Tuple[int, str, str]] = []  # (offset, snippet, log_msg)
    skipped: List[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        meta = _component_call_meta(node)
        if not meta:
            continue
        ref = meta["ref"]
        value = meta["value"]
        if meta["has_mpn"]:
            skipped.append(f"  - {ref}: already has MPN= kwarg")
            continue
        ev = ev_by_ref.get(ref)
        if not ev:
            # No evidence: either generic passive (R "1M", C "390pF" — no
            # distributor MPN) or sentinel was missing this ref. Either way,
            # nothing to inject.
            skipped.append(f"  - {ref}: no MPN+datasheet evidence (generic passive or unverified)")
            continue
        # Note: mpn == value is normal for ICs/connectors per passive_value_rule
        # (value silks the MPN like "AMC1311BDWVR" / "KF128-5.08-2P"). Still
        # inject Datasheet (and redundant MPN field for analyzer compatibility).
        offset = _line_col_to_offset(src, meta["end_lineno"], meta["end_col_offset"])
        # offset points to char *after* the call; inject before the closing ')'
        # which is at offset-1 in the source (end_col_offset is 1 past last char).
        snippet = _build_props_snippet(ev["mpn"], ev["datasheet"],
                                        ev.get("manufacturer", ""))
        mfg_part = f" Mfg={ev['manufacturer']}" if ev.get("manufacturer") else ""
        edits.append((offset - 1, snippet,
                      f"  + {ref}: MPN={ev['mpn']} Datasheet={ev['datasheet']}{mfg_part}"))

    if not edits:
        log = ["No injections needed (all components already have properties or no evidence)."]
        log.extend(skipped)
        return 0, log

    # Apply in reverse so earlier offsets remain valid
    edits.sort(key=lambda e: e[0], reverse=True)
    new_src = src
    for offset, snippet, _ in edits:
        new_src = new_src[:offset] + snippet + new_src[offset:]

    log = [f"Injected {len(edits)} properties:"]
    for _, _, msg in sorted(edits, key=lambda e: e[2]):
        log.append(msg)
    if skipped:
        log.append(f"Skipped {len(skipped)}:")
        log.extend(skipped)

    if not dry_run:
        py_file.write_text(new_src, encoding="utf-8")
        log.append(f"✓ Wrote {py_file}")
    else:
        log.append("(dry-run — file not modified)")

    return len(edits), log


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("py_file", type=Path)
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would change but don't write")
    args = p.parse_args()

    if not args.py_file.exists():
        print(f"❌ {args.py_file} not found", file=sys.stderr)
        sys.exit(1)

    sentinel = _load_sentinel(args.py_file)
    if not sentinel:
        print("❌ no .bom_readiness.json sentinel — run check_readiness.py first",
              file=sys.stderr)
        sys.exit(1)

    n, log = inject(args.py_file, sentinel, dry_run=args.dry_run)
    for line in log:
        print(line)
    sys.exit(0 if n >= 0 else 1)


if __name__ == "__main__":
    main()
