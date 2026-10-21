#!/usr/bin/env python3
"""Probe local + cached + online KiCad library readiness for component-selecting-JP.

Search order (each level only runs if higher levels did not produce a complete result):

  1. lib_external/components.{kicad_sym,pretty,3dshapes}      (canonical vendored)
  2. KiCad official symbols / footprints (installed cache)    (standard libraries)
  3. lib_cache/sources/kicad-symbols                          (mirrored official)
  4. lib_cache/sources/kicad-footprints                       (mirrored official)
  5. lib_cache/sources/jlcpcb-kicad-library                   (JLC assembly-aware)
  6. lib_cache/sources/digikey-kicad-library                  (Digi-Key legacy)
  7. LCSC dry-run (download_lcsc_lib.py --verify-only)        (network, optional)
  8. Browser probe (DigiKey JP /en/models, Ultra Librarian)   (network, optional)

It returns a 7-class status (per merged component-selecting-JP SKILL):

  vendored_complete         - lib_external has both symbol + footprint, package consistency pass
  standard_ready            - KiCad official has exact symbol bound to footprint, package consistent
  external_cache_exact      - lib_cache exact MPN match outside lib_external
  lcsc_vendorable           - LCSC EasyEDA dry-run succeeds (commit will fetch)
  browser_vendorable        - Vendor EDA page (DigiKey/UL/SnapEDA) confirms symbol+footprint
  external_cache_compatible - only package-compatible footprint match (NOT exact MPN)
  unavailable               - no practical library path

Hard rules baked in:

* `bound_footprint_hits` only counts AFTER `_verify_package_consistency` accepts the
  symbol -> footprint binding. This is the fix for the v2 TES 1-0511 false positive
  where a SIP/THT MPN bound to a KiCad official SMD footprint produced standard_ready.
* lib_cache absence is fine - script keeps running with what's available.
* Network probes are off by default. CLI exposes --include-network-probes; the
  in-process API takes include_network_probes=True/False.

Workspace path:
  __file__ = <root>/.claude/skills/component-selecting-JP/scripts/library_probe.py
  parents[0]=scripts, [1]=component-selecting-JP, [2]=skills, [3]=.claude, [4]=workspace
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time  # V2-only: cache TTL check
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional


# ---------- Paths -----------------------------------------------------------

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
LIB_EXTERNAL = WORKSPACE_ROOT / "lib_external"
LIB_CACHE = WORKSPACE_ROOT / "lib_cache" / "sources"
KICAD_STD_SYMBOLS = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols")
KICAD_STD_FOOTPRINTS = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")


# ---------- Package consistency token sets ----------------------------------
#
# These are *not* a global "MPN ends with X => package = Y" rule. They are
# corroborating signals: when distributor mounting / datasheet wording / footprint
# filename all agree, we accept; when they conflict, we reject. Footprint names
# alone are weak evidence and stay "unknown" unless we also have distributor /
# datasheet context.

SMD_TOKENS = {
    "SMD", "SMT", "SOIC", "SOP", "TSSOP", "SSOP", "QFN", "QFP", "TQFP", "BGA",
    "DFN", "WLCSP", "LGA", "SOT", "SOT23", "SOT223", "MELF", "MicroSMD",
    "0201", "0402", "0603", "0805", "1206", "1210", "2010", "2512",
}
THT_TOKENS = {
    "THT", "TH", "DIP", "PDIP", "SIP", "ZIP", "Radial", "Axial",
    "TO-220", "TO-247", "TO-92", "TO-3", "TO-218",
    "PinHeader", "PinSocket", "ScrewTerminal",
}

_SMD_TOKEN_LOWER = {t.lower() for t in SMD_TOKENS}
_THT_TOKEN_LOWER = {t.lower() for t in THT_TOKENS}

# Distributor / datasheet wording uses prose, so phrase-level regex still helps.
_DISTRIBUTOR_TH_RE = re.compile(
    r"(through[\s\-]?hole|スルーホール|プラグイン|插件|直插|引脚|PTH)",
    re.I,
)
_DISTRIBUTOR_SMD_RE = re.compile(
    r"(surface[\s\-]?mount|表面実装|SMT|SMD|表面贴装|贴片)",
    re.I,
)


def _tokens(text: str) -> list[str]:
    """Lowercase tokens, split on any non-alphanumeric (so underscore-separated
    filenames like Converter_DCDC_Murata_CRE1xxxxxx3C_THT yield ['converter',
    'dcdc', 'murata', 'cre1xxxxxx3c', 'tht'])."""
    return [t for t in re.split(r"[^A-Za-z0-9]+", text or "") if t]


# ---------- Helpers ---------------------------------------------------------


@dataclass
class Hit:
    source: str
    kind: str          # "symbol" | "footprint" | "text" | "bound_footprint"
    path: str
    match: str
    confidence: str    # "exact" | "text" | "bound" | "compatible"


def norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def variants(mpn: str) -> list[str]:
    raw = mpn.strip()
    out = {
        raw,
        raw.upper(),
        raw.replace("/", "_"),
        raw.replace("/", "-"),
        raw.replace(" ", ""),
        raw.replace(" ", "-"),
        raw.replace("-", ""),
        raw.replace("_", "-"),
    }
    return [v for v in out if v]


def walk_files(root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


# ---------- Library cache index --------------------------------------------
#
# The big lib_cache/sources/{kicad-symbols, kicad-footprints, ...} trees hold
# ~485MB / 39k files. Walking + reading them per MPN dominates probe latency
# (single-MPN offline pass: 3-9s; batch of 5 ~50s). We build an index once,
# persist it to disk, and consult the index in O(1)/O(N_in_root) instead of
# re-walking. Invalidation is mtime-based on each indexed root.

_PROBE_INDEX: Optional[dict] = None
_INDEX_FILE = LIB_CACHE / ".probe_index.json"

# (root_path, source_label) — only roots indexed for symbols.
_SYMBOL_INDEX_ROOTS: list[tuple[Path, str]] = [
    (LIB_CACHE / "kicad-symbols", "kicad_symbols_cache"),
    (LIB_CACHE / "jlcpcb-kicad-library", "jlcpcb"),
    (LIB_CACHE / "digikey-kicad-library", "digikey_legacy"),
    (KICAD_STD_SYMBOLS, "kicad_symbols_installed"),
]
# (root_path, source_label) — only roots indexed for footprints.
_FOOTPRINT_INDEX_ROOTS: list[tuple[Path, str]] = [
    (LIB_CACHE / "kicad-footprints", "kicad_footprints_cache"),
    (LIB_CACHE / "jlcpcb-kicad-library", "jlcpcb"),
    (LIB_CACHE / "digikey-kicad-library", "digikey_legacy"),
    (KICAD_STD_FOOTPRINTS, "kicad_footprints_installed"),
]

# Map (resolved abs path) -> source label, for fast O(1) routing in the
# scanner functions below.
_SYMBOL_ROOT_TO_LABEL = {str(r.resolve()): lbl for r, lbl in _SYMBOL_INDEX_ROOTS if r.exists()}
_FOOTPRINT_ROOT_TO_LABEL = {str(r.resolve()): lbl for r, lbl in _FOOTPRINT_INDEX_ROOTS if r.exists()}


def _root_max_mtime(root: Path) -> float:
    """Cheap-ish staleness signal: max mtime across the *top two* directory
    levels under root. Full-tree mtime would defeat the purpose of indexing."""
    if not root.exists():
        return 0.0
    mt = root.stat().st_mtime
    try:
        for child in root.iterdir():
            try:
                mt = max(mt, child.stat().st_mtime)
                if child.is_dir():
                    for grand in child.iterdir():
                        try:
                            mt = max(mt, grand.stat().st_mtime)
                        except OSError:
                            pass
            except OSError:
                pass
    except OSError:
        pass
    return mt


def _build_index() -> dict:
    """Walk every indexed root once, extract symbol names / footprint stems."""
    sym_pat_kicad = re.compile(r'\(symbol\s+"([^"]+)"')
    sym_pat_legacy = re.compile(r"^DEF\s+(\S+)", re.M)

    symbols_by_source: dict[str, dict[str, list[str]]] = {}
    # footprints stored as a flat list of (norm_stem, lower_stem, abs_path) per source
    footprints_by_source: dict[str, list[list[str]]] = {}
    root_mtimes: dict[str, float] = {}

    for root, label in _SYMBOL_INDEX_ROOTS:
        if not root.exists():
            continue
        bucket = symbols_by_source.setdefault(label, {})
        for path in walk_files(root, (".kicad_sym", ".lib", ".dcm")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            names: set[str] = set()
            for m in sym_pat_kicad.finditer(text):
                names.add(m.group(1))
            if path.suffix.lower() == ".lib":
                for m in sym_pat_legacy.finditer(text):
                    names.add(m.group(1))
            if not names:
                continue
            abs_path = str(path)
            for n in names:
                bucket.setdefault(n.upper(), []).append(abs_path)
        root_mtimes[str(root.resolve())] = _root_max_mtime(root)

    for root, label in _FOOTPRINT_INDEX_ROOTS:
        if not root.exists():
            continue
        bucket_fp = footprints_by_source.setdefault(label, [])
        for path in walk_files(root, (".kicad_mod",)):
            stem = path.stem
            # LCSC Part property is the authoritative part identity inside the
            # footprint (vendored files are named by family, not by C-code).
            lcsc_codes = ""
            try:
                _mod_text = path.read_text(encoding="utf-8", errors="ignore")
                _codes = re.findall(
                    r'property\s+"LCSC Part"\s+"([Cc]\d+)"', _mod_text)
                lcsc_codes = " ".join(c.lower() for c in _codes)
            except OSError:
                pass
            bucket_fp.append([norm(stem), stem.lower(), str(path), lcsc_codes])
        if str(root.resolve()) not in root_mtimes:
            root_mtimes[str(root.resolve())] = _root_max_mtime(root)

    import time as _time
    return {
        "built_at": _time.time(),
        "schema": 1,
        "root_mtimes": root_mtimes,
        "symbols_by_source": symbols_by_source,
        "footprints_by_source": footprints_by_source,
    }


def _index_is_fresh(idx: dict) -> bool:
    if not idx or idx.get("schema") != 1:
        return False
    built_at = idx.get("built_at", 0.0)
    for root_str, recorded_mt in (idx.get("root_mtimes") or {}).items():
        current_mt = _root_max_mtime(Path(root_str))
        if current_mt > built_at:
            return False
        if abs(current_mt - recorded_mt) > 1.0:
            return False
    return True


def _get_index() -> dict:
    global _PROBE_INDEX
    if _PROBE_INDEX is not None:
        return _PROBE_INDEX
    # Try disk first
    if _INDEX_FILE.exists():
        try:
            disk_idx = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
            if _index_is_fresh(disk_idx):
                _PROBE_INDEX = disk_idx
                return _PROBE_INDEX
        except (json.JSONDecodeError, OSError):
            pass
    # Build fresh
    idx = _build_index()
    try:
        _INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        _INDEX_FILE.write_text(json.dumps(idx), encoding="utf-8")
    except OSError:
        pass
    _PROBE_INDEX = idx
    return _PROBE_INDEX


def _root_label_for_symbols(root: Path) -> Optional[str]:
    return _SYMBOL_ROOT_TO_LABEL.get(str(root.resolve()))


def _root_label_for_footprints(root: Path) -> Optional[str]:
    return _FOOTPRINT_ROOT_TO_LABEL.get(str(root.resolve()))


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return str(path)


def file_contains(path: Path, needles: list[str]) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    low = text.lower()
    for needle in needles:
        if needle.lower() in low:
            return needle
    return None


# ---------- Symbol / footprint scanners -------------------------------------


def exact_symbol_hits(mpn: str, root: Path, source: str) -> list[Hit]:
    # Fast path: use prebuilt index for the big lib_cache / KiCad-installed roots.
    idx_label = _root_label_for_symbols(root)
    if idx_label:
        idx = _get_index()
        bucket = (idx.get("symbols_by_source") or {}).get(idx_label) or {}
        if not bucket:
            return []
        hits: list[Hit] = []
        seen_paths: set[str] = set()
        # Original regex matched if the symbol name == variant OR started with
        # variant followed by '_' or '-'. Both forms reduce to upper-case
        # comparison since the index stores .upper() keys.
        for v in variants(mpn):
            vu = v.upper()
            for name, paths in bucket.items():
                if name == vu or name.startswith(vu + "_") or name.startswith(vu + "-"):
                    for p in paths:
                        if p in seen_paths:
                            continue
                        seen_paths.add(p)
                        hits.append(Hit(source, "symbol", rel(Path(p)), name, "exact"))
        return hits

    # Slow path: legacy direct walk for non-indexed roots (lib_external, etc.)
    hits = []
    pats = []
    for v in variants(mpn):
        pats.append(re.compile(r'\(symbol\s+"' + re.escape(v) + r'(?:"|[_-])', re.I))
        pats.append(re.compile(r"^DEF\s+" + re.escape(v) + r"(?=\s|$)", re.I | re.M))
    for path in walk_files(root, (".kicad_sym", ".lib", ".dcm")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in pats:
            if pat.search(text):
                hits.append(Hit(source, "symbol", rel(path), pat.pattern, "exact"))
                break
    return hits


def text_hits(mpn: str, root: Path, source: str, limit: int = 20) -> list[Hit]:
    hits: list[Hit] = []
    needles = variants(mpn)
    for path in walk_files(root, (".kicad_sym", ".lib", ".dcm", ".kicad_mod")):
        found = file_contains(path, needles)
        if found:
            kind = "footprint" if path.suffix.lower() == ".kicad_mod" else "text"
            hits.append(Hit(source, kind, rel(path), found, "text"))
            if len(hits) >= limit:
                break
    return hits


def footprint_name_hits(mpn: str, root: Path, source: str) -> list[Hit]:
    hits: list[Hit] = []
    normalized = norm(mpn)
    if not normalized:
        return hits
    idx_label = _root_label_for_footprints(root)
    if idx_label:
        idx = _get_index()
        bucket = (idx.get("footprints_by_source") or {}).get(idx_label) or []
        for entry in bucket:
            stem_norm, _stem_lower, abs_path = entry[0], entry[1], entry[2]
            lcsc_codes = entry[3] if len(entry) > 3 else ""
            if normalized in stem_norm or (lcsc_codes and normalized in lcsc_codes):
                p = Path(abs_path)
                hits.append(Hit(source, "footprint", rel(p), p.stem, "exact"))
        return hits
    for path in walk_files(root, (".kicad_mod",)):
        stem_norm = norm(path.stem)
        matched = normalized in stem_norm
        if not matched and re.fullmatch(r"[Cc]\d{4,10}", mpn or ""):
            # C-code identity lives in the file's LCSC Part property, not the stem.
            try:
                _mod_text = path.read_text(encoding="utf-8", errors="ignore")
                matched = re.search(
                    r'property\s+"LCSC Part"\s+"' + re.escape(mpn) + r'"',
                    _mod_text) is not None
            except OSError:
                pass
        if matched:
            hits.append(Hit(source, "footprint", rel(path), path.stem, "exact"))
    return hits


def compatible_package_hits(package_hint: Optional[str], root: Path,
                            source: str, limit: int = 20) -> list[Hit]:
    if not package_hint:
        return []
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", package_hint) if len(t) >= 3]
    if not tokens:
        return []
    needle = [t.lower() for t in tokens[:3]]
    hits: list[Hit] = []
    idx_label = _root_label_for_footprints(root)
    if idx_label:
        idx = _get_index()
        bucket = (idx.get("footprints_by_source") or {}).get(idx_label) or []
        for entry in bucket:
            _stem_norm, stem_lower, abs_path = entry[0], entry[1], entry[2]
            if all(t in stem_lower for t in needle):
                p = Path(abs_path)
                hits.append(Hit(source, "footprint", rel(p), p.stem, "compatible"))
                if len(hits) >= limit:
                    break
        return hits
    for path in walk_files(root, (".kicad_mod",)):
        name = path.stem.lower()
        if all(t in name for t in needle):
            hits.append(Hit(source, "footprint", rel(path), path.stem, "compatible"))
            if len(hits) >= limit:
                break
    return hits


def footprint_path_from_nickname(nickname: str, footprint: str) -> Optional[Path]:
    """Resolve `<lib>:<name>` references to actual files on disk."""
    maps = {
        "components": LIB_EXTERNAL / "components.pretty",
        "digikey-footprints": LIB_CACHE / "digikey-kicad-library" / "digikey-footprints.pretty",
        "JLCPCB": LIB_CACHE / "jlcpcb-kicad-library" / "footprints" / "JLCPCB.pretty",
    }
    if nickname in maps:
        return maps[nickname] / f"{footprint}.kicad_mod"
    cache_official = LIB_CACHE / "kicad-footprints" / f"{nickname}.pretty" / f"{footprint}.kicad_mod"
    if cache_official.exists():
        return cache_official
    installed_official = KICAD_STD_FOOTPRINTS / f"{nickname}.pretty" / f"{footprint}.kicad_mod"
    return installed_official


def symbol_body(text: str, mpn: str, suffix: str) -> str:
    """Return only the matched symbol block so monolithic libraries do not leak refs."""
    if suffix == ".lib":
        for v in variants(mpn):
            match = re.search(r"^DEF\s+" + re.escape(v) + r"(?=\s|$)", text, re.I | re.M)
            if not match:
                continue
            end = re.search(r"^ENDDEF\s*$", text[match.start():], re.M)
            if end:
                return text[match.start():match.start() + end.end()]
        return ""

    if suffix != ".kicad_sym":
        return ""

    for v in variants(mpn):
        match = re.search(r'\(symbol\s+"' + re.escape(v) + r'(?:"|[_-])', text, re.I)
        if not match:
            continue
        depth = 0
        in_string = False
        escaped = False
        for idx in range(match.start(), len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[match.start():idx + 1]
    return ""


# ---------- Package consistency --------------------------------------------


def _classify_text(text: str) -> str:
    """Return 'smd' | 'tht' | 'mixed' | 'unknown' based on token presence.

    Tokenizes on any non-alphanumeric so file paths and underscore-separated
    library names work the same as prose distributor strings.
    """
    if not text:
        return "unknown"
    toks = {t.lower() for t in _tokens(text)}
    has_smd = bool(toks & _SMD_TOKEN_LOWER) or bool(_DISTRIBUTOR_SMD_RE.search(text))
    has_tht = bool(toks & _THT_TOKEN_LOWER) or bool(_DISTRIBUTOR_TH_RE.search(text))
    if has_smd and has_tht:
        return "mixed"
    if has_smd:
        return "smd"
    if has_tht:
        return "tht"
    return "unknown"


def _classify_footprint_path(footprint_path: str) -> str:
    """Classify a footprint by filename / library nickname tokens."""
    if not footprint_path:
        return "unknown"
    return _classify_text(footprint_path)


def _verify_package_consistency(
    mpn: str,
    symbol_info: Optional[dict],
    footprint_path: Optional[str],
    distributor_package: Optional[str] = None,
    datasheet_package: Optional[str] = None,
    distributor_mounting: Optional[str] = None,
) -> dict:
    """Cross-check footprint package class against distributor / datasheet evidence.

    Goal: catch the v2 false-positive case where a THT/SIP MPN was bound to a
    KiCad official SMD footprint and got rubber-stamped as standard_ready.

    Returns:
        {"status": "pass" | "fail" | "unknown" | "compatible_only",
         "reason": "...",
         "evidence": {fp_class, distributor_class, datasheet_class, ...}}

    Rules (no MPN-suffix global hacks):
      1. footprint contradicts distributor_mounting (TH vs SMD or vice versa) -> fail
      2. footprint contradicts datasheet_package wording -> fail
      3. footprint contradicts distributor_package wording -> fail
      4. all signals agree (or only one signal exists) -> pass
      5. only compatible-package evidence (no exact MPN bound) -> compatible_only
      6. nothing to compare on -> unknown
    """
    fp_cls = _classify_footprint_path(footprint_path or "")
    dpkg_cls = _classify_text(distributor_package or "")
    dmount_cls = _classify_text(distributor_mounting or "")
    dsh_cls = _classify_text(datasheet_package or "")

    evidence = {
        "footprint": {"path": footprint_path, "class": fp_cls},
        "distributor_package": {"text": distributor_package, "class": dpkg_cls},
        "distributor_mounting": {"text": distributor_mounting, "class": dmount_cls},
        "datasheet_package": {"text": datasheet_package, "class": dsh_cls},
    }

    def _conflict(a: str, b: str) -> bool:
        return (a == "smd" and b == "tht") or (a == "tht" and b == "smd")

    # Strongest signal: distributor mounting field (machine-parsed from API)
    if fp_cls != "unknown" and dmount_cls != "unknown" and _conflict(fp_cls, dmount_cls):
        return {
            "status": "fail",
            "reason": f"footprint is {fp_cls} but distributor mounting is {dmount_cls}",
            "evidence": evidence,
        }
    # Datasheet package wording (high signal if user / pipeline has read PDF)
    if fp_cls != "unknown" and dsh_cls != "unknown" and _conflict(fp_cls, dsh_cls):
        return {
            "status": "fail",
            "reason": f"footprint is {fp_cls} but datasheet package is {dsh_cls}",
            "evidence": evidence,
        }
    # Distributor package text
    if fp_cls != "unknown" and dpkg_cls != "unknown" and _conflict(fp_cls, dpkg_cls):
        return {
            "status": "fail",
            "reason": f"footprint is {fp_cls} but distributor package text is {dpkg_cls}",
            "evidence": evidence,
        }

    has_external_signal = any(c != "unknown" for c in (dmount_cls, dsh_cls, dpkg_cls))
    if fp_cls != "unknown" and has_external_signal:
        return {
            "status": "pass",
            "reason": f"footprint class {fp_cls} agrees with external signals",
            "evidence": evidence,
        }
    if fp_cls != "unknown" and not has_external_signal:
        # We have a footprint but no distributor/datasheet to compare. Don't claim pass.
        return {
            "status": "unknown",
            "reason": "no distributor / datasheet package text to corroborate footprint class",
            "evidence": evidence,
        }
    if has_external_signal and fp_cls == "unknown":
        return {
            "status": "unknown",
            "reason": "footprint class unknown",
            "evidence": evidence,
        }
    return {
        "status": "unknown",
        "reason": "no package evidence at all",
        "evidence": evidence,
    }


# ---------- Bound footprint resolution --------------------------------------


def bound_footprint_hits(mpn: str, symbol_hits: list[Hit],
                         distributor_package: Optional[str] = None,
                         distributor_mounting: Optional[str] = None,
                         datasheet_package: Optional[str] = None,
                         ) -> tuple[list[Hit], list[dict]]:
    """For each symbol hit, follow the (property "Footprint" "<lib>:<name>") binding,
    verify the file exists on disk AND passes package consistency, then return
    the surviving footprint hits + a parallel list of consistency reports.
    """
    hits: list[Hit] = []
    reports: list[dict] = []
    seen: set[str] = set()
    for hit in symbol_hits:
        path = WORKSPACE_ROOT / hit.path
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        body = symbol_body(text, mpn, path.suffix.lower())
        if not body:
            continue
        refs = re.findall(r'\(property\s+"Footprint"\s+"([^"]+)"', body)
        refs.extend(re.findall(r'^F2\s+"([^"]+)"', body, flags=re.M))
        for ref in refs:
            if ":" not in ref:
                continue
            nickname, footprint = ref.split(":", 1)
            fp_path = footprint_path_from_nickname(nickname, footprint)
            if not fp_path or not fp_path.exists():
                continue
            key = str(fp_path)
            if key in seen:
                continue
            seen.add(key)

            # Run package consistency BEFORE accepting this binding as evidence.
            consistency = _verify_package_consistency(
                mpn,
                symbol_info={"source": hit.source, "path": hit.path},
                footprint_path=str(fp_path),
                distributor_package=distributor_package,
                distributor_mounting=distributor_mounting,
                datasheet_package=datasheet_package,
            )
            if consistency["status"] == "fail":
                reports.append({
                    "ref": ref, "fp_path": rel(fp_path), "consistency": consistency,
                    "accepted": False,
                })
                continue

            source = "bound_footprint"
            if "kicad-footprints" in key:
                source = "kicad_footprints"
            elif "digikey-kicad-library" in key:
                source = "digikey_legacy"
            elif "jlcpcb-kicad-library" in key:
                source = "jlcpcb"
            elif "lib_external" in key:
                source = "lib_external"
            elif str(KICAD_STD_FOOTPRINTS) in key:
                source = "kicad_official"
            hits.append(Hit(source, "bound_footprint", rel(fp_path), ref, "bound"))
            reports.append({
                "ref": ref, "fp_path": rel(fp_path), "consistency": consistency,
                "accepted": True,
            })
    return hits, reports


# ---------- Optional network probes (off by default) -----------------------


def _v2_lcsc_cache_path(mpn: str) -> Path:
    """V2-only cache path. Stored under lib_cache/lcsc_dryrun_cache_v2/."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", mpn)
    return WORKSPACE_ROOT / "lib_cache" / "lcsc_dryrun_cache_v2" / f"{safe}.json"


def _v2_read_lcsc_cache(mpn: str, ttl_days: int = 7) -> Optional[dict]:
    """Return cached LCSC dryrun if fresh (<7 days), else None.
    Cache hit = 0 network = immune to jlcsearch.tscircuit.com instability."""
    path = _v2_lcsc_cache_path(mpn)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_days * 86400:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _v2_write_lcsc_cache(mpn: str, result: dict) -> None:
    """Cache only confirmed network responses (subprocess succeeded + parsed)."""
    path = _v2_lcsc_cache_path(mpn)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # cache write failure is non-fatal


def _network_probe_lcsc(mpn: str, timeout_sec: int = 30) -> dict:
    """V2-only: LCSC dryrun with cache + retry network resilience.

    Differences from V1's library_probe.py:
      1. Cache successful network calls (TTL 7 days). Cache hit = 0 network.
      2. Retry once on transient subprocess failure (timeout/exception).
      3. Mark `transient=True` when network fails — distinguishable from
         "LCSC confirmed no library" (complete=False).
      Transient errors do NOT poison the cache.
    """
    cached = _v2_read_lcsc_cache(mpn)
    if cached is not None:
        cached["from_cache"] = True
        return cached

    out = {"ok": False, "complete": False, "error": None, "raw": None}
    downloader = WORKSPACE_ROOT / ".claude/skills/draw-schematic/scripts/download_lcsc_lib.py"
    if not downloader.exists():
        out["error"] = f"downloader_not_found: {downloader}"
        return out

    for attempt in range(2):  # 1 try + 1 retry
        try:
            res = subprocess.run(
                [sys.executable, str(downloader), "--verify-only", mpn],
                cwd=str(WORKSPACE_ROOT),
                capture_output=True, text=True, timeout=timeout_sec,
            )
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            out["transient"] = True
            if attempt < 1:
                time.sleep(0.3)
                continue
            return out

        try:
            results = json.loads(res.stdout or "[]")
            if isinstance(results, list) and results:
                r = results[0]
                out["raw"] = r
                out["complete"] = bool(r.get("ok"))
                out["ok"] = True
                _v2_write_lcsc_cache(mpn, dict(out))
                return out
            # Empty array = LCSC said "no match" (real negative answer)
            out["ok"] = True
            out["complete"] = False
            _v2_write_lcsc_cache(mpn, dict(out))
            return out
        except Exception as e:
            out["error"] = f"parse_failed: {e}"
            out["transient"] = True
            if attempt < 1:
                time.sleep(0.3)
                continue
            return out
    return out


# Tier 5 (_network_probe_browser) was removed in V2 — it never actually worked.
# The original implementation always returned a placeholder error
# ("browser_probe_handled_inline_by_select_component") because library_probe.py
# couldn't easily reuse select_component.py's in-process agent-browser probe.
# Real agent-browser library fetches still happen in V1's --commit flow; they
# are not gated by Phase 1 in V2's selection pipeline.


# ---------- Main probe ------------------------------------------------------


def probe(
    mpn: str,
    package_hint: Optional[str] = None,
    distributor_package: Optional[str] = None,
    distributor_mounting: Optional[str] = None,
    datasheet_package: Optional[str] = None,
    locale_block: Optional[dict] = None,
    dk_part_id: Optional[str] = None,
    include_network_probes: bool = False,
) -> dict:
    """Probe library readiness for a single MPN. Returns a dict with:

      status: 7-class library status (see module docstring)
      package_consistency: {status, reason, evidence}
      sources: list of evidence Hit dicts that contributed to the decision
      lib_external / cache / network: per-tier raw findings
      notes: list of caveats for the LLM/caller
    """
    notes: list[str] = []

    # Tier 1: lib_external
    le_sym = exact_symbol_hits(mpn, LIB_EXTERNAL, "lib_external")
    le_fp = footprint_name_hits(mpn, LIB_EXTERNAL / "components.pretty", "lib_external")

    # Tier 2-6: cached / installed sources
    tier_sources = {
        "kicad_symbols_installed": (KICAD_STD_SYMBOLS, "kicad_symbols_installed"),
        "kicad_footprints_installed": (KICAD_STD_FOOTPRINTS, "kicad_footprints_installed"),
        "kicad_symbols_cache": (LIB_CACHE / "kicad-symbols", "kicad_symbols_cache"),
        "kicad_footprints_cache": (LIB_CACHE / "kicad-footprints", "kicad_footprints_cache"),
        "jlcpcb": (LIB_CACHE / "jlcpcb-kicad-library", "jlcpcb"),
        "digikey_legacy": (LIB_CACHE / "digikey-kicad-library", "digikey_legacy"),
    }

    source_hits: list[Hit] = []
    for name, (root, label) in tier_sources.items():
        if "symbols" in name or label in {"jlcpcb", "digikey_legacy"}:
            source_hits.extend(exact_symbol_hits(mpn, root, label))
        if "footprints" in name or label in {"jlcpcb", "digikey_legacy"}:
            source_hits.extend(footprint_name_hits(mpn, root, label))
        if not source_hits and label in {"jlcpcb", "digikey_legacy"}:
            # Legacy text fallback (Digi-Key library indexes by DK part #, not MPN)
            source_hits.extend(text_hits(mpn, root, label, limit=5))

    # Resolve symbol -> bound footprint AFTER consistency check
    bound_hits, bound_reports = bound_footprint_hits(
        mpn,
        le_sym + source_hits,
        distributor_package=distributor_package,
        distributor_mounting=distributor_mounting,
        datasheet_package=datasheet_package,
    )

    # Compatible package fallback (only used to label external_cache_compatible)
    compatible_hits = []
    compatible_hits.extend(compatible_package_hits(
        package_hint, LIB_CACHE / "kicad-footprints", "kicad_footprints_cache"))
    compatible_hits.extend(compatible_package_hits(
        package_hint, LIB_CACHE / "jlcpcb-kicad-library", "jlcpcb"))
    compatible_hits.extend(compatible_package_hits(
        package_hint, KICAD_STD_FOOTPRINTS, "kicad_footprints_installed"))

    # Network tiers (optional) — initialized lazily after local decision so
    # parts that are already vendored/cached locally don't pay the LCSC dry-run
    # + UL session round-trip cost.
    network: dict = {"lcsc": None, "browser": None}

    # ---------- Decision tree (top to bottom) ----------
    package_consistency = {"status": "unknown", "reason": "no decision yet", "evidence": {}}
    status = "unavailable"
    decision_evidence: list[Hit] = []

    # Tier 1: lib_external complete
    # Footprint can come from either:
    #   (a) name match: footprint file *stem* contains the MPN (legacy/UL/SnapEDA case);
    #       captured as `le_fp` via footprint_name_hits.
    #   (b) bound binding: the symbol's `Footprint` property points to a file that
    #       exists in lib_external/components.pretty/ (canonical easyeda2kicad case
    #       where the footprint file is named by package, e.g. "TI_SOP-8.kicad_mod"
    #       — won't match by MPN substring). Captured by bound_footprint_hits with
    #       source=="lib_external" and already package-consistency-checked.
    le_bound_fp = [h for h in bound_hits if h.source == "lib_external"]
    le_fp_any = le_fp + le_bound_fp
    le_complete = bool(le_sym) and bool(le_fp_any)
    if le_complete:
        # Prefer the name-matched footprint as the consistency target if any;
        # otherwise use the bound footprint (already passed consistency in
        # bound_footprint_hits, but we re-run for the same evidence shape).
        fp_path_for_check = (le_fp[0].path if le_fp else le_bound_fp[0].path)
        package_consistency = _verify_package_consistency(
            mpn, symbol_info={"source": "lib_external"},
            footprint_path=fp_path_for_check,
            distributor_package=distributor_package,
            distributor_mounting=distributor_mounting,
            datasheet_package=datasheet_package,
        )
        if package_consistency["status"] == "fail":
            status = "unavailable"
            notes.append("lib_external complete but package consistency failed; treat as unavailable")
        else:
            status = "vendored_complete"
            decision_evidence.extend(le_sym + le_fp_any)
    elif le_sym or le_fp_any:
        # Vendored but partial (only one of symbol/footprint). Per merged plan
        # we no longer expose a vendored_partial state - treat as unavailable
        # and force the caller (commit) to re-fetch or pick another part.
        status = "unavailable"
        which = "symbol" if not le_sym else "footprint"
        notes.append(f"lib_external partial: missing {which}; treat as unavailable")

    # Tier 2-3: KiCad official (installed or cached) with bound footprint
    if status == "unavailable":
        official_sym = [
            h for h in (le_sym + source_hits)
            if h.source in {"kicad_symbols_installed", "kicad_symbols_cache"}
            and h.confidence == "exact"
        ]
        official_bound = [
            h for h in bound_hits
            if h.source in {"kicad_official", "kicad_footprints", "kicad_footprints_cache",
                            "kicad_footprints_installed"}
        ]
        if official_sym and official_bound:
            # bound_footprint_hits already package-consistency-checked.
            fp_path = official_bound[0].path
            package_consistency = _verify_package_consistency(
                mpn, symbol_info={"source": official_sym[0].source},
                footprint_path=fp_path,
                distributor_package=distributor_package,
                distributor_mounting=distributor_mounting,
                datasheet_package=datasheet_package,
            )
            if package_consistency["status"] != "fail":
                status = "standard_ready"
                decision_evidence.extend(official_sym[:1] + official_bound[:1])

    # Tier 4-6: external cache exact MPN hit
    # Require BOTH exact symbol/MPN evidence AND a usable footprint (either a
    # consistency-passing bound footprint OR a name-matching footprint hit).
    # Without a usable footprint we must NOT label external_cache_exact -- that
    # status implies "commit will succeed", which is a lie when consistency
    # already rejected the only binding.
    if status == "unavailable":
        external_exact = [
            h for h in source_hits
            if h.confidence == "exact"
            and h.source in {"jlcpcb", "digikey_legacy", "kicad_symbols_cache",
                             "kicad_footprints_cache"}
        ]
        external_bound = [
            h for h in bound_hits
            if h.source in {"jlcpcb", "digikey_legacy", "kicad_footprints_cache"}
        ]
        external_fp_name_hits = [
            h for h in source_hits
            if h.kind == "footprint" and h.confidence == "exact"
            and h.source in {"jlcpcb", "digikey_legacy", "kicad_footprints_cache"}
        ]
        usable_fp = external_bound or external_fp_name_hits
        if external_exact and usable_fp:
            fp_path = usable_fp[0].path
            package_consistency = _verify_package_consistency(
                mpn, symbol_info={"source": external_exact[0].source},
                footprint_path=fp_path,
                distributor_package=distributor_package,
                distributor_mounting=distributor_mounting,
                datasheet_package=datasheet_package,
            )
            if package_consistency["status"] != "fail":
                status = "external_cache_exact"
                decision_evidence.extend(external_exact[:1] + usable_fp[:1])
                notes.append(
                    "external_cache_exact: vendor only after datasheet pinout/dimension check; "
                    "Digi-Key legacy hits do not prove lifecycle/availability."
                )

    # Tier 7: LCSC dry-run (only called now if local tiers exhausted)
    if status == "unavailable" and include_network_probes:
        network["lcsc"] = _network_probe_lcsc(mpn)
        if (network["lcsc"] or {}).get("complete"):
            status = "lcsc_vendorable"
            notes.append("lcsc_vendorable: commit_part will run LCSC + easyeda2kicad to vendor.")

    # Tier 8 (browser probe) removed in V2 — see comment near top of file.

    # Compatible-only fallback
    if status == "unavailable" and compatible_hits:
        status = "external_cache_compatible"
        package_consistency = {
            "status": "compatible_only",
            "reason": "package-compatible footprint match only; no exact MPN binding",
            "evidence": {"compatible_count": len(compatible_hits)},
        }
        notes.append(
            "external_cache_compatible: NOT a pinout guarantee; exclude from default shortlist."
        )

    return {
        "mpn": mpn,
        "normalized": norm(mpn),
        "workspace_root": str(WORKSPACE_ROOT),
        "lib_cache_exists": LIB_CACHE.exists(),
        "lib_external_exists": LIB_EXTERNAL.exists(),
        "kicad_std_exists": KICAD_STD_SYMBOLS.exists(),
        "include_network_probes": include_network_probes,
        "status": status,
        "package_consistency": package_consistency,
        "lib_external": {
            "symbol_hits": [asdict(h) for h in le_sym],
            "footprint_hits": [asdict(h) for h in le_fp],
        },
        "source_hits": [asdict(h) for h in source_hits[:50]],
        "bound_footprint_hits": [asdict(h) for h in bound_hits[:25]],
        "bound_footprint_reports": bound_reports[:25],
        "compatible_hits": [asdict(h) for h in compatible_hits[:25]],
        "network": network,
        "decision_evidence": [asdict(h) for h in decision_evidence],
        "notes": notes + [
            "3D model is not a hard gate unless mechanical fit matters.",
            "Compatible hits require datasheet pinout and dimensions check.",
            "Digi-Key legacy hits do not prove lifecycle or availability.",
        ],
    }


# ---------- CLI -------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mpn", required=True)
    parser.add_argument("--package-hint", default=None,
                        help="Optional package text, e.g. 'SIP3 2.54mm'")
    parser.add_argument("--distributor-package", default=None,
                        help="Distributor parameter 'パッケージ/ケース' / 'Package / Case'")
    parser.add_argument("--distributor-mounting", default=None,
                        help="Distributor parameter '取り付けタイプ' / 'Mounting Type' "
                             "(e.g. 'Through Hole', 'スルーホール', 'Surface Mount')")
    parser.add_argument("--datasheet-package", default=None,
                        help="Optional package wording extracted from datasheet PDF")
    parser.add_argument("--include-network-probes", action="store_true",
                        help="Run LCSC dry-run + agent-browser probes (default off)")
    parser.add_argument("--summary", action="store_true",
                        help="Print human-readable summary instead of full JSON")
    args = parser.parse_args()

    result = probe(
        args.mpn,
        package_hint=args.package_hint,
        distributor_package=args.distributor_package,
        distributor_mounting=args.distributor_mounting,
        datasheet_package=args.datasheet_package,
        include_network_probes=args.include_network_probes,
    )
    if args.summary:
        print(f"MPN: {result['mpn']}")
        print(f"Status: {result['status']}")
        pkg = result.get("package_consistency", {})
        print(f"Package consistency: {pkg.get('status')} ({pkg.get('reason')})")
        print(f"lib_external symbols: {len(result['lib_external']['symbol_hits'])}")
        print(f"lib_external footprints: {len(result['lib_external']['footprint_hits'])}")
        print(f"source hits: {len(result['source_hits'])}")
        print(f"bound footprint hits: {len(result['bound_footprint_hits'])}")
        print(f"compatible hits: {len(result['compatible_hits'])}")
        for h in result["source_hits"][:8]:
            print(f"  - {h['source']} {h['kind']} {h['confidence']}: {h['path']}")
        for h in result["bound_footprint_hits"][:5]:
            print(f"  - bound {h['source']} {h['path']} (ref={h['match']})")
        for h in result["compatible_hits"][:5]:
            print(f"  - compatible {h['source']} {h['path']}")
        for n in result["notes"]:
            print(f"  note: {n}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
