#!/usr/bin/env python3
"""import_incoming_zip — 把一个 DigiKey EDA Models / SnapEDA / Ultra Librarian
导出的 KiCad zip 合并进 lib_external/components.{kicad_sym, pretty, 3dshapes}。

输入：lib_external/incoming/<MPN>.zip （手工或自动抓的，不影响导入逻辑）
输出：
  - lib_external/components.kicad_sym 追加 (symbol "<name>" ...) 块（幂等：同名替换）
  - lib_external/components.pretty/<name>.kicad_mod
  - lib_external/components.3dshapes/<name>.step / .wrl
  - 项目 evidence JSON 更新：library.status=vendored_complete + kicad_symbol/footprint 路径
  - lib_external/README.md 追加一行元件清单

zip 结构兼容（自适应）：
  • SnapEDA: <MPN>/KiCad/<MPN>.kicad_sym + <MPN>.kicad_mod + <MPN>.step
  • UltraLibrarian: <MPN>/KiCad/library/<MPN>.kicad_sym + ... 不同嵌套
  • flat: 直接 <MPN>.kicad_sym + .kicad_mod + .step

简单粗暴：递归扫 zip 里所有 *.kicad_sym / *.kicad_mod / *.step / *.wrl 文件，
按扩展名分流。多个 .kicad_sym 时合并所有 (symbol ...) 块。

用法：
  python3 import_incoming_zip.py \\
      --zip lib_external/incoming/TMA-0505S.zip \\
      --mpn "TMA 0505S" \\
      --project Projects/&lt;your_project&gt;

退出码：0=ok / 2=zip 不存在 / 3=zip 里没有 kicad 资产 / 4=symbol 解析失败
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
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


# Shared workspace discovery: pcbenv at the repo root.
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
import pcbenv

WORKSPACE = pcbenv.workspace_root()
LIB_EXTERNAL = WORKSPACE / "lib_external"
COMPONENTS_SYM = LIB_EXTERNAL / "components.kicad_sym"
COMPONENTS_PRETTY = LIB_EXTERNAL / "components.pretty"
COMPONENTS_3D = LIB_EXTERNAL / "components.3dshapes"
README = LIB_EXTERNAL / "README.md"


def safe_name(mpn: str) -> str:
    """Filename-safe MPN."""
    return re.sub(r"_+", "_", re.sub(r"[/\\:*?\"<>|,;\s]", "_", mpn)).strip("_")


def extract_zip(zip_path: Path) -> Path:
    """Extract zip to a temp dir and return that dir."""
    tmp = Path(tempfile.mkdtemp(prefix="vendor_zip_"))
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp)
    return tmp


def find_assets(extract_dir: Path) -> dict[str, list[Path]]:
    """Recursively find all KiCad assets in extracted zip."""
    return {
        "sym": list(extract_dir.rglob("*.kicad_sym")),
        "fp": list(extract_dir.rglob("*.kicad_mod")),
        "step": list(extract_dir.rglob("*.step")) + list(extract_dir.rglob("*.STEP")),
        "wrl": list(extract_dir.rglob("*.wrl")),
    }


def parse_symbols_from_lib(sym_text: str) -> list[tuple[str, str]]:
    """Extract (name, full_block_text) for each top-level (symbol "...") in a
    .kicad_sym file. Supports KiCad 6/7/8/9/10 format.

    Top-level symbols are at indent depth 1 inside (kicad_symbol_lib ...).
    Nested symbols (sub-symbols like "<name>_0_1") are inside the parent and
    must NOT be treated as top-level.
    """
    out = []
    # Find each top-level (symbol "<name>" by tracking paren depth.
    # Simple state machine: when we see "(symbol " at the right depth, capture
    # name + balanced block.
    depth = 0
    i = 0
    n = len(sym_text)
    while i < n:
        c = sym_text[i]
        if c == "(":
            # Check if this is a top-level "(symbol "
            if depth == 1 and sym_text[i:i+8] == "(symbol ":
                # Find name
                m = re.match(r'\(symbol\s+"([^"]+)"', sym_text[i:])
                if m:
                    name = m.group(1)
                    # Capture balanced block
                    block_start = i
                    block_depth = 0
                    j = i
                    while j < n:
                        if sym_text[j] == "(":
                            block_depth += 1
                        elif sym_text[j] == ")":
                            block_depth -= 1
                            if block_depth == 0:
                                block_end = j + 1
                                out.append((name, sym_text[block_start:block_end]))
                                i = block_end
                                break
                        j += 1
                    continue
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return out


def merge_symbols(new_blocks: list[tuple[str, str]]) -> list[str]:
    """Merge new (name, block) pairs into COMPONENTS_SYM. Idempotent: same name → replace.
    Returns list of names actually written."""
    if not COMPONENTS_SYM.exists():
        # Fresh file
        header = '(kicad_symbol_lib\n  (version 20231120)\n  (generator "circuit-design.import_incoming_zip")\n'
        body = "\n".join("  " + b for _, b in new_blocks)
        COMPONENTS_SYM.write_text(f"{header}{body}\n)\n", encoding="utf-8")
        return [n for n, _ in new_blocks]

    text = COMPONENTS_SYM.read_text(encoding="utf-8")
    written = []
    for name, block in new_blocks:
        # Try to remove existing block with same name (idempotent replace)
        pattern = re.compile(
            r"(?:\n)?\s*\(symbol\s+\"" + re.escape(name) + r"\"\b.*?(?=\n\s*\(symbol\s+\"|\n\)\s*$)",
            re.DOTALL,
        )
        text_old = text
        text = pattern.sub("", text, count=1)
        if text != text_old:
            print(f"  ↻ replacing existing symbol '{name}'", file=sys.stderr)
        # Insert before final ')'
        # Find last ')' (closing kicad_symbol_lib)
        close_idx = text.rstrip().rfind(")")
        if close_idx < 0:
            print(f"error: malformed components.kicad_sym (no closing paren)", file=sys.stderr)
            return written
        indented = "\n".join("  " + line if line.strip() else line for line in block.split("\n"))
        text = text[:close_idx] + indented + "\n" + text[close_idx:]
        written.append(name)
    COMPONENTS_SYM.write_text(text, encoding="utf-8")
    return written


def copy_footprints(fp_paths: list[Path], target_basename: str) -> list[str]:
    """Copy .kicad_mod files into components.pretty/. Use original filename if
    multiple, target_basename for the first/only one."""
    COMPONENTS_PRETTY.mkdir(parents=True, exist_ok=True)
    written = []
    for src in fp_paths:
        # Keep original filename to preserve internal (footprint "...") name
        dst = COMPONENTS_PRETTY / src.name
        shutil.copy2(src, dst)
        written.append(src.stem)
    return written


def copy_3dmodels(model_paths: list[Path]) -> list[str]:
    """Copy 3D model files."""
    if not model_paths:
        return []
    COMPONENTS_3D.mkdir(parents=True, exist_ok=True)
    written = []
    for src in model_paths:
        dst = COMPONENTS_3D / src.name
        shutil.copy2(src, dst)
        written.append(src.name)
    return written


def update_evidence(project: Path, mpn: str, sym_names: list[str], fp_names: list[str]) -> bool:
    """Update Projects/<name>/datasheets/component_selecting/<safe_mpn>.json:
    library.status=vendored_complete + kicad_symbol/footprint paths."""
    ev_dir = project / "datasheets" / "component_selecting"
    candidate_files = [
        ev_dir / f"{safe_name(mpn)}.json",
        # Fallback: try MPN with different sanitization
        ev_dir / f"{mpn.replace(' ', '_').replace('/', '_')}.json",
    ]
    ev_path = next((p for p in candidate_files if p.exists()), None)
    if ev_path is None:
        print(f"warn: no evidence JSON found for MPN={mpn} in {ev_dir}", file=sys.stderr)
        return False

    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    lib_block = ev.setdefault("library", {})
    lib_block["status"] = "vendored_complete"
    if sym_names:
        lib_block["kicad_symbol"] = f"components:{sym_names[0]}"
    if fp_names:
        lib_block["kicad_footprint"] = f"components:{fp_names[0]}"
    lib_block["vendored_at"] = "imported_via_import_incoming_zip.py"

    ev_path.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✓ updated evidence: {ev_path}", file=sys.stderr)
    return True


def update_readme(mpn: str, sym_names: list[str], fp_names: list[str]) -> None:
    """Append a line to lib_external/README.md noting this MPN."""
    if not README.exists():
        return
    line = f"\n- **{mpn}** — symbol={sym_names[0] if sym_names else 'n/a'}, footprint={fp_names[0] if fp_names else 'n/a'}"
    text = README.read_text(encoding="utf-8")
    if mpn not in text:
        README.write_text(text.rstrip() + line + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import DK EDA Models zip into lib_external")
    parser.add_argument("--zip", required=True, type=Path, help="Path to <MPN>.zip")
    parser.add_argument("--mpn", required=True, help="Manufacturer part number")
    parser.add_argument("--project", type=Path, help="Projects/<name> for evidence JSON update")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, no writes")
    args = parser.parse_args()

    if not args.zip.exists():
        print(f"error: zip not found: {args.zip}", file=sys.stderr)
        return 2

    print(f"=== import_incoming_zip ===")
    print(f"  zip:     {args.zip}")
    print(f"  mpn:     {args.mpn}")
    print(f"  project: {args.project}")

    extract_dir = extract_zip(args.zip)
    print(f"  extracted to: {extract_dir}")

    assets = find_assets(extract_dir)
    print(f"  found: sym={len(assets['sym'])} fp={len(assets['fp'])} "
          f"step={len(assets['step'])} wrl={len(assets['wrl'])}")

    if not assets["sym"] and not assets["fp"]:
        print(f"error: zip has no .kicad_sym or .kicad_mod files", file=sys.stderr)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return 3

    # Parse all symbols
    all_symbols: list[tuple[str, str]] = []
    for sym_file in assets["sym"]:
        try:
            text = sym_file.read_text(encoding="utf-8")
            blocks = parse_symbols_from_lib(text)
            all_symbols.extend(blocks)
            print(f"  parsed {sym_file.name}: {[n for n, _ in blocks]}")
        except Exception as exc:
            print(f"warn: failed to parse {sym_file}: {exc}", file=sys.stderr)

    if args.dry_run:
        print(f"\n[DRY-RUN] would write:")
        print(f"  symbols:    {[n for n, _ in all_symbols]}")
        print(f"  footprints: {[fp.stem for fp in assets['fp']]}")
        print(f"  3d models:  {[m.name for m in assets['step'] + assets['wrl']]}")
        shutil.rmtree(extract_dir, ignore_errors=True)
        return 0

    # Write
    sym_names = merge_symbols(all_symbols)
    fp_names = copy_footprints(assets["fp"], safe_name(args.mpn))
    model_names = copy_3dmodels(assets["step"] + assets["wrl"])

    print(f"\n=== summary ===")
    print(f"  symbols:    {sym_names}")
    print(f"  footprints: {fp_names}")
    print(f"  3d models:  {model_names}")

    if args.project:
        update_evidence(args.project, args.mpn, sym_names, fp_names)
    update_readme(args.mpn, sym_names, fp_names)

    shutil.rmtree(extract_dir, ignore_errors=True)
    print("✅ vendoring complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
