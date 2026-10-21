#!/usr/bin/env python3
"""Export KiCad fabrication outputs with kicad-cli.

Outputs:
  - Gerber layers
  - Excellon drill files + map/report
  - CSV position file (CPL / pick-and-place)
  - Optional assembly BOM from the matching schematic
  - ZIP archive containing the generated fab files
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

# Shared environment discovery lives at the workspace root.
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
else:
    raise ImportError("pcbenv.py not found above " + __file__)
import pcbenv


DEFAULT_NON_COPPER_LAYERS = [
    "F.Paste",
    "B.Paste",
    "F.Silkscreen",
    "B.Silkscreen",
    "F.Mask",
    "B.Mask",
    "Edge.Cuts",
]


def find_kicad_cli(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if path.exists():
            return str(path)
        raise FileNotFoundError(f"kicad-cli not found: {explicit}")

    found = pcbenv.find_kicad_cli()
    if found:
        return found

    raise FileNotFoundError("kicad-cli not found")


def parse_board_layers(pcb_path: Path) -> list[str]:
    """Return copper layer names from the board file, preserving stack order."""
    text = pcb_path.read_text(encoding="utf-8", errors="ignore")
    layers_match = re.search(r"\(layers\s*(.*?)\n\s*\)", text, re.S)
    if not layers_match:
        return ["F.Cu", "B.Cu"]

    layers: list[str] = []
    for line in layers_match.group(1).splitlines():
        match = re.search(r'\(\s*\d+\s+"([^"]+)"\s+([^)]+)\)', line)
        if not match:
            continue
        name = match.group(1)
        layer_type = match.group(2)
        if name.endswith(".Cu") and any(token in layer_type for token in ("signal", "power")):
            layers.append(name)

    if "F.Cu" not in layers:
        layers.insert(0, "F.Cu")
    if "B.Cu" not in layers:
        layers.append("B.Cu")
    return layers


def find_matching_schematic(pcb_path: Path, explicit: str | None = None) -> Path | None:
    if explicit:
        sch = Path(explicit).expanduser().resolve()
        if not sch.exists():
            raise FileNotFoundError(f"schematic not found: {sch}")
        return sch

    same_stem = pcb_path.with_suffix(".kicad_sch")
    if same_stem.exists():
        return same_stem

    candidates = sorted(pcb_path.parent.glob("*.kicad_sch"))
    if len(candidates) == 1:
        return candidates[0]
    return None


def run(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def require_ok(result: dict) -> None:
    if result["returncode"] != 0:
        cmd = " ".join(result["cmd"])
        raise RuntimeError(
            f"command failed ({result['returncode']}): {cmd}\n"
            f"stdout:\n{result['stdout']}\n\nstderr:\n{result['stderr']}"
        )


def make_zip(src_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path == zip_path or path.is_dir():
                continue
            zf.write(path, path.relative_to(src_dir))


def export_fab(args: argparse.Namespace) -> dict:
    pcb_path = Path(args.pcb).expanduser().resolve()
    if not pcb_path.exists():
        raise FileNotFoundError(f"PCB file not found: {pcb_path}")
    if pcb_path.suffix != ".kicad_pcb":
        raise ValueError(f"expected .kicad_pcb, got: {pcb_path}")

    kicad_cli = find_kicad_cli(args.kicad_cli)
    cli = pcbenv.cli_argv(kicad_cli)  # wrapper commands (flatpak/snap) are multi-word
    schematic = find_matching_schematic(pcb_path, args.schematic)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_output = Path(args.output).expanduser().resolve() if args.output else pcb_path.parent / "fab" / f"release_{pcb_path.stem}_{timestamp}"
    gerber_dir = base_output / "gerbers"
    assembly_dir = base_output / "assembly"
    gerber_dir.mkdir(parents=True, exist_ok=True)
    assembly_dir.mkdir(parents=True, exist_ok=True)

    layers = args.layers.split(",") if args.layers else parse_board_layers(pcb_path) + DEFAULT_NON_COPPER_LAYERS
    layers_arg = ",".join(dict.fromkeys(layer.strip() for layer in layers if layer.strip()))

    results: dict[str, dict | None] = {}

    gerbers_cmd = [
        *cli,
        "pcb",
        "export",
        "gerbers",
        "--output",
        str(gerber_dir),
    ]
    if args.board_plot_params:
        gerbers_cmd.append("--board-plot-params")
    else:
        gerbers_cmd.extend(["--layers", layers_arg])
    gerbers_cmd.extend([
        "--subtract-soldermask",
        "--check-zones",
        str(pcb_path),
    ])
    results["gerbers"] = run(gerbers_cmd)
    require_ok(results["gerbers"])

    drill_report = gerber_dir / f"{pcb_path.stem}-drill_report.txt"
    drill_cmd = [
        *cli,
        "pcb",
        "export",
        "drill",
        "--output",
        str(gerber_dir),
        "--format",
        "excellon",
        "--excellon-units",
        "mm",
        "--generate-map",
        "--map-format",
        "gerberx2",
        "--generate-report",
        "--report-path",
        str(drill_report),
        str(pcb_path),
    ]
    results["drill"] = run(drill_cmd)
    require_ok(results["drill"])

    pos_path = assembly_dir / f"{pcb_path.stem}_positions.csv"
    pos_cmd = [
        *cli,
        "pcb",
        "export",
        "pos",
        "--output",
        str(pos_path),
        "--side",
        "both",
        "--format",
        "csv",
        "--units",
        "mm",
        "--exclude-dnp",
        str(pcb_path),
    ]
    if args.smd_only:
        pos_cmd.insert(-1, "--smd-only")
    results["positions"] = run(pos_cmd)
    require_ok(results["positions"])

    # JLCPCB CPL variant — fab portals expect Mid X/Mid Y/Rotation/Layer columns
    # and reject kicad-cli's Ref/Val/Package/PosX/PosY header.
    try:
        import csv as _csv
        jlc_path = assembly_dir / f"{pcb_path.stem}_jlc_cpl.csv"
        with open(pos_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.reader(f))
        hdr = rows[0]
        col = {name: hdr.index(name) for name in
               ("Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side")}
        with open(jlc_path, "w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            w.writerow(["Designator", "Val", "Package",
                        "Mid X", "Mid Y", "Rotation", "Layer"])
            for r in rows[1:]:
                if not r or not r[col["Ref"]].strip():
                    continue
                side = r[col["Side"]].strip().lower()
                layer = "top" if side in ("top", "front", "f") else "bottom"
                w.writerow([r[col["Ref"]], r[col["Val"]], r[col["Package"]],
                            r[col["PosX"]], r[col["PosY"]], r[col["Rot"]], layer])
        results["positions_jlc"] = {"ok": True, "file": str(jlc_path)}
    except Exception as exc:  # best-effort addon, never block the release
        results["positions_jlc"] = {"ok": False, "error": str(exc)}

    bom_path = None
    if schematic and not args.no_bom:
        bom_path = assembly_dir / f"{pcb_path.stem}_assembly_bom.csv"
        bom_cmd = [
            *cli,
            "sch",
            "export",
            "bom",
            "--output",
            str(bom_path),
            "--fields",
            "Reference,Value,Footprint,QUANTITY,DNP,MPN,LCSC Part #",
            "--labels",
            "Refs,Value,Footprint,Qty,DNP,MPN,LCSC Part #",
            "--group-by",
            "Value,Footprint,MPN,LCSC Part #",
            "--exclude-dnp",
            str(schematic),
        ]
        results["bom"] = run(bom_cmd)
        require_ok(results["bom"])

        # Sync PCB-side DNP into the assembly BOM: netlist-driven schematics
        # carry no dnp flags, so --exclude-dnp above is a no-op. Drop refs the
        # PCB marks DNP so BOM rows stay aligned with the CPL.
        try:
            import csv as _csv
            import re as _re
            pcb_text = pcb_path.read_text(errors="replace", encoding="utf-8")
            dnp_refs = set()
            # anchor on each dnp attr, then backtrack to its own footprint's
            # Reference property (non-greedy block regex would span neighbors)
            for am in _re.finditer(r'\(attr\b[^)]*\bdnp\b[^)]*\)', pcb_text):
                back = pcb_text.rfind('(footprint ', 0, am.start())
                if back < 0:
                    continue
                rm = _re.search(
                    r'\(property\s+"Reference"\s+"([^"]+)"',
                    pcb_text[back:am.start()])
                if rm:
                    dnp_refs.add(rm.group(1))
            if dnp_refs:
                def _expand(cell):
                    refs = []
                    for tok in (x.strip() for x in cell.split(",")):
                        if not tok:
                            continue
                        rm = _re.fullmatch(r'([A-Za-z]+)(\d+)-([A-Za-z]+)?(\d+)', tok)
                        if rm and (rm.group(3) or rm.group(1)) == rm.group(1):
                            p = rm.group(1)
                            refs += [f"{p}{n}" for n in range(int(rm.group(2)), int(rm.group(4)) + 1)]
                        else:
                            refs.append(tok)
                    return refs
                with open(bom_path, newline="", encoding="utf-8") as f:
                    rows = list(_csv.reader(f))
                hdr = rows[0]
                i_refs, i_qty = hdr.index("Refs"), hdr.index("Qty")
                out = [hdr]
                dropped = []
                for r in rows[1:]:
                    refs = _expand(r[i_refs])
                    hit = [x for x in refs if x in dnp_refs]
                    if not hit:
                        out.append(r)  # untouched — keep kicad's range notation+qty
                        continue
                    dropped += hit
                    keep = [x for x in refs if x not in dnp_refs]
                    if not keep:
                        continue
                    r[i_refs] = ",".join(keep)
                    r[i_qty] = str(len(keep))
                    out.append(r)
                with open(bom_path, "w", newline="", encoding="utf-8") as f:
                    _csv.writer(f).writerows(out)
                results["bom_dnp_synced"] = {"ok": True, "dropped": dropped}
        except Exception as exc:  # best-effort; never block the release
            results["bom_dnp_synced"] = {"ok": False, "error": str(exc)}
    else:
        results["bom"] = None

    manifest = {
        "ok": True,
        "pcb": str(pcb_path),
        "schematic": str(schematic) if schematic else None,
        "kicad_cli": kicad_cli,
        "output_dir": str(base_output),
        "gerber_dir": str(gerber_dir),
        "assembly_dir": str(assembly_dir),
        "layers": layers_arg.split(","),
        "positions_csv": str(pos_path),
        "assembly_bom_csv": str(bom_path) if bom_path else None,
        "commands": results,
    }
    zip_path = base_output / f"{pcb_path.stem}_fab.zip"
    manifest["zip"] = str(zip_path)
    manifest_path = base_output / "fab_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    make_zip(base_output, zip_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export KiCad Gerber/Drill/CPL/BOM fab files.")
    parser.add_argument("pcb", help="Path to .kicad_pcb")
    parser.add_argument("-o", "--output", help="Output release directory. Default: <pcb_dir>/fab/release_<stem>_<timestamp>")
    parser.add_argument("--schematic", help="Optional matching .kicad_sch for assembly BOM export")
    parser.add_argument("--layers", help="Comma-separated Gerber layers. Default: all copper + paste/silk/mask/Edge.Cuts")
    parser.add_argument("--kicad-cli", help="Explicit kicad-cli path")
    parser.add_argument("--board-plot-params", action="store_true", help="Use plot settings stored in the board file")
    parser.add_argument("--smd-only", action="store_true", default=False, help="Opt-in: export only SMD footprints (use only when fab does SMT-only and refuses THT lines)")
    parser.add_argument("--all-pos", action="store_true", help="Deprecated no-op; CPL now includes THT by default. Kept for backward compat.")
    parser.add_argument("--no-bom", action="store_true", help="Skip schematic BOM export")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        manifest = export_fab(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
