#!/usr/bin/env python3
"""Internal LCSC/EasyEDA library fetch implementation for component-selecting.

Direct write mode is intentionally guarded. component-selecting is the only
selection/vendoring gate; this script may be called directly only with
--verify-only, or by component-selecting with COMPONENT_SELECTING_LCSC_WRITE=1.

Probe-only:
    python download_lcsc_lib.py --verify-only <MPN1> <MPN2> ...

依赖: easyeda2kicad（已装在 PCB-Agent-Teams/.venv/）
不需要 API key。
"""

# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

# Workspace root via shared pcbenv (KICAD_ROOT env override honored there).
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
import pcbenv

KICAD_ROOT = pcbenv.workspace_root()
LIB_EXTERNAL = KICAD_ROOT / "lib_external"
_venv_bin = "Scripts" if os.name == "nt" else "bin"
_venv_ext = ".exe" if os.name == "nt" else ""
EASYEDA2KICAD = KICAD_ROOT / ".venv" / _venv_bin / ("easyeda2kicad" + _venv_ext)
WRITE_GUARD_ENV = "COMPONENT_SELECTING_LCSC_WRITE"


def query_lcsc_id(mpn: str) -> str | None:
    """用 Jina Reader 代理读 LCSC 搜索页，提取真实 LCSC C 编号。"""
    # Input already IS an LCSC C-code — skip network resolution entirely.
    if re.fullmatch(r"[Cc]\d{4,10}", mpn or ""):
        return mpn.upper()
    url = f"https://r.jina.ai/https://www.lcsc.com/search?q={mpn}"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  ⚠ 查 {mpn} 失败: {e}")
        return None

    # 真实 LCSC ID 在 product-detail URL 里
    m = re.search(r"/product-detail/(C\d+)\.html", text)
    return m.group(1) if m else None


def download_one(lcsc_id: str, lib_name: str) -> bool:
    """用 easyeda2kicad 下载 symbol+footprint+3D。"""
    out = LIB_EXTERNAL / lib_name
    cmd = [
        str(EASYEDA2KICAD),
        "--lcsc_id", lcsc_id,
        "--full",
        "--output", str(out),
        "--overwrite",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    return "Created 3D model" in result.stdout or result.returncode == 0


def verify_one(mpn: str) -> dict:
    """Probe LCSC EasyEDA library availability WITHOUT writing to lib_external.

    Used by component-selecting parametric_discover for library readiness
    pre-filtering (locale=中国大陆 strategy = lcsc_easyeda).

    Returns:
      {
        "ok": bool,            # True iff has_symbol AND has_footprint
        "mpn": str,
        "lcsc_id": str | None,
        "has_symbol": bool,
        "has_footprint": bool,
        "has_3d": bool,
        "partial_reason": str | None,
        "error": str | None,
      }
    """
    import tempfile

    result = {
        "ok": False, "mpn": mpn, "lcsc_id": None,
        "has_symbol": False, "has_footprint": False, "has_3d": False,
        "partial_reason": None, "error": None,
    }
    lcsc_id = query_lcsc_id(mpn)
    if not lcsc_id:
        result["error"] = "no_lcsc_id"
        return result
    result["lcsc_id"] = lcsc_id

    with tempfile.TemporaryDirectory() as td:
        probe_out = Path(td) / "probe"
        cmd = [
            str(EASYEDA2KICAD),
            "--lcsc_id", lcsc_id,
            "--full",
            "--output", str(probe_out),
            "--overwrite",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        except Exception as e:
            result["error"] = f"easyeda2kicad exception: {type(e).__name__}: {e}"
            return result

        # Inspect outputs (easyeda2kicad writes to <output>.kicad_sym + <output>.pretty/ + <output>.3dshapes/)
        stem = probe_out.with_suffix("")
        sym_file = Path(str(stem) + ".kicad_sym")
        pretty_dir = Path(str(stem) + ".pretty")
        shapes_dir = Path(str(stem) + ".3dshapes")

        if sym_file.exists():
            try:
                txt = sym_file.read_text(encoding="utf-8", errors="replace")
                # Top-level (symbol "..." block beyond the lib header
                if re.search(r'\(symbol\s+"', txt):
                    result["has_symbol"] = True
            except Exception:
                pass

        if pretty_dir.is_dir():
            mods = list(pretty_dir.glob("*.kicad_mod"))
            if mods:
                result["has_footprint"] = True

        if shapes_dir.is_dir():
            three_d = list(shapes_dir.glob("*.step")) + list(shapes_dir.glob("*.wrl")) + list(shapes_dir.glob("*.stp"))
            if three_d:
                result["has_3d"] = True

    result["ok"] = result["has_symbol"] and result["has_footprint"]
    if not result["ok"]:
        missing = []
        if not result["has_symbol"]: missing.append("symbol")
        if not result["has_footprint"]: missing.append("footprint")
        result["partial_reason"] = "missing: " + ",".join(missing)
    return result


def get_pin_count(sym_file: Path, sym_name_prefix: str) -> int:
    """从 .kicad_sym 数指定 symbol 的 pin 数。"""
    if not sym_file.exists():
        return 0
    text = sym_file.read_text(encoding="utf-8")
    # 找 _0_1 子符号（含 pin 定义的那一段）
    pattern = rf'\(symbol\s+"{re.escape(sym_name_prefix)}[^"]*_0_1"(.+?)(?=\(symbol\s+"[A-Z]|\Z)'
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return 0
    return m.group(1).count("(pin ")


def get_symbol_metadata(sym_file: Path, mpn: str) -> dict:
    """Find the top-level vendored symbol and its bound footprint property."""
    if not sym_file.exists():
        return {"symbol": "", "footprint": ""}
    text = sym_file.read_text(errors="replace", encoding="utf-8")
    matches = list(re.finditer(r'^[ \t]+\(symbol\s+"([^"]+)"', text, re.MULTILINE))
    norm_mpn = mpn.upper().replace("-", "").replace("_", "").replace("/", "")
    for i, m in enumerate(matches):
        name = m.group(1)
        if re.search(r"_\d+_\d+$", name):
            continue
        norm_name = name.upper().replace("-", "").replace("_", "").replace("/", "")
        if norm_mpn not in norm_name and norm_name not in norm_mpn:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.start():end]
        fp = ""
        fp_match = re.search(
            r'\(property\s+"Footprint"\s+"([^"]+)"',
            block,
            re.DOTALL,
        )
        if fp_match:
            fp = fp_match.group(1)
        return {"symbol": f"components:{name}", "footprint": fp}
    return {"symbol": "", "footprint": ""}


def update_readme(lib_name: str, downloads: list[dict]):
    """追加新下载到 lib_external/README.md。"""
    LIB_EXTERNAL.mkdir(parents=True, exist_ok=True)
    readme = LIB_EXTERNAL / "README.md"
    today = date.today().isoformat()

    if not readme.exists():
        readme.write_text(
            "# 工作区共享元件库（vendoring）\n\n"
            "每个 KiCad 项目共享，避免重复下载。\n\n"
            "## 元件清单\n\n"
            "| MPN | LCSC ID | Pin 数 | 下载日期 | 备注 |\n"
            "|---|---|---|---|---|\n", encoding="utf-8"
        )

    text = readme.read_text(encoding="utf-8")
    lines = text.splitlines()
    for d in downloads:
        meta = get_symbol_metadata(LIB_EXTERNAL / f"{lib_name}.kicad_sym", d["mpn"])
        note_parts = []
        if meta.get("symbol"):
            note_parts.append(f"symbol=`{meta['symbol']}`")
        if meta.get("footprint"):
            note_parts.append(f"footprint=`{meta['footprint']}`")
        note = "; ".join(note_parts)
        new_line = f"| {d['mpn']} | {d['lcsc_id']} | {d['pins']} | {today} | {note} |"
        row_prefix = f"| {d['mpn']} |"
        lines = [ln for ln in lines if not ln.startswith(row_prefix)]
        lines.append(new_line)
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mpns", nargs="+", help="MPN 列表（如 AMC1311DWV TLV70033DDCR）")
    ap.add_argument("--lib-name", default="components",
                    help=argparse.SUPPRESS)
    ap.add_argument("--datasheet-dir",
                    help="顺手下 datasheet 到此目录（默认 lib_external/datasheets/）")
    ap.add_argument("--verify-only", action="store_true",
                    help="Probe LCSC + EasyEDA availability without writing to lib_external. "
                         "Outputs JSON with {ok, has_symbol, has_footprint, has_3d, ...} per MPN.")
    ap.add_argument("--allow-write-from-component-selecting", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not EASYEDA2KICAD.exists():
        sys.exit(f"❌ easyeda2kicad 不存在: {EASYEDA2KICAD}\n   先跑: cd PCB-Agent-Teams && .venv/bin/pip install easyeda2kicad")

    # --verify-only: probe each MPN, emit JSON, exit
    if args.verify_only:
        import json as _json
        results = [verify_one(m) for m in args.mpns]
        print(_json.dumps(results, ensure_ascii=False, indent=2))
        sys.exit(0 if all(r.get("ok") for r in results) else 1)

    if not args.allow_write_from_component_selecting and os.environ.get(WRITE_GUARD_ENV) != "1":
        sys.exit(
            "❌ direct LCSC write is blocked. Run component-preparing, "
            "or use --verify-only for a non-writing probe."
        )
    if args.lib_name != "components":
        sys.exit("❌ lib_external uses a single canonical library: components")

    LIB_EXTERNAL.mkdir(parents=True, exist_ok=True)
    sym_file = LIB_EXTERNAL / f"{args.lib_name}.kicad_sym"
    downloads = []

    # datasheet 目录默认 lib_external/datasheets
    ds_dir = (Path(args.datasheet_dir) if args.datasheet_dir
              else LIB_EXTERNAL / "datasheets")
    ds_dir.mkdir(parents=True, exist_ok=True)

    # 顺手下 datasheet 用：失败不影响主流程
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from download_datasheet import download_datasheet
        _ds_avail = True
    except Exception as e:
        print(f"⚠ download_datasheet 不可用: {e}")
        _ds_avail = False

    for mpn in args.mpns:
        print(f"\n=== {mpn} ===")
        lcsc_id = query_lcsc_id(mpn)
        if not lcsc_id:
            print(f"  ❌ 没查到 LCSC ID")
            continue
        print(f"  LCSC: {lcsc_id}")

        if download_one(lcsc_id, args.lib_name):
            pins = get_pin_count(sym_file, mpn)
            print(f"  ✅ 下载完成 ({pins} pin)")
            ds_path = None
            if _ds_avail:
                ds_path = download_datasheet(lcsc_id, mpn, ds_dir)
                if ds_path:
                    print(f"  📄 datasheet → {ds_path}")
            downloads.append({"mpn": mpn, "lcsc_id": lcsc_id, "pins": pins,
                              "datasheet": str(ds_path) if ds_path else None})
        else:
            print(f"  ❌ 下载失败")

    if downloads:
        update_readme(args.lib_name, downloads)
        print(f"\n✅ {len(downloads)} 个元件下载完成")
        print(f"   库文件: {sym_file}")
        print(f"   README: {LIB_EXTERNAL / 'README.md'}")
        print(f"\n⚠️  下一步：对照 datasheet 校验每个元件的 pin 1-N 含义")
        print(f"   LCSC 数据有时 pin 顺序跟厂商 datasheet 不一致")


if __name__ == "__main__":
    main()
