#!/usr/bin/env python3
"""Deprecated compatibility shim for old footprint auto-resolve flow.

This script used to auto-download missing LCSC footprints after
verify_footprints reported "0 candidates". That bypasses the component-selecting
locale/vendor gate, so it is now intentionally non-writing.

Current rule:
  - draw-schematic: fail fast on 0 candidates
  - user/agent: run component-preparing for the MPN
  - then run bom-readiness and come back to draw-schematic

用法（模块）:
    from resolve_missing_footprint import auto_resolve
    report = auto_resolve(py_file, needs_manual)

或命令行:
    python resolve_missing_footprint.py <py_file> --mpn <MPN> --ref <REF>
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
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional


def _count_pads(kicad_mod_path: Path) -> int:
    """数 .kicad_mod 文件里电气 pad 个数（type smd/thru_hole）。"""
    if not kicad_mod_path.exists():
        return 0
    text = kicad_mod_path.read_text(encoding="utf-8")
    # (pad "1" smd ...) 或 (pad "1" thru_hole ...)
    pads = re.findall(r'\(pad\s+"\w+"\s+(?:smd|thru_hole)\b', text)
    return len(pads)


def _extract_pitch(footprint_name: str) -> Optional[float]:
    """从 footprint 名提 pitch（mm）。例 'P5.08' → 5.08, 'P7.62mm' → 7.62。"""
    m = re.search(r'P(\d+\.?\d*)(?:mm)?', footprint_name)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _extract_pitch_from_description(name: str) -> Optional[float]:
    """从原 footprint 名（用户写的）提 pitch。

    例 'TerminalBlock_Phoenix_MKDS-1,5-2_2x5.00mm_Vertical' → 5.00
        'MKDS-5-2_2x7.50mm' → 7.50
    """
    # 优先找 "_NxYY.YYmm" 这种
    m = re.search(r'_\d+x(\d+\.?\d*)mm', name)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # 退而求其次找 "PYY.YY"
    return _extract_pitch(name)


def _expected_pin_count_from_orig(orig_footprint: str) -> Optional[int]:
    """从原 footprint 名提期望 pin 数（如 '_2x5.00' = 2 pin）。"""
    m = re.search(r'_(\d+)x\d+\.?\d*mm', orig_footprint)
    if m:
        return int(m.group(1))
    # MKDS-5-2 / MKDS-1,5-2 这种"型号-段-段-N"末段是 pin 数
    m = re.search(r'MKDS-[\d,.]+-(\d+)', orig_footprint)
    if m:
        return int(m.group(1))
    return None


def auto_resolve_one(mpn: str, orig_footprint: str,
                     auto_download: bool = True,
                     datasheet_dir: Optional[Path] = None) -> Dict:
    """Return a blocked result; component-selecting owns vendoring now.

    返回:
      {
        "mpn": str,
        "orig_footprint": str,
        "lcsc_id": str | None,
        "downloaded_footprint": str | None,  # "components:CONN-TH_..."
        "expected_pins": int | None,
        "expected_pitch": float | None,
        "actual_pins": int,
        "actual_pitch": float | None,
        "match": bool,                       # 特征一致
        "action": str,                       # 给 LLM 的建议
      }
    """
    return {
        "mpn": mpn, "orig_footprint": orig_footprint,
        "lcsc_id": None, "downloaded_footprint": None,
        "expected_pins": _expected_pin_count_from_orig(orig_footprint),
        "expected_pitch": _extract_pitch_from_description(orig_footprint),
        "actual_pins": 0, "actual_pitch": None,
        "match": False,
        "blocked": True,
        "action": (
            "此脚本已禁用写库。请先跑 component-preparing "
            f"--mpn '{mpn}' --project-path <proj> --verified-url ...，"
            "再跑 bom-readiness；draw-schematic 只消费 sentinel。"
        ),
    }


def auto_resolve(needs_manual: List[Dict],
                 datasheet_dir: Optional[Path] = None) -> List[Dict]:
    """对 verify_footprints 报的 needs_manual 列表批量调研。

    需要 needs_manual 元素含: ref / value / ref（footprint 字符串）
    datasheet_dir 已废弃，仅保留函数签名兼容。
    """
    out = []
    for m in needs_manual:
        cands = m.get("candidates", [])
        if cands:
            # 已经有候选不需要走 LCSC 下载
            continue
        mpn = m.get("value")
        orig_fp = m.get("ref")
        if not mpn or not orig_fp:
            continue
        # MPN 太短（< 4 字符）跳过（避免 "1uF" 这种 value 误查）
        if len(mpn) < 4 or not any(c.isdigit() for c in mpn):
            continue
        print(f"  阻断旧自动下载路径 {m.get('ref_designator')} = MPN {mpn} ...")
        r = auto_resolve_one(mpn, orig_fp, datasheet_dir=datasheet_dir)
        r["ref_designator"] = m.get("ref_designator")
        out.append(r)
        print(f"    {r['action']}")
        if r.get("datasheet_path"):
            print(f"    📄 datasheet: {r['datasheet_path']}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("py_file", nargs="?", help="（可选）项目 .py，从中提取需调研的元件")
    ap.add_argument("--mpn", help="单元件调研模式：MPN")
    ap.add_argument("--orig-footprint", help="单元件调研模式：原 footprint 引用")
    args = ap.parse_args()

    if args.mpn:
        rep = auto_resolve_one(args.mpn, args.orig_footprint or args.mpn)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return

    if args.py_file:
        sys.path.insert(0, str(Path(__file__).parent))
        from verify_footprints import verify_and_fix
        rep = verify_and_fix(Path(args.py_file), do_fix=False)
        results = auto_resolve(rep.get("needs_manual", []))
        print("\n=== 调研报告 ===")
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
