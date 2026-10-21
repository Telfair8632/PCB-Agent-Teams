#!/usr/bin/env python3
"""verify_vendoring — Phase 2.5 hard gate.

For every MPN in the project's evidence JSONs (Projects/<name>/datasheets/
component_selecting/*.json), check whether its library.status is one that
permits skipping vendoring. Enum is the same one selecting writes via
commit_part — see GOOD_LIBRARY_STATUSES in component-selecting-JP.

  ALLOWED (no further action needed):
    - vendored_complete     : symbol+footprint already in lib_external/
    - passive_generic       : R/C/L/D/etc, KiCad std Device:R/C covers
    - standard_ready        : exact MPN match in KiCad std lib — Phase 3 .py
                              should reference it as <KiCadLib>:<Symbol>
                              directly (no lib_external commit needed)

  NEEDS_LCSC_COMMIT (Phase 2.5 commit required — fail until run):
    - lcsc_vendorable       : easyeda2kicad probe passed; same call commits
                              symbol+footprint to lib_external/components.*
    - external_cache_exact  : exact MPN in lib_cache/sources/ (gitignored;
                              not visible to Phase 3) — must commit into
                              lib_external. Same easyeda2kicad call works
                              because lib_cache was originally populated by
                              an LCSC probe.

  REJECTED (substitute reuse — must vendor real MPN):
    - kicad_std_compatible  : "borrowed" KiCad std footprint, pad pitch may differ
    - compatible_existing   : reuses some other vendored component
    - any other status      : ambiguous → demand vendoring

Legacy alias: older evidence written before standard_ready existed used
"kicad_std" — treated as standard_ready for back-compat.

Sanity-checks per status:
  - vendored_complete       → grep lib_external/components.kicad_sym
  - standard_ready          → check lib_cache/sources/kicad-symbols/<lib>/<sym>
  - NEEDS_LCSC_COMMIT       → grep lib_external/components.kicad_sym (must
                              be present after vendor_mpn.py runs)

Output: human-readable summary + JSON report.
Exit: 0 if all pass, 2 if any item rejected.

Usage:
  python3 verify_vendoring.py Projects/<name>
  python3 verify_vendoring.py Projects/<name> --json-output gate.json

This script is the **only authoritative end-of-Phase-2.5 check**. SKILL.md
documents the workflow but the actual gate runs through this script — LLM
cannot rationalize past a non-zero exit code.
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
import json
import math
import re
import sys
from pathlib import Path

# Generic pad-pitch tolerance for the footprint-vs-datasheet numeric compare.
# ±5% is a conventional default: it absorbs unit-rounding (2.54 vs 2.5 mm
# nominal naming) while still catching a real mismatch like 2.54 vs 3.5 mm.
PITCH_TOLERANCE_FRAC = 0.05

# Footprint library statuses that mean "borrowed / package-compatible" rather
# than an exact-MPN library. For these the pinout/pad order is NOT guaranteed
# to belong to this MPN, so a human must verify it against the datasheet.
# (Mirrors selecting's compatible-only labelling + STATUS_REJECTED here.)
PINOUT_VERIFY_STATUSES = {
    "kicad_std_compatible",
    "compatible_existing",
    "external_cache_compatible",
}

# Status classification — must stay in sync with selecting's GOOD_LIBRARY_STATUSES
# (see component-selecting-JP/scripts/component_select.py). The 5 statuses below
# are all the values selecting writes via commit_part; verify must classify each.
#
# ALLOWED = no further vendoring action needed (symbol is already reachable).
# NEEDS_LCSC_COMMIT = preparing must run download_lcsc_lib.py to commit into
#                     lib_external/components.* (vendor_mpn.py orchestrates this).
# REJECTED = borrowed/substitute reuse — must vendor the real MPN's library.
STATUS_ALLOWED = {
    "vendored_complete",      # already in lib_external/components.kicad_sym
    "passive_generic",        # KiCad std Device:R/C/L/D covers
    "standard_ready",         # exact MPN in KiCad std lib — Phase 3 .py uses it directly
}
STATUS_NEEDS_LCSC_COMMIT = {
    "lcsc_vendorable",        # easyeda2kicad probe passed; same call commits
    "external_cache_exact",   # symbol cached in lib_cache/sources/ but that dir is
                              # gitignored — must commit into lib_external for Phase 3
}
STATUS_REJECTED = {
    "kicad_std_compatible",   # borrowed std footprint, pad pitch may differ
    "compatible_existing",    # reuses some other vendored component's lib
}
# Backward-compat alias: older evidence files written before standard_ready was
# coined used "kicad_std" — treat them as the same ALLOWED bucket. New writes
# should use "standard_ready" to stay aligned with selecting.
_STATUS_LEGACY_ALIASES = {"kicad_std": "standard_ready"}


def _short(s: str, n: int = 28) -> str:
    return (s[: n - 1] + "…") if len(s) > n else s


def _verify_in_lib_external(workspace: Path, kicad_symbol: str) -> bool:
    """Return True if symbol exists in lib_external/components.kicad_sym."""
    if not kicad_symbol or ":" not in kicad_symbol:
        return False
    lib, name = kicad_symbol.split(":", 1)
    if lib != "components":
        return False  # not a lib_external symbol
    path = workspace / "lib_external" / "components.kicad_sym"
    if not path.exists():
        return False
    # cheap text search; full s-expression parse is overkill
    needle = f'(symbol "{name}"'
    return needle in path.read_text(encoding="utf-8", errors="ignore")


def _installed_kicad_symbol_roots() -> list[Path]:
    """KiCad install dirs (version-globbed on Windows custom drives too)."""
    import glob as _glob
    import os as _os
    roots = [
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
        Path("/usr/share/kicad/symbols"),
    ]
    for pat in (
        r"C:\Program Files\KiCad\*\share\kicad\symbols",
        r"F:\KiCad\*\share\kicad\symbols",
        r"F:\KiCad\share\kicad\symbols",
    ):
        roots.extend(Path(p) for p in _glob.glob(pat))
    return [r for r in roots if r.is_dir()]


def _verify_in_kicad_std(workspace: Path, kicad_symbol: str) -> bool:
    """Return True if symbol exists in KiCad std lib_cache or installed KiCad."""
    if not kicad_symbol or ":" not in kicad_symbol:
        return False
    lib, name = kicad_symbol.split(":", 1)
    if lib in ("Device", "Connector_Generic", "power", "Switch"):
        # generic libraries — assume always present (well-known)
        return True
    sym_dir = workspace / "lib_cache" / "sources" / "kicad-symbols" / f"{lib}.kicad_symdir"
    if sym_dir.is_dir():
        return (sym_dir / f"{name}.kicad_sym").exists()
    # installed KiCad (Windows / Linux / macOS custom locations)
    for root in _installed_kicad_symbol_roots():
        lib_file = root / f"{lib}.kicad_sym"
        if lib_file.exists():
            if f'(symbol "{name}"' in lib_file.read_text(encoding="utf-8", errors="ignore"):
                return True
    return False


def _parse_pitch_mm(text) -> float | None:
    """Parse a pitch value into millimetres from a distributor/datasheet string.

    Accepts forms like '2.54mm', '2.50 mm', '0.1\"', '100 mil', '2.54'. Returns
    None when no number is recoverable. Inch/mil are converted to mm so the
    numeric compare is unit-consistent.
    """
    if text is None:
        return None
    s = str(text).strip().lower()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    val = float(m.group(1))
    if "mil" in s:
        return val * 0.0254
    if '"' in s or "inch" in s or "in." in s:
        return val * 25.4
    # default: already mm (or bare number assumed mm)
    return val


def _resolve_footprint_file(workspace: Path, kicad_footprint: str) -> Path | None:
    """Find the .kicad_mod file for a 'Lib:Name' footprint reference.

    Searches lib_external/<Lib>.pretty first, then the KiCad standard footprint
    dirs. Returns None when not resolvable (e.g. KiCad std lib not installed) —
    callers treat that as 'pitch unverifiable', never a false fail.
    """
    if not kicad_footprint or ":" not in kicad_footprint:
        return None
    lib, name = kicad_footprint.split(":", 1)
    candidates = [
        workspace / "lib_external" / f"{lib}.pretty" / f"{name}.kicad_mod",
    ]
    kicad_fp_roots = [
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
        Path("/usr/share/kicad/footprints"),
        Path("/usr/local/share/kicad/footprints"),
    ]
    import glob as _glob
    for pat in (
        r"C:\Program Files\KiCad\*\share\kicad\footprints",
        r"F:\KiCad\*\share\kicad\footprints",
        r"F:\KiCad\share\kicad\footprints",
    ):
        kicad_fp_roots.extend(Path(p) for p in _glob.glob(pat))
    for root in kicad_fp_roots:
        candidates.append(root / f"{lib}.pretty" / f"{name}.kicad_mod")
    for c in candidates:
        if c.exists():
            return c
    return None


def _footprint_pad_pitch_mm(mod_path: Path) -> float | None:
    """Smallest centre-to-centre distance between numbered pads in a .kicad_mod.

    Returns the minimum nearest-neighbour pad spacing in mm, which for a normal
    in-line/grid footprint is the pad pitch. Returns None when fewer than two
    locatable pads (can't define a pitch). Best-effort regex parse — never
    raises; on any parse trouble returns None so the check degrades gracefully.
    """
    try:
        text = mod_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # Match: (pad "1" smd|thru_hole ... (at X Y [rot]) ...)  — capture name+coords
    coords: list[tuple[float, float]] = []
    pad_re = re.compile(
        r'\(pad\s+"[^"]*"\s+\S+\s+\S+\s*.*?\(at\s+(-?[0-9.]+)\s+(-?[0-9.]+)',
        re.DOTALL,
    )
    for m in pad_re.finditer(text):
        try:
            coords.append((float(m.group(1)), float(m.group(2))))
        except ValueError:
            continue
    if len(coords) < 2:
        return None
    best = None
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            d = math.hypot(coords[i][0] - coords[j][0], coords[i][1] - coords[j][1])
            if d > 1e-6 and (best is None or d < best):
                best = d
    return round(best, 4) if best is not None else None


def _check_pitch(workspace: Path, kicad_footprint: str, key_parameters: dict) -> dict | None:
    """Numeric pad-pitch cross-check: footprint .kicad_mod vs distributor pitch.

    Returns None when the check is not applicable (either side missing a usable
    number, or footprint file unresolvable). Otherwise returns a dict with the
    verdict and both measured values. A 'mismatch' verdict (outside tolerance)
    is a fail-worthy signal; 'match' is informational.
    """
    if not isinstance(key_parameters, dict):
        return None
    ds_pitch = _parse_pitch_mm(key_parameters.get("pitch"))
    if ds_pitch is None:
        return None
    mod_path = _resolve_footprint_file(workspace, kicad_footprint)
    if mod_path is None:
        return {
            "verdict": "unverified",
            "reason": f"footprint file for '{kicad_footprint}' not found on disk",
            "datasheet_pitch_mm": ds_pitch,
            "footprint_pitch_mm": None,
        }
    fp_pitch = _footprint_pad_pitch_mm(mod_path)
    if fp_pitch is None:
        return {
            "verdict": "unverified",
            "reason": "could not derive pad pitch from .kicad_mod (<2 pads / parse)",
            "datasheet_pitch_mm": ds_pitch,
            "footprint_pitch_mm": None,
        }
    tol = ds_pitch * PITCH_TOLERANCE_FRAC
    ok = abs(fp_pitch - ds_pitch) <= tol
    return {
        "verdict": "match" if ok else "mismatch",
        "datasheet_pitch_mm": ds_pitch,
        "footprint_pitch_mm": fp_pitch,
        "tolerance_frac": PITCH_TOLERANCE_FRAC,
        "delta_mm": round(abs(fp_pitch - ds_pitch), 4),
    }


def verify_project(project_dir: Path, workspace: Path) -> dict:
    """Scan all evidence JSON for the project; classify each MPN."""
    ev_dir = project_dir / "datasheets" / "component_selecting"
    if not ev_dir.is_dir():
        return {
            "ok": False,
            "reason": f"evidence dir not found: {ev_dir}",
            "items": [],
        }

    results = []
    for json_path in sorted(ev_dir.glob("*.json")):
        try:
            ev = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            results.append({
                "file": json_path.name,
                "verdict": "reject",
                "reason": f"unparseable JSON: {exc}",
            })
            continue
        # Skip longlist files
        if "longlist" in ev or "shortlist" in ev:
            continue
        mpn = ev.get("mpn") or json_path.stem
        role = ev.get("role") or ev.get("role_id") or "?"
        lib_block = ev.get("library") or {}
        raw_status = lib_block.get("status")
        status = _STATUS_LEGACY_ALIASES.get(raw_status, raw_status)
        kicad_symbol = lib_block.get("kicad_symbol") or ""
        kicad_footprint = lib_block.get("kicad_footprint") or ""
        vendor_url = (ev.get("vendor") or {}).get("product_url")
        key_parameters = ev.get("key_parameters") or {}

        item = {
            "file": json_path.name,
            "mpn": mpn,
            "role": role,
            "library_status": status,
            "kicad_symbol": kicad_symbol,
            "kicad_footprint": kicad_footprint,
            "vendor_url": vendor_url,
        }

        # Numeric pad-pitch cross-check (footprint .kicad_mod vs distributor
        # pitch). Only runs when both sides have a usable number; otherwise
        # None (not applicable) or 'unverified' — never a silent pass.
        pitch_check = _check_pitch(workspace, kicad_footprint, key_parameters)
        if pitch_check is not None:
            item["pitch_check"] = pitch_check

        # Structured pinout-verification flag for borrowed / package-compatible
        # footprints. The pad order is NOT guaranteed to belong to this MPN, so
        # a human must verify it against the datasheet. Surfaced explicitly here
        # (and re-surfaced by check_readiness) instead of buried in a status
        # label. We have no reliable per-pad pinout source, so the pin-order
        # state is honestly 'required-but-unverified', never fabricated.
        if status in PINOUT_VERIFY_STATUSES:
            item["pinout_verification_required"] = True
            item["pinout_verification"] = {
                "required": True,
                "pin_order_verified": False,
                "reason": (
                    f"library.status={status} is a borrowed/package-compatible "
                    f"footprint; pad pitch may match but pin order is not "
                    f"guaranteed for MPN {mpn}. Verify pinout against datasheet."
                ),
            }

        if status == "vendored_complete":
            ok = _verify_in_lib_external(workspace, kicad_symbol)
            if ok:
                item["verdict"] = "ok"
                item["reason"] = "in lib_external/components.kicad_sym"
            else:
                item["verdict"] = "reject"
                item["reason"] = (
                    f"library.status=vendored_complete but symbol "
                    f"'{kicad_symbol}' missing from lib_external/components.kicad_sym"
                )
                item["next_action"] = (
                    "evidence claims vendored but lib_external is out of sync — "
                    f"re-run vendor_mpn.py <project> '{mpn}' to recommit, or "
                    "fix kicad_symbol field to match the actual lib_external entry"
                )
        elif status == "passive_generic":
            item["verdict"] = "ok"
            item["reason"] = "generic passive (R/C/L/D); KiCad std Device library covers"
        elif status == "standard_ready":
            ok = _verify_in_kicad_std(workspace, kicad_symbol)
            if ok:
                item["verdict"] = "ok"
                item["reason"] = "exact MPN match in KiCad std lib"
            else:
                item["verdict"] = "reject"
                item["reason"] = (
                    f"library.status=standard_ready but symbol '{kicad_symbol}' "
                    f"not found in lib_cache/sources/kicad-symbols/ — "
                    f"selecting may have written stale cache state"
                )
                item["next_action"] = (
                    "re-run selecting library probe, or change Phase 3 .py to "
                    "reference a verified KiCad std lib symbol"
                )
        elif status in STATUS_NEEDS_LCSC_COMMIT:
            # selecting verified the part is vendorable but did not commit it.
            # preparing must run the commit so the symbol lands in lib_external/
            # (Phase 3 only loads from lib_external + KiCad std).
            ok = _verify_in_lib_external(workspace, kicad_symbol)
            if ok:
                item["verdict"] = "ok"
                item["reason"] = (
                    f"library.status={raw_status} and symbol already committed "
                    f"to lib_external (vendor_mpn.py was run)"
                )
            else:
                item["verdict"] = "reject"
                item["reason"] = (
                    f"library.status={raw_status} — selecting passed the LCSC/"
                    f"easyeda2kicad probe but lib_external/components.* still "
                    f"missing this MPN. Phase 2.5 commit not yet run."
                )
                item["next_action"] = (
                    f"python3 .claude/skills/component-preparing/scripts/"
                    f"vendor_mpn.py <project> '{mpn}'  "
                    "# runs same easyeda2kicad call selecting verified, writes lib_external"
                )
        elif status in STATUS_REJECTED:
            item["verdict"] = "reject"
            item["reason"] = (
                f"library.status={status} — borrowed/substitute lib reuse is "
                f"NOT a substitute for proper vendoring. PCB pad/pin mismatch "
                f"risk is real. Must vendor the actual MPN's KiCad library."
            )
            item["next_action"] = (
                f"agent-browser → DigiKey EDA Models page → download symbol+"
                f"footprint+3D zip → lib_external/incoming/{mpn}.zip → "
                f"import into lib_external/components.*"
            )
        else:
            item["verdict"] = "reject"
            item["reason"] = (
                f"library.status={status!r} — unknown classification, "
                f"vendoring required to confirm symbol/footprint match real MPN."
            )
            item["next_action"] = "vendor real KiCad library for this MPN"

        # Pad-pitch mismatch is a real geometry conflict — it overrides an
        # otherwise-ok verdict to reject (parts won't seat on the pads). An
        # 'unverified' pitch result never changes the verdict (kept advisory).
        pc = item.get("pitch_check")
        if pc and pc.get("verdict") == "mismatch":
            item["verdict"] = "reject"
            item["reason"] = (
                f"footprint pad pitch {pc['footprint_pitch_mm']}mm ≠ datasheet/"
                f"distributor pitch {pc['datasheet_pitch_mm']}mm "
                f"(Δ{pc['delta_mm']}mm, tol ±{int(pc['tolerance_frac']*100)}%) — "
                f"parts will not seat. " + (item.get("reason") or "")
            ).strip()
            item["next_action"] = (
                f"fix the footprint for {mpn} to match the {pc['datasheet_pitch_mm']}mm "
                f"pitch (re-vendor correct footprint or pick a footprint variant)"
            )

        results.append(item)

    # Orphan datasheet check — datasheets/*.pdf must all map to current evidence
    ds_dir = project_dir / "datasheets"
    orphan_pdfs: list[str] = []
    if ds_dir.is_dir():
        evidence_pdfs: set[str] = set()
        for r in results:
            sym = r.get("kicad_symbol") or ""
            mpn = r.get("mpn") or ""
            # Use mpn-derived safe filename(s) to build expected set
            for cand in (mpn, mpn.replace("/", "_").replace(" ", "_")):
                if cand:
                    evidence_pdfs.add(cand)
        # Read evidence JSONs again to get datasheet.path field
        for json_path in sorted(ev_dir.glob("*.json")):
            try:
                ev = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if "longlist" in ev:
                continue
            ds_path = (ev.get("datasheet") or {}).get("path")
            if ds_path:
                evidence_pdfs.add(Path(ds_path).stem)
        for pdf in ds_dir.glob("*.pdf"):
            if pdf.stem not in evidence_pdfs:
                orphan_pdfs.append(pdf.name)

    ok_items = [r for r in results if r["verdict"] == "ok"]
    reject_items = [r for r in results if r["verdict"] == "reject"]

    # Fail-closed when no evidence files exist. Mirrors check_readiness's
    # vacuous guard: an empty component_selecting/ dir means selecting hasn't
    # run for this project (or its output was wiped) — there's nothing to
    # verify, so the gate must not pass.
    return {
        "ok": (
            len(results) > 0
            and len(reject_items) == 0
            and len(orphan_pdfs) == 0
        ),
        "summary": {
            "total": len(results),
            "ok": len(ok_items),
            "reject": len(reject_items),
            "orphan_datasheets": len(orphan_pdfs),
        },
        "items": results,
        "orphan_datasheets": orphan_pdfs,
        "vacuous": len(results) == 0,
    }


def print_report(report: dict) -> None:
    items = report.get("items", [])
    print(f"\n=== verify_vendoring — {len(items)} MPN ===")
    if report.get("vacuous"):
        print("  ⛔ no component_selecting/*.json — selecting hasn't run "
              "(or evidence was wiped). Phase 2.5 gate cannot pass on "
              "empty input.")
    for r in items:
        v = r.get("verdict")
        sym = "✅" if v == "ok" else "❌"
        mpn = _short(r.get("mpn") or "?", 28)
        st = r.get("library_status") or "?"
        reason = _short(r.get("reason") or "", 60)
        print(f"  {sym} {mpn:<28} {st:<22} {reason}")
        pc = r.get("pitch_check")
        if pc:
            pv = pc.get("verdict")
            pg = {"match": "✅", "mismatch": "❌", "unverified": "❓"}.get(pv, "·")
            if pv == "match":
                print(f"     {pg} pitch: footprint {pc['footprint_pitch_mm']}mm "
                      f"≈ distributor {pc['datasheet_pitch_mm']}mm")
            elif pv == "mismatch":
                print(f"     {pg} pitch MISMATCH: footprint {pc['footprint_pitch_mm']}mm "
                      f"≠ distributor {pc['datasheet_pitch_mm']}mm (Δ{pc['delta_mm']}mm)")
            else:
                print(f"     {pg} pitch unverified: {pc.get('reason', '')}")
        if r.get("pinout_verification_required"):
            print(f"     ⚠ pinout 需人工核对（pin order required-but-unverified）: "
                  f"{(r.get('pinout_verification') or {}).get('reason', '')}")
        if v == "reject":
            url = r.get("vendor_url")
            action = r.get("next_action")
            if url:
                print(f"     vendor: {url}")
            if action:
                print(f"     action: {action}")
    s = report.get("summary", {})
    orphans = report.get("orphan_datasheets") or []
    print(f"\nsummary: total={s.get('total')} ok={s.get('ok')} reject={s.get('reject')} orphan_datasheets={len(orphans)}")
    if orphans:
        print("\n❌ Orphan datasheet PDFs (not in current BOM):")
        for f in orphans:
            print(f"  - datasheets/{f}")
        print("\n  → run: python3 .claude/skills/component-preparing/scripts/clean_orphan_datasheets.py <project> --apply")
    if not report.get("ok"):
        print("\n❌ Phase 2.5 vendoring incomplete — fix rejected items above before draw-schematic.")
    else:
        print("\n✅ All MPN library status verified. Phase 2.5 vendoring done.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2.5 vendoring gate")
    parser.add_argument("project_dir", type=Path, help="Projects/<name>")
    parser.add_argument("--json-output", type=Path, help="Optional JSON report path")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(),
                        help="Workspace root (default: cwd)")
    args = parser.parse_args()

    if not args.project_dir.is_dir():
        print(f"error: project_dir not found: {args.project_dir}", file=sys.stderr)
        return 2

    report = verify_project(args.project_dir.resolve(), args.workspace.resolve())
    print_report(report)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
