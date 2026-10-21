#!/usr/bin/env python3
"""accept_shortlist.py — convert component-selecting shortlist into per-MPN evidence.

component-selecting writes shortlists to
    Projects/<name>/_artifacts/component_selecting/<role>_shortlist.json
but check_readiness.py reads per-MPN evidence at
    Projects/<name>/datasheets/component_selecting/<safe_mpn>.json
This script bridges the two: pick a top candidate from a shortlist (or all
passing candidates) and write evidence files in a schema both phase2.5_v1
consumers (verify_vendoring, import_incoming_zip, coverage_scan) and
check_readiness.py (top-level verdict, vendor.status, vendor.url) understand.

This script is intentionally non-interactive — the user confirms the top
pick to component-preparing in chat, then runs this script with --mpn or
--accept-top to commit. It does not query distributors (component-selecting
already did) and does not vendor libraries (bulk_fetch_datasheets + import
do that next). All it does is write the evidence JSON contract.

Usage:
    accept_shortlist.py --shortlist <path>            --list
    accept_shortlist.py --shortlist <path>            --accept-top
    accept_shortlist.py --shortlist <path> --mpn <M>
    accept_shortlist.py --shortlist <path>            --all-pass
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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Must stay in sync with
# .claude/skills/component-preparing/scripts/check_readiness.py::_safe_mpn_for_evidence
def _safe_mpn(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", mpn or "")


_OK_VERDICTS = {"pass", "warn_single_source"}
_JP_LOCAL_VENDOR_IDS = {"digikey_jp", "mouser_jp", "akizuki", "marutsu", "chip1stop", "rs_jp"}


def _pick_primary_vendor(vendor_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the vendor row that backs the buyable verdict.

    Preference order:
      1. JP-local vendor with status=active (lowest local price first)
      2. any active vendor (LCSC fallback)
      3. nrnd vendor
    """
    active = [v for v in vendor_results if (v.get("status") or "").lower() == "active"]
    nrnd = [v for v in vendor_results if (v.get("status") or "").lower() == "nrnd"]

    def _price_key(v: dict[str, Any]) -> float:
        p = v.get("price")
        return float(p) if isinstance(p, (int, float)) else float("inf")

    jp_active = [v for v in active if v.get("vendor_id") in _JP_LOCAL_VENDOR_IDS]
    jp_active.sort(key=_price_key)
    if jp_active:
        return jp_active[0]
    if active:
        active.sort(key=_price_key)
        return active[0]
    if nrnd:
        return nrnd[0]
    return None


def _ds_dir_for_shortlist(shortlist_path: Path) -> Path:
    """Map _artifacts/component_selecting/<role>_shortlist.json
    to datasheets/component_selecting/."""
    cs = shortlist_path.parent
    if cs.name != "component_selecting" or cs.parent.name != "_artifacts":
        sys.exit(
            f"❌ shortlist path must be Projects/<name>/_artifacts/component_selecting/"
            f"<role>_shortlist.json (got {shortlist_path})"
        )
    project = cs.parent.parent
    return project / "datasheets" / "component_selecting"


def _build_evidence(result: dict[str, Any], shortlist_meta: dict[str, Any]) -> dict[str, Any]:
    """Translate one shortlist result → evidence JSON.

    Writes both phase2.5_v1 legacy fields (vendor.active / vendor.product_url /
    vendor.price_jpy / verdict_source) AND check_readiness.py gate fields
    (top-level verdict, vendor.status, vendor.url, vendor.price). One JSON,
    two contracts — keeps existing readers happy without a migration step.
    """
    mpn = result.get("mpn") or ""
    role = result.get("expected_role")
    vendor_results = result.get("vendor_results") or []
    primary = _pick_primary_vendor(vendor_results) or {}
    library = result.get("library") or {}

    locale = result.get("locale") or shortlist_meta.get("locale") or "日本"
    currency = result.get("currency") or shortlist_meta.get("currency") or "JPY"

    product_url = (
        primary.get("final_url")
        or primary.get("url")
        or (result.get("product_urls") or {}).get(primary.get("vendor_id") or "")
    )
    datasheet_url = (
        primary.get("datasheet_url")
        or (result.get("datasheet_urls") or {}).get(primary.get("vendor_id") or "")
        or next(iter((result.get("datasheet_urls") or {}).values()), None)
    )

    vendor_block = {
        "primary": primary.get("vendor_id"),
        # legacy phase2.5_v1 fields
        "active": (primary.get("status") == "active"),
        "product_url": product_url,
        "price_jpy": primary.get("price") if currency == "JPY" else None,
        # check_readiness.py contract fields
        "status": primary.get("status"),
        "url": product_url,
        "price": primary.get("price"),
        # shared
        "stock": primary.get("stock"),
        "currency": currency,
        "locale": locale,
    }

    library_block = {
        "status": library.get("status"),
        "kicad_symbol": library.get("kicad_symbol"),
        "kicad_footprint": library.get("kicad_footprint"),
    }

    datasheet_block = {
        # bulk_fetch_datasheets.py will overwrite this once the PDF lands on disk
        "status": "pending",
        "path": None,
        "sha256_short": None,
        "vendor_url_for_manual_fetch": datasheet_url,
    }

    return {
        "schema_version": "phase2.5_v1",
        "role_id": role,
        "role": role,
        "mpn": mpn,
        "mpn_lookup": mpn,
        "manufacturer": (result.get("input") or {}).get("manufacturer"),
        # check_readiness.py looks here:
        "verdict": result.get("verdict"),
        "reason": result.get("reason"),
        # legacy phase2.5_v1 marker (verify_vendoring etc. read this):
        "verdict_source": "component-preparing/accept_shortlist.py",
        "locked_at": datetime.now(timezone.utc).date().isoformat(),
        "vendor": vendor_block,
        "library": library_block,
        "datasheet": datasheet_block,
        "key_parameters": result.get("key_parameters"),
        "shortlist_origin": {
            "schema": shortlist_meta.get("schema"),
            "evaluated_at": shortlist_meta.get("evaluated_at"),
            "rank": result.get("rank"),
        },
    }


def _select_results(
    shortlist: dict[str, Any], mode: str, mpn: str | None
) -> list[dict[str, Any]]:
    results = shortlist.get("results") or []
    passing = [
        r for r in results
        if (r.get("verdict") or "").lower() in _OK_VERDICTS
    ]
    if not passing:
        sys.exit(
            "❌ shortlist has no candidate with verdict pass/warn_single_source — "
            "回去重跑 component-selecting 或换型号"
        )

    if mode == "mpn":
        if not mpn:
            sys.exit("❌ --mpn required when mode=mpn")
        match = [r for r in passing if (r.get("mpn") or "") == mpn]
        if not match:
            sys.exit(
                f"❌ MPN {mpn!r} not in shortlist (or its verdict is fail). "
                f"passing MPNs: {[r.get('mpn') for r in passing]}"
            )
        return match

    if mode == "top":
        passing_sorted = sorted(passing, key=lambda r: r.get("rank", 999))
        return [passing_sorted[0]]

    if mode == "all":
        return passing

    sys.exit(f"❌ unknown selection mode: {mode}")


def _print_listing(shortlist: dict[str, Any]) -> None:
    results = shortlist.get("results") or []
    print(f"shortlist: schema={shortlist.get('schema')} locale={shortlist.get('locale')}")
    print(f"  total candidates: {len(results)}")
    print(f"  {'rank':>4}  {'verdict':<22}  {'price':>9}  {'stock':>7}  mpn")
    for r in sorted(results, key=lambda x: x.get("rank", 999)):
        verdict = (r.get("verdict") or "").lower()
        price = r.get("local_price")
        stock = r.get("local_stock")
        mark = "✓" if verdict in _OK_VERDICTS else " "
        print(
            f"  {r.get('rank', '?'):>4}{mark} {verdict:<22}  "
            f"{(f'{price:.2f}' if isinstance(price, (int, float)) else '-'):>9}  "
            f"{(str(stock) if stock is not None else '-'):>7}  {r.get('mpn')}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shortlist", required=True, type=Path,
                    help="Projects/<name>/_artifacts/component_selecting/<role>_shortlist.json")
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument("--list", action="store_true",
                     help="print candidates (with verdict / price / stock) and exit")
    sel.add_argument("--accept-top", action="store_true",
                     help="write evidence for the top-ranked pass/warn candidate")
    sel.add_argument("--mpn", help="write evidence for a specific MPN in the shortlist")
    sel.add_argument("--all-pass", action="store_true",
                     help="write evidence for every pass/warn candidate "
                          "(rare; only when the shortlist is curated 1-MPN-per-role)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be written, no disk writes")
    args = ap.parse_args()

    if not args.shortlist.exists():
        sys.exit(f"❌ shortlist not found: {args.shortlist}")
    try:
        shortlist = json.loads(args.shortlist.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"❌ shortlist is not valid JSON: {exc}")

    if args.list or not (args.accept_top or args.mpn or args.all_pass):
        _print_listing(shortlist)
        if not args.list:
            print("\n→ pass --accept-top / --mpn <M> / --all-pass to commit evidence")
        return 0

    if args.accept_top:
        mode = "top"
    elif args.all_pass:
        mode = "all"
    else:
        mode = "mpn"

    selected = _select_results(shortlist, mode, args.mpn)
    ds_dir = _ds_dir_for_shortlist(args.shortlist)

    written: list[Path] = []
    for r in selected:
        evidence = _build_evidence(r, shortlist)
        out_path = ds_dir / f"{_safe_mpn(r.get('mpn') or '')}.json"
        if args.dry_run:
            print(f"[DRY-RUN] would write {out_path}")
            print(json.dumps(evidence, indent=2, ensure_ascii=False))
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        written.append(out_path)

    if args.dry_run:
        print(f"\n[DRY-RUN] mode={mode} selected={len(selected)} target_dir={ds_dir}")
        return 0

    print(f"✓ wrote {len(written)} evidence file(s) to {ds_dir}")
    for p in written:
        print(f"  - {p.name}")
    print("\nNext: run scripts/bulk_fetch_datasheets.py + import zip + verify_vendoring.py,")
    print("      then check_readiness.py to write the BOM gate sentinel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
