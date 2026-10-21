#!/usr/bin/env python3
"""vendor_mpn — Phase 2.5 单 MPN vendoring 入口（orchestrator）。

工作流：
  Path 1. lib_external/incoming/<MPN>.zip 已存在（user 手工放）→ import 路径
  Path 2. agent-browser dkjp session → DigiKey JP /en/models/<dk_part_id>
          → 点 "Download Now" 抓 zip → import 路径
  Path 3. LCSC/EasyEDA fallback → 调 download_lcsc_lib.py（同 selecting verify
          走的 easyeda2kicad 调用，仅 --output 路径不同）→ 直接落盘
          lib_external/components.{kicad_sym,pretty,3dshapes}
          → selecting library.status ∈ LCSC_COMMIT_STATUSES（见 module 顶部）
            时走这条；当前覆盖 lcsc_vendorable + external_cache_exact。
            `standard_ready` 不在此 set —— 那是 KiCad std lib，不需要 LCSC fetch。
  Path 4. 全失败 → 打印手工下载指引 + exit 4

成功后更新 evidence JSON：library.status = vendored_complete

需要 Projects/<name>/datasheets/component_selecting/<MPN>.json 已存在
（component-selecting 阶段产出），从里面读 dk_part_id / vendor.product_url。

用法：
  python3 vendor_mpn.py Projects/<name> "TMA 0505S"
  python3 vendor_mpn.py Projects/<name> "100SP1T1B1M2QEH" --auto-download
  python3 vendor_mpn.py Projects/<name> --all  # 扫所有 reject 的 MPN，逐个 vendor
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
import subprocess
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent

# Shared workspace/skills discovery: pcbenv at the repo root.
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
import pcbenv

WORKSPACE = pcbenv.workspace_root()
LIB_INCOMING = WORKSPACE / "lib_external" / "incoming"
LIB_EXTERNAL = WORKSPACE / "lib_external"
IMPORT_SCRIPT = THIS_DIR / "import_incoming_zip.py"
VERIFY_SCRIPT = THIS_DIR / "verify_vendoring.py"
LCSC_DOWNLOADER = pcbenv.skills_root() / "draw-schematic/scripts/download_lcsc_lib.py"

# library.status values that route into Path 3 (LCSC easyeda2kicad commit).
# Must stay in sync with verify_vendoring.STATUS_NEEDS_LCSC_COMMIT — both
# scripts treat these as "lib_external commit required, easyeda2kicad path
# works because selecting already verified the LCSC C-number resolves".
# `standard_ready` is intentionally NOT here — that's KiCad std lib, no
# LCSC fetch required (Phase 3 .py references std lib symbol directly).
LCSC_COMMIT_STATUSES = {"lcsc_vendorable", "external_cache_exact"}


def safe_name(mpn: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[/\\:*?\"<>|,;\s]", "_", mpn)).strip("_")


def find_incoming_zip(mpn: str) -> Path | None:
    """Look for lib_external/incoming/<MPN-variants>.zip."""
    if not LIB_INCOMING.exists():
        return None
    candidates = [
        LIB_INCOMING / f"{safe_name(mpn)}.zip",
        LIB_INCOMING / f"{mpn}.zip",
        LIB_INCOMING / f"{mpn.replace(' ', '_')}.zip",
        LIB_INCOMING / f"{mpn.replace('/', '_')}.zip",
    ]
    return next((p for p in candidates if p.exists()), None)


def load_evidence(project: Path, mpn: str) -> dict | None:
    ev_dir = project / "datasheets" / "component_selecting"
    candidates = [
        ev_dir / f"{safe_name(mpn)}.json",
        ev_dir / f"{mpn.replace(' ', '_').replace('/', '_')}.json",
    ]
    for p in candidates:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def auto_download_via_agent_browser(mpn: str, dk_part_id: str | None,
                                     product_url: str | None) -> Path | None:
    """Try to use agent-browser dkjp session to download the zip.

    Returns path to downloaded zip, or None if any step fails.
    Implementation note: this is a thin wrapper around agent-browser CLI.
    Actual DK page interaction is complex (DK changes layouts); this function
    is best-effort. On any failure, returns None and lets caller fall back to
    manual instructions.
    """
    # Sanity: agent-browser must be on PATH
    try:
        subprocess.run(["agent-browser", "--version"],
                       capture_output=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    # Sanity: dkjp session must exist
    sess_dir = Path.home() / ".agent-browser" / "sessions"
    if not list(sess_dir.glob("dkjp*")):
        return None

    # Build models URL
    if dk_part_id:
        url = f"https://www.digikey.jp/en/models/{dk_part_id}"
    elif product_url:
        # Convert /products/detail/.../<id> to /models/<id> if possible
        m = re.search(r"/products/detail/[^/]+/[^/]+/(\d+)", product_url)
        if not m:
            return None
        url = f"https://www.digikey.jp/en/models/{m.group(1)}"
    else:
        return None

    # NOTE: Real implementation needs agent-browser DOM scripting to:
    #   1. Navigate to URL
    #   2. Wait for "Download Now" button (KiCad format)
    #   3. Click and capture the resulting zip URL
    #   4. Save to LIB_INCOMING/<MPN>.zip
    # This is non-trivial and DK page layout changes break it.
    #
    # For v1 of this script, return None to force manual fallback.
    # Future: implement DK page scripting here.
    return None


def lcsc_commit_fallback(project: Path, mpn: str) -> int:
    """Path 3: drive download_lcsc_lib.py to commit symbol+footprint+3D into
    lib_external/components.* via the SAME easyeda2kicad call that selecting's
    `lcsc_vendorable` verify-only already ran (only --output path differs).

    Returns 0 on success, non-zero on failure (caller falls through to manual).

    Side effects (on success):
      • lib_external/components.{kicad_sym,pretty,3dshapes} updated in place
      • lib_external/README.md row appended by download_lcsc_lib.py
      • evidence JSON library block updated to vendored_complete
    """
    if not LCSC_DOWNLOADER.exists():
        print(f"  ⚠ LCSC downloader missing: {LCSC_DOWNLOADER}")
        return -1
    print(f"→ LCSC fallback (same easyeda2kicad path selecting verified)…")
    rc = subprocess.run(
        [sys.executable, str(LCSC_DOWNLOADER), mpn,
         "--allow-write-from-component-selecting"],
    )
    if rc.returncode != 0:
        return rc.returncode

    # Update evidence JSON — bypassed import_incoming_zip, so do it inline.
    sym_file = LIB_EXTERNAL / "components.kicad_sym"
    sym_name, fp_name = "", ""
    try:
        sys.path.insert(0, str(LCSC_DOWNLOADER.parent))
        from download_lcsc_lib import get_symbol_metadata  # type: ignore
        sys.path.insert(0, str(IMPORT_SCRIPT.parent))
        from import_incoming_zip import update_evidence  # type: ignore
        meta = get_symbol_metadata(sym_file, mpn)
        sym_name = (meta.get("symbol") or "").replace("components:", "")
        fp_name = (meta.get("footprint") or "").replace("components:", "")
        update_evidence(
            project, mpn,
            [sym_name] if sym_name else [],
            [fp_name] if fp_name else [],
        )
    except Exception as e:
        print(f"  ⚠ LCSC commit succeeded but evidence update failed: {e}")
        # Library files are on disk — not a hard failure; user can re-run check_readiness
        return 0
    return 0


def manual_instructions(mpn: str, product_url: str | None) -> str:
    safe = safe_name(mpn)
    target = LIB_INCOMING / f"{safe}.zip"
    return f"""
❌ 自动 vendoring 没成功。手工下载 3 步：

  1. 浏览器打开 DigiKey 产品页：
     {product_url or '(在 evidence JSON 里查 vendor.product_url)'}

  2. 页面右侧找 "Symbol/Footprint/3D Model" 区块：
     • 选择 "Download EDA Models"（或 "Symbol & Footprint"）
     • 弹出窗口里 EDA 软件选 "KiCad"
     • 点 "Download Now"
     • 浏览器下载 zip

  3. 把下载的 zip 改名 + 移到本工作区：
     mkdir -p "{LIB_INCOMING}"
     mv ~/Downloads/<下载的文件名>.zip "{target}"

  4. 重跑本脚本：
     python3 skills/component-preparing/scripts/vendor_mpn.py \\
         <project_dir> "{mpn}"
"""


def vendor_one(project: Path, mpn: str, allow_auto: bool, dry_run: bool) -> int:
    print(f"\n=== vendor_mpn: {mpn} ===")

    # Load evidence
    ev = load_evidence(project, mpn)
    if not ev:
        print(f"❌ evidence JSON not found for {mpn}", file=sys.stderr)
        return 3
    product_url = (ev.get("vendor") or {}).get("product_url")
    dk_part_id = (ev.get("vendor") or {}).get("dk_part_id")

    # Path 1: zip already exists
    zip_path = find_incoming_zip(mpn)
    if zip_path:
        print(f"✓ found existing zip: {zip_path}")

    # Path 2: auto-download (best-effort)
    if not zip_path and allow_auto:
        print("→ trying agent-browser dkjp auto-download…")
        zip_path = auto_download_via_agent_browser(mpn, dk_part_id, product_url)
        if zip_path:
            print(f"✓ auto-downloaded: {zip_path}")
        else:
            print("  (auto-download not available — trying LCSC fallback)")

    # Path 3: LCSC fallback — see LCSC_COMMIT_STATUSES at module top for
    # which library.status values route here. For parts that don't fit (e.g.,
    # LCSC has no C-number for this MPN), skip ahead to manual instructions
    # instead of hitting a sure failure.
    if not zip_path:
        lib_block = ev.get("library") or {}
        if lib_block.get("status") in LCSC_COMMIT_STATUSES:
            rc = lcsc_commit_fallback(project, mpn)
            if rc == 0:
                print(f"✓ LCSC commit succeeded — lib_external/components.* updated")
                return 0
            print("  (LCSC commit failed — falling back to manual)")

    # Path 4: manual instructions
    if not zip_path:
        print(manual_instructions(mpn, product_url))
        return 4

    # Run import
    cmd = [
        sys.executable,
        str(IMPORT_SCRIPT),
        "--zip", str(zip_path),
        "--mpn", mpn,
        "--project", str(project),
    ]
    if dry_run:
        cmd.append("--dry-run")
    print(f"  running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def find_reject_mpns(project: Path) -> list[str]:
    """Run verify_vendoring.py and parse output to get list of rejected MPNs."""
    json_out = project / "_artifacts" / "phase2.5_vendoring_check.json"
    json_out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(project), "--json-output", str(json_out)],
        capture_output=True,
    )
    if not json_out.exists():
        return []
    data = json.loads(json_out.read_text(encoding="utf-8"))
    return [it["mpn"] for it in data.get("items", []) if it.get("verdict") == "reject"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2.5 single-MPN vendoring orchestrator")
    parser.add_argument("project", type=Path, help="Projects/<name>")
    parser.add_argument("mpn", nargs="?", help="MPN to vendor (omit if --all)")
    parser.add_argument("--all", action="store_true",
                        help="Auto-detect rejected MPNs via verify_vendoring.py")
    parser.add_argument("--auto-download", action="store_true",
                        help="Try agent-browser dkjp auto-download (default: manual only)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.project.is_dir():
        print(f"error: project not found: {args.project}", file=sys.stderr)
        return 2

    if args.all:
        mpns = find_reject_mpns(args.project)
        if not mpns:
            print("✅ no rejected MPNs — all already vendored or generic")
            return 0
        print(f"vendoring {len(mpns)} rejected MPNs: {mpns}")
        rc_total = 0
        for m in mpns:
            rc = vendor_one(args.project, m, args.auto_download, args.dry_run)
            if rc != 0:
                rc_total = rc
        return rc_total

    if not args.mpn:
        parser.error("must provide MPN or --all")
    return vendor_one(args.project, args.mpn, args.auto_download, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
