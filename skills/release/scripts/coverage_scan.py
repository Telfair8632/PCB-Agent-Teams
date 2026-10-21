"""Aggregate per-MPN vendor coverage for release ORDER_GUIDE.

Two data sources, in priority order:

1. **All-lane truth** — `<project>/_artifacts/component_selecting/*_shortlist.json`
   has per-MPN `vendor_results` for every probed lane (DK_JP + Mouser_JP + LCSC).
   This is what we report as coverage when present, because it answers the real
   question "does vendor X have this part in stock right now?".

2. **Primary-only fallback** — `<project>/datasheets/component_selecting/*.json`
   keeps only the chosen winning lane (`vendor.primary` from
   `accept_shortlist.py`). Coverage from this source labels other vendors
   "no evidence" — that's accurate but misleads humans into thinking the
   vendor was never queried.

If artifacts are missing (e.g., legacy projects), fall back to primary-only and
mark `data_source = "primary-only"` so callers can warn the user.

Reads only files where the filename stem matches the evidence's mpn (longlists
and `_pending_*` are skipped).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

VENDORS = ("digikey_jp", "mouser_jp", "lcsc")


def _is_locked_evidence_file(path: Path) -> bool:
    if path.suffix != ".json":
        return False
    stem = path.stem
    if stem.startswith("_"):
        return False
    if stem.endswith("_longlist"):
        return False
    return True


def _load_evidence(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("mpn") != path.stem:
        return None
    return data


def _load_artifact_lanes(artifacts_dir: Path) -> dict[str, dict[str, dict]]:
    """Walk `*_shortlist.json` and build {mpn: {vendor_id: vendor_result}}.

    On duplicate (multiple shortlists touched the same MPN), keeps the most
    recently fetched probe per (mpn, vendor_id).
    """
    by_mpn: dict[str, dict[str, dict]] = {}
    if not artifacts_dir.exists():
        return by_mpn
    for path in sorted(artifacts_dir.glob("*_shortlist.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for r in data.get("results") or []:
            if not isinstance(r, dict):
                continue
            mpn = r.get("mpn")
            if not mpn:
                continue
            for vr in r.get("vendor_results") or []:
                if not isinstance(vr, dict):
                    continue
                vid = vr.get("vendor_id")
                if vid not in VENDORS:
                    continue
                slot = by_mpn.setdefault(mpn, {})
                prev = slot.get(vid)
                if prev is None or (vr.get("fetched_at") or "") >= (prev.get("fetched_at") or ""):
                    slot[vid] = vr
    return by_mpn


def _row_from_lane(lane: dict, primary: bool) -> dict:
    return {
        "active": True,
        "stock": lane.get("stock"),
        "price": lane.get("price"),
        "currency": lane.get("currency"),
        # Backwards-compat: templates still read price_jpy. JP lanes have it
        # (fall back to price), LCSC stays None (CNY needs FX, not done here).
        "price_jpy": lane.get("price_jpy") or (
            lane.get("price") if (lane.get("currency") or "").upper() == "JPY" else None
        ),
        "url": lane.get("final_url") or lane.get("url"),
        "is_primary": primary,
    }


def scan_coverage(
    component_selecting_dir: Path,
    artifacts_dir: Optional[Path] = None,
) -> dict:
    matrix: list[dict] = []
    totals = {v: 0 for v in VENDORS}

    if not component_selecting_dir.exists():
        return {
            "matrix": [],
            "totals": totals,
            "n_unique_mpn": 0,
            "single_vendor_coverage": {v: "0/0" for v in VENDORS},
            "recommended_paths": [],
            "data_source": "missing",
        }

    # Auto-derive artifacts_dir from convention if not given:
    # <proj>/datasheets/component_selecting/  →  <proj>/_artifacts/component_selecting/
    if artifacts_dir is None:
        proj_dir = component_selecting_dir.parent.parent
        artifacts_dir = proj_dir / "_artifacts" / "component_selecting"

    lanes_by_mpn = _load_artifact_lanes(artifacts_dir)
    data_source = "all-lane" if lanes_by_mpn else "primary-only"

    for path in sorted(component_selecting_dir.glob("*.json")):
        if not _is_locked_evidence_file(path):
            continue
        data = _load_evidence(path)
        if data is None:
            continue

        mpn = data["mpn"]
        vendor = data.get("vendor", {}) or {}
        primary = vendor.get("primary")
        primary_active = bool(vendor.get("active"))
        lanes = lanes_by_mpn.get(mpn, {})

        row: dict = {
            "mpn": mpn,
            "qty": data.get("qty_per_board", 1),
            "refs": data.get("designators", ""),
        }
        for v in VENDORS:
            lane = lanes.get(v)
            if lane and (lane.get("status") or "").lower() == "active":
                row[v] = _row_from_lane(lane, primary=(primary == v and primary_active))
                totals[v] += 1
            elif lane:
                # Lane probed but inactive (NRND / out of stock / not found)
                reason = lane.get("reason") or lane.get("status") or "inactive"
                row[v] = {"active": False, "note": f"probed but inactive: {reason}"}
            elif primary == v and primary_active:
                # No artifact data, fall back to primary evidence (legacy projects)
                row[v] = {
                    "active": True,
                    "stock": vendor.get("stock"),
                    "price_jpy": vendor.get("price_jpy"),
                    "price": vendor.get("price") or vendor.get("price_jpy"),
                    "currency": vendor.get("currency", "JPY"),
                    "url": vendor.get("product_url"),
                    "is_primary": True,
                }
                totals[v] += 1
            else:
                row[v] = {"active": False, "note": "no evidence"}
        matrix.append(row)

    n = len(matrix)
    coverage = {v: f"{totals[v]}/{n}" for v in VENDORS}
    recommended = [v for v in VENDORS if n > 0 and totals[v] == n]

    return {
        "matrix": matrix,
        "totals": totals,
        "n_unique_mpn": n,
        "single_vendor_coverage": coverage,
        "recommended_paths": recommended,
        "data_source": data_source,
    }
