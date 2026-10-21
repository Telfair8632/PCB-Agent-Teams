"""Pure CSV transformers: 采购 BOM → distributor upload formats.

Source CSV (from bom-readiness) columns:
  Qty, Refs, MPN, Category, Footprint, Vendor_Status, Vendor_Stock, Vendor_Url, Datasheet

Targets: DigiKey BOM Manager, Mouser BOM Tool, LCSC BOM.
Format spec: ../references/distributor_csv_formats.md
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable


def _read_bom(csv_path: Path) -> list[dict[str, str]]:
    # utf-8-sig: tolerates a BOM on read and keeps Excel happy with Chinese
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def transform_to_digikey(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "Manufacturer Part Number": r["MPN"],
            "Quantity": r["Qty"],
            "Customer Reference": r["Refs"],
        }
        for r in rows
    ]


def transform_to_mouser(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "Mfr Part Number": r["MPN"],
            "Quantity": r["Qty"],
            "Description": f'{r["Category"]} / {r["Footprint"]}',
        }
        for r in rows
    ]


def transform_to_lcsc(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "Comment": r["MPN"],
            "Designator": r["Refs"],
            "Footprint": r["Footprint"],
            "LCSC Part #": "",
            "Manufacture Part Number": r["MPN"],
            "Quantity": r["Qty"],
        }
        for r in rows
    ]


def write_distributor_csvs(
    procurement_bom_csv: Path,
    out_dir: Path,
) -> dict[str, Path]:
    """Read 采购 BOM, write 3 distributor CSVs into out_dir, return paths."""
    rows = _read_bom(procurement_bom_csv)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, fn in [
        ("digikey_bulk.csv", transform_to_digikey),
        ("mouser_bom.csv", transform_to_mouser),
        ("lcsc_bom.csv", transform_to_lcsc),
    ]:
        target = fn(rows)
        path = out_dir / name
        fieldnames = list(target[0].keys()) if target else []
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(target)
        written[name] = path
    return written
