#!/usr/bin/env python3
"""Stage 0：BOM pin 兼容验证（画图之前必跑）。

工作流：
  1. AST 解析 .py → 拿所有元件 (ref, symbol, value=MPN, footprint)
  2. 对每个 MPN：
     a. 检查 lib_external/components.kicad_sym 里有没有
     b. 没有 → 标记为库证据缺失；不下载
     c. 已有 → 拿真实 pin 数
  3. 对比 .py 里用的占位 symbol 的 pin 数 vs 真元件 pin 数
  4. 输出报告：✅ 兼容 / ⚠️ 占位但 pin 数对 / ❌ pin 数不同（必须改 .py）

注意：本脚本不再下载库。component-selecting 是唯一选品 / vendoring gate；
缺库时先跑 component-preparing，再跑 bom-readiness。

退出码：
  0 = 全 OK
  1 = 有 ❌ 必修问题
  2 = 有 ⚠️ 警告但能继续

用法:
    python verify_bom.py <project.py>
    python verify_bom.py <project.py> --auto-download  # deprecated no-op
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

import kicad_sch_api as ksa

# Make sibling _helpers/ importable without polluting sys.path globally.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.register_ksa import register as _register_ksa, KICAD_ROOT, COMPONENTS_LIB  # noqa: E402

# Register lib_external before any ksa.get_symbol_info() lookup; otherwise every
# `components:*` symbol comes back as "not found" and trips PIN_COUNT_MISMATCH.
_register_ksa()


# 已知"通用占位 symbol"白名单（常见值类元件 R/C/L/D/LED 等）
GENERIC_PLACEHOLDERS = {
    "Device:R", "Device:C", "Device:L", "Device:LED", "Device:D",
    "Device:D_TVS", "Device:D_Schottky", "Device:D_Zener",
    "Device:R_Small", "Device:C_Small",
}


def parse_py_bom(py_file: Path) -> list[dict]:
    """从 .py AST 提元件清单。"""
    text = py_file.read_text()
    tree = ast.parse(text)
    components = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not isinstance(node.value, ast.Call):
            continue
        f = node.value.func
        fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if fname != "Component":
            continue

        comp = {"symbol": None, "ref": None, "value": None, "footprint": None}
        for kw in node.value.keywords:
            if kw.arg in comp and isinstance(kw.value, ast.Constant):
                comp[kw.arg] = kw.value.value
        if comp["ref"] and comp["symbol"]:
            components.append(comp)
    return components


def get_lib_pin_count(lib_id: str) -> int:
    """用 ksa 拿 symbol 的 pin 数。"""
    sym = ksa.get_symbol_info(lib_id)
    return len(sym.pins) if sym else 0


def find_in_components_lib(mpn: str) -> tuple[str, int] | None:
    """在 lib_external/components 里找包含 MPN 的 symbol。返回 (symbol_name, pin_count) 或 None。"""
    if not COMPONENTS_LIB.exists():
        return None
    text = COMPONENTS_LIB.read_text()
    # 找 (symbol "X" 顶层（不带 sub-symbol suffixes _0_0 / _0_1 / _1_0 / _1_1）
    for m in re.finditer(r'\(symbol\s+"([^"]+)"', text):
        name = m.group(1)
        if re.search(r"_[01]_[01]$", name):
            continue
        # name 含 MPN？比对忽略大小写 + 去掉 - / 空格 / 路径分隔
        norm_mpn = mpn.upper().replace("-", "").replace("_", "").replace("/", "")
        norm_name = name.upper().replace("-", "").replace("_", "").replace("/", "")
        if norm_mpn in norm_name or norm_name.startswith(norm_mpn):
            # 数 pin —— 子符号可能是 _0_1 (KiCad standard) or _0_0 (extended/inherited).
            # Try both suffixes on this symbol, then if zero, follow `extends` to a parent.
            def _count_for(target_name: str) -> int:
                for suffix in ("_0_1", "_0_0", "_1_1"):
                    # stop at ANY next symbol declaration — names may start with
                    # a digit (2N3904BU), not only [A-Z]
                    sub_pattern = rf'\(symbol\s+"{re.escape(target_name)}{suffix}"(.+?)(?=\(symbol\s+"|\Z)'
                    sm = re.search(sub_pattern, text, re.DOTALL)
                    if sm:
                        n = sm.group(1).count("(pin ")
                        if n:
                            return n
                return 0

            pin_count = _count_for(name)
            if pin_count == 0:
                # Look up the symbol's own block to detect (extends "PARENT")
                own_pattern = rf'\(symbol\s+"{re.escape(name)}"(.+?)(?=\(symbol\s+"|\Z)'
                own_match = re.search(own_pattern, text, re.DOTALL)
                if own_match:
                    ext_match = re.search(r'\(extends\s+"([^"]+)"', own_match.group(1))
                    if ext_match:
                        pin_count = _count_for(ext_match.group(1))
            return f"components:{name}", pin_count
    return None


def _blocked_download_attempt(mpn: str, datasheet_dir=None) -> bool:
    """Deprecated compatibility shim.

    verify_bom used to download missing LCSC libraries. That bypasses the
    component-selecting locale/vendor gate, so it is now intentionally blocked.
    """
    raise RuntimeError(
        "verify_bom no longer downloads libraries; run component-preparing"
    )


def verify(py_file: Path, auto_download: bool = False) -> dict:
    """主验证逻辑。"""
    if auto_download:
        print("⚠ --auto-download 已废弃并被忽略；请先跑 component-preparing")

    bom = parse_py_bom(py_file)
    print(f"=== Stage 0: BOM 验证 ===\n")
    print(f".py 元件清单: {len(bom)} 个\n")

    report = {"components": [], "summary": {}}
    n_ok = 0
    n_warn = 0
    n_fail = 0
    n_pure_placeholder = 0

    for comp in bom:
        ref = comp["ref"]
        py_symbol = comp["symbol"]
        mpn = comp["value"] or ""
        py_pin_count = get_lib_pin_count(py_symbol)

        item = {
            "ref": ref,
            "mpn": mpn,
            "py_symbol": py_symbol,
            "py_pin_count": py_pin_count,
            "real_symbol": None,
            "real_pin_count": 0,
            "status": "UNKNOWN",
            "action": "",
        }

        # 通用占位（R/C/L/D/LED）— 不验证 MPN，只看 footprint 是否合理
        if py_symbol in GENERIC_PLACEHOLDERS:
            item["status"] = "GENERIC_OK"
            item["action"] = "通用元件占位（footprint 决定 PCB 是否对）"
            n_pure_placeholder += 1
            report["components"].append(item)
            continue

        # 连接器 / 电源 / Switch / J 系列 = 物理元件，pin 数必须靠 datasheet（不强求上游库一致）
        is_physical_connector = (
            "Connector" in py_symbol or
            "Switch" in py_symbol or
            "J_" in (mpn or "") or mpn.startswith("J") and len(mpn) <= 4
        )

        # 非通用元件 — 找真元件
        found = find_in_components_lib(mpn)
        # 物理连接器/开关：永远走 PHYSICAL_OK，不做 pin 数对照
        # 原因：vendor symbol 可能含机械 pin（螺丝、防呆、外壳针）跟实际电气 pin 不同，
        # py 用 Conn_01xN 占位本来就只表达电气 pin。强对照会假阳性。
        if is_physical_connector:
            item["status"] = "PHYSICAL_OK"
            item["action"] = f"物理连接器/开关，按 datasheet 校 footprint 即可（{py_pin_count} pin）"
            n_pure_placeholder += 1
        elif not found:
            item["status"] = "LIB_NOT_FOUND"
            item["action"] = (
                f"lib_external 没有 {mpn} 的真 symbol；先跑 component-preparing，"
                "再跑 bom-readiness"
            )
            n_warn += 1
        else:
            real_symbol, real_pin_count = found
            item["real_symbol"] = real_symbol
            item["real_pin_count"] = real_pin_count
            if py_pin_count == real_pin_count:
                item["status"] = "OK_PIN_COMPAT"
                item["action"] = f"占位 {py_symbol} pin 数对 ({py_pin_count})，可继续。PCB 前换真 symbol = {real_symbol}"
                n_ok += 1
            else:
                item["status"] = "PIN_COUNT_MISMATCH"
                item["action"] = f"❌ 占位 {py_pin_count} pin ≠ 真元件 {real_pin_count} pin。改 .py: symbol=\"{real_symbol}\" + 对照 datasheet 重写 pin 映射"
                n_fail += 1

        report["components"].append(item)

    # 打印每个元件
    for item in report["components"]:
        icon = {"GENERIC_OK": "🔷", "OK_PIN_COMPAT": "✅",
                "LIB_NOT_FOUND": "⚠️ ", "PIN_COUNT_MISMATCH": "❌"}.get(item["status"], "?")
        print(f"  {icon} {item['ref']:8} {item['mpn']:20} | {item['py_symbol']}")
        if item["status"] != "GENERIC_OK":
            print(f"     → {item['action']}")

    report["summary"] = {
        "total": len(bom),
        "generic_ok": n_pure_placeholder,
        "ok_pin_compat": n_ok,
        "warn_library_not_found": n_warn,
        "warn_lcsc_not_found": n_warn,  # backwards-compatible alias
        "fail_pin_mismatch": n_fail,
    }

    print(f"\n=== 总结 ===")
    print(f"  🔷 通用元件: {n_pure_placeholder}")
    print(f"  ✅ pin 兼容: {n_ok}")
    print(f"  ⚠️  库证据缺失: {n_warn}（回 component-selecting / bom-readiness）")
    print(f"  ❌ pin 数不同: {n_fail}（必须改 .py 才能做 PCB）")

    if n_fail > 0:
        print(f"\n❌ 不能进 PCB 阶段。先改 .py 把 pin 不匹配的元件换成真 symbol。")
        return report

    if n_warn > 0:
        print(f"\n⚠️  能继续画图，但 {n_warn} 个元件缺真库证据；PCB 前必须补齐。")
    else:
        print(f"\n✅ BOM 全过，可进 PCB 阶段。")

    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("py_file")
    ap.add_argument("--auto-download", action="store_true",
                    help="deprecated no-op；缺库请跑 component-preparing")
    args = ap.parse_args()

    report = verify(Path(args.py_file).resolve(), args.auto_download)
    n_fail = report["summary"]["fail_pin_mismatch"]
    n_warn = report["summary"].get("warn_library_not_found",
                                   report["summary"].get("warn_lcsc_not_found", 0))

    if n_fail > 0:
        sys.exit(1)
    elif n_warn > 0:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
