#!/usr/bin/env python3
"""L4 — footprint 可用性验证 + 自动修。

为啥需要这一层：
  ERC 把 footprint 引用错只当 warning。结果 sch 阶段全过，
  PCB 阶段 F8 (Update PCB from Schematic) 才发现 footprint 找不到 →
  元件不进板 → 飞线全断 → 没法布线 → 又得回头修 .py 重跑。

策略：
  1. 索引：扫 KiCad 自带 footprint 库 + lib_external/components.pretty/，
     建 footprint 名 → 库列表的反向索引
  2. 验证：对项目 .py 提到的每个 footprint `lib:name`，查存在性
  3. 自动修：name 在别的库里 → 走 fuzzy 把 .py 里的 lib 改对
     name 处处都没 → flag 给用户（触发 LCSC 下载或手工补）
  4. 还会顺手处理常见命名差异（如 SOIC-8W ↔ SOIC-8）

用法（模块）:
    from verify_footprints import verify_and_fix
    report = verify_and_fix(py_file)

或命令行:
    python verify_footprints.py <py_file>          # 只查
    python verify_footprints.py <py_file> --fix    # 查 + 自动修 .py
"""
import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ============================================================
# Footprint 库搜索路径（硬编码，跨平台）
# ============================================================

_KICAD_FP_DIRS = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",  # macOS
    "/usr/share/kicad/footprints",                                       # Linux apt
    "/usr/local/share/kicad/footprints",                                 # Linux compile
    "/snap/kicad/current/share/kicad/footprints",                        # Linux snap
    r"C:\Program Files\KiCad\10.0\share\kicad\footprints",               # Windows
    r"F:\KiCad\share\kicad\footprints",                                  # Windows custom drive
]

_LIB_EXTERNAL = Path(__file__).resolve().parents[4] / "lib_external"


def _find_kicad_fp_dir() -> Path | None:
    for p in _KICAD_FP_DIRS:
        if Path(p).exists():
            return Path(p)
    return None


def build_footprint_index() -> Tuple[Dict[str, List[str]], Dict[Tuple[str, str], Path]]:
    """扫所有可用 footprint 库，建两个索引：

    name_to_libs: {fp_name: [lib_name, ...]} —— 给定 footprint 名能在哪些库找到
    exact: {(lib_name, fp_name): file_path} —— 精确查存在性
    """
    name_to_libs: Dict[str, List[str]] = defaultdict(list)
    exact: Dict[Tuple[str, str], Path] = {}

    search_paths = []
    kc = _find_kicad_fp_dir()
    if kc:
        search_paths.append(kc)
    if _LIB_EXTERNAL.exists():
        search_paths.append(_LIB_EXTERNAL)

    for base in search_paths:
        for lib_dir in base.iterdir():
            if not lib_dir.is_dir() or not lib_dir.name.endswith(".pretty"):
                continue
            lib_name = lib_dir.name[:-len(".pretty")]
            for mod in lib_dir.glob("*.kicad_mod"):
                fp_name = mod.stem
                exact[(lib_name, fp_name)] = mod
                if lib_name not in name_to_libs[fp_name]:
                    name_to_libs[fp_name].append(lib_name)
    return name_to_libs, exact


# ============================================================
# 从 .py 文件提取 (ref, value, footprint, line) 四元组
# 用 AST 而不是正则 —— Component(...) 可能跨多行
# ============================================================

_SCH_FP_PAT = re.compile(
    r'\(symbol\s+\(lib_id\s+"([^"]+)"\).*?'
    r'\(property\s+"Reference"\s+"([^"]+)".*?'
    r'\(property\s+"Value"\s+"([^"]+)".*?'
    r'\(property\s+"Footprint"\s+"([^"]+)"',
    re.DOTALL
)


def extract_components_from_sch(sch_path: Path) -> List[Dict]:
    """从已生成的 sch 提 (ref, value, footprint) 三元组。

    用途：.py 里动态拼字符串（R_SIG_FP.replace("R_", "C_")）AST 看不到，
    sch 里就是最终字面量。两步扫描：先扫 .py 改字面量，再扫 sch 兜底。
    """
    text = sch_path.read_text()
    out = []
    for lib_id, ref, value, fp in _SCH_FP_PAT.findall(text):
        if ref.startswith("#"):  # 跳过 #PWR / #FLG 之类内部符号
            continue
        if not fp:
            continue
        out.append({
            "ref": ref, "value": value, "footprint": fp,
            "lib_id": lib_id,
        })
    return out


def verify_and_fix_sch(sch_path: Path, do_fix: bool = False) -> Dict:
    """sch 阶段二次验证：扫 (property "Footprint" "...") 字段。
    do_fix=True 时直接 sed sch 文件（不改 .py）。
    """
    name_to_libs, exact = build_footprint_index()
    components = extract_components_from_sch(sch_path)
    missing: List[Dict] = []
    seen_ok = 0

    for c in components:
        fp_str = c["footprint"]
        if ':' not in fp_str:
            # Malformed footprint reference (no `lib:name` form). Don't silently
            # skip — it'd inflate the OK count. Route to needs_manual.
            missing.append({
                "ref": fp_str, "ref_designator": c["ref"],
                "value": c["value"], "candidates": [],
                "reason": "malformed footprint (no `lib:name` separator)",
            })
            continue
        lib, name = fp_str.split(':', 1)
        if (lib, name) in exact:
            seen_ok += 1
            continue
        cands = find_alternate(name, c["value"], name_to_libs)
        missing.append({
            "ref": fp_str, "ref_designator": c["ref"],
            "value": c["value"], "candidates": cands,
        })

    fixed: List[Dict] = []
    needs_manual: List[Dict] = []
    if do_fix and missing:
        text = sch_path.read_text()
        # 同一个 (ref, candidate) 组合可能被多个 component 共用（如 C3/C4/C6/C7
        # 都用 Resistor_SMD:C_0603_1608Metric）。替换是全文 replace，一次修全部，
        # 后续同组合的 entry 都标 fixed 不进 needs_manual。
        for m in missing:
            cands = m["candidates"]
            if len(cands) == 1:
                old = m["ref"]; new = cands[0]
                old_str = f'"Footprint" "{old}"'
                new_str = f'"Footprint" "{new}"'
                if old_str in text:
                    text = text.replace(old_str, new_str)
                    fixed.append({**m, "new": new})
                elif new_str in text:
                    # 同 ref 已被前一次 replace 修过 → 也算 fixed
                    fixed.append({**m, "new": new, "note": "已随同组合修过"})
                else:
                    needs_manual.append(m)
            else:
                needs_manual.append(m)
        if fixed:
            sch_path.write_text(text)
    elif missing:
        needs_manual = missing

    return {
        "ok": not missing or (do_fix and not needs_manual),
        "total": len(components),
        "ok_count": seen_ok,
        "fixed": fixed,
        "needs_manual": needs_manual,
    }


def _build_module_const_map(tree: ast.Module) -> Dict[str, object]:
    """Resolve top-level `NAME = "literal"` assignments so Component() calls
    that reference module-level constants (e.g. footprint=FP_R_0805) extract
    correctly. Without this, the Component() filter `if d["footprint"]` drops
    every component whose footprint is a Name reference rather than a string
    literal — silently producing zero components from a fully-populated .py
    and feeding a vacuous pass into Phase 2.5's BOM gate.

    Only handles string/numeric Constants; chained references (FP = OTHER)
    are intentionally not resolved (rare in practice and not worth the
    cycle-detection complexity).
    """
    const_map: Dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                const_map[target.id] = node.value.value
    return const_map


def _kw_value(kw_value: ast.AST, const_map: Dict[str, object]) -> Optional[object]:
    """Resolve a Component() kwarg AST node to its string/numeric value.

    Accepts ast.Constant (literal) and ast.Name (module-level constant ref).
    Anything else (function call, attribute access, f-string) returns None —
    the caller treats None as "field unset" which is the correct behavior for
    runtime-computed values we can't reason about statically.
    """
    if isinstance(kw_value, ast.Constant):
        return kw_value.value
    if isinstance(kw_value, ast.Name):
        return const_map.get(kw_value.id)
    return None


def extract_components_from_py(py_file: Path) -> List[Dict]:
    """AST 解析 .py，找所有 Component(symbol=..., ref=..., value=..., footprint=...)。

    返回 [{ref, value, footprint, symbol, line}]
    """
    text = py_file.read_text()
    tree = ast.parse(text)
    const_map = _build_module_const_map(tree)
    out: List[Dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = (func.attr if isinstance(func, ast.Attribute)
                     else func.id if isinstance(func, ast.Name)
                     else None)
        if func_name != "Component":
            continue
        d: Dict[str, Optional[str]] = {
            "ref": None, "value": None, "footprint": None, "symbol": None,
            "line": node.lineno,
        }
        for kw in node.keywords:
            if kw.arg not in d:
                continue
            resolved = _kw_value(kw.value, const_map)
            if resolved is not None:
                d[kw.arg] = resolved
        if d["footprint"]:
            out.append(d)
    return out


# ============================================================
# Fuzzy 匹配（处理常见命名差异）
# ============================================================

def _normalize_name(s: str) -> str:
    """归一化用于 fuzzy 匹配：删 SOIC-?W 这种 W 后缀，统一连字符。"""
    # SOIC-8W → SOIC-8（W 是 Wide Body 后缀，但 KiCad 库里数字+尺寸已表达宽度）
    s = re.sub(r'(SOIC|TSOP|SOP|MSOP)-(\d+)W', r'\1-\2', s)
    return s


def find_alternate(target: str, value: Optional[str],
                   name_to_libs: Dict[str, List[str]]) -> List[str]:
    """target 找不到时，找候选 footprint。返回 ['lib:name', ...]。

    匹配优先级（从严到松）：
      Tier 1：同名跨库（库名拼错，如 Resistor_SMD:C_0603 → Capacitor_SMD:C_0603）
      Tier 2：归一化 fuzzy（SOIC-8W ↔ SOIC-8 等命名差异）
      Tier 3：value (MPN) 子串匹配 lib_external 文件名
              （如 value="EG1218" → 找 SW-TH_EG1218）
    """
    candidates: List[str] = []

    # Tier 1：精确同名跨库
    if target in name_to_libs:
        for lib in name_to_libs[target]:
            candidates.append(f"{lib}:{target}")
        return candidates

    # Tier 2：归一化 fuzzy（双向）
    norm = _normalize_name(target)
    if norm != target and norm in name_to_libs:
        for lib in name_to_libs[norm]:
            candidates.append(f"{lib}:{norm}")
        return candidates
    for name, libs in name_to_libs.items():
        if _normalize_name(name) == target:
            for lib in libs:
                candidates.append(f"{lib}:{name}")
    if candidates:
        return candidates

    # Tier 3：value 子串 → lib_external 文件名匹配
    # 仅对足够长（≥4 字符）的 value 启用，避免 "C" / "R" 误匹配满库
    if value and len(value) >= 4:
        v_clean = re.sub(r'[^A-Za-z0-9]', '', value).lower()
        if len(v_clean) >= 4:
            for name, libs in name_to_libs.items():
                n_clean = re.sub(r'[^A-Za-z0-9]', '', name).lower()
                if v_clean in n_clean:
                    for lib in libs:
                        candidates.append(f"{lib}:{name}")
    if candidates:
        return candidates

    # Tier 4：原 footprint 名的"型号 token"匹配（如 MKDS-5-2、SOIC-8）
    # 提原名里夹连字符的类型代码（≥4 字符），全库搜包含该 token 的 footprint
    tokens = re.findall(r'[A-Z][A-Z0-9]{2,}(?:-[A-Z0-9,.]+)+', target)
    seen: set = set()
    for tok in tokens:
        for name, libs in name_to_libs.items():
            if tok in name and name not in seen:
                seen.add(name)
                for lib in libs:
                    candidates.append(f"{lib}:{name}")
    return candidates


# ============================================================
# 验证 + 报告
# ============================================================

def verify_and_fix(py_file: Path, do_fix: bool = False) -> Dict:
    """主入口。返回报告 dict。"""
    name_to_libs, exact = build_footprint_index()

    components = extract_components_from_py(py_file)
    if not components:
        return {
            "ok": True,
            "py_file": str(py_file),
            "total": 0,
            "missing": [],
            "fixed": [],
            "needs_manual": [],
        }

    # 对每个 component 的 footprint 引用
    missing: List[Dict] = []
    seen_ok: int = 0

    for c in components:
        fp_str = c["footprint"]
        line_no = c["line"]
        if ':' not in fp_str:
            missing.append({
                "line": line_no, "ref": fp_str, "ref_designator": c["ref"],
                "value": c["value"],
                "reason": "格式错（缺 ':')",
                "candidates": [],
            })
            continue
        lib, name = fp_str.split(':', 1)
        if (lib, name) in exact:
            seen_ok += 1
            continue
        candidates = find_alternate(name, c["value"], name_to_libs)
        missing.append({
            "line": line_no,
            "ref": fp_str,
            "ref_designator": c["ref"],
            "value": c["value"],
            "reason": ("库名错或命名差异（候选已找到）" if candidates
                       else "footprint 在所有可用库里都找不到"),
            "candidates": candidates,
        })

    # 自动修：仅对"恰好 1 个候选"的情况操作（多候选要人工选）
    fixed: List[Dict] = []
    needs_manual: List[Dict] = []
    if do_fix and missing:
        text = py_file.read_text()
        for m in missing:
            cands = m["candidates"]
            if len(cands) == 1:
                old = m["ref"]
                new = cands[0]
                # 谨慎：只替换精确匹配的字符串
                count = text.count(f'"{old}"') + text.count(f"'{old}'")
                if count > 0:
                    text = text.replace(f'"{old}"', f'"{new}"')
                    text = text.replace(f"'{old}'", f"'{new}'")
                    fixed.append({**m, "new": new, "occurrences": count})
                else:
                    needs_manual.append({**m, "note": "字符串没找到（已被其他改动覆盖？）"})
            else:
                needs_manual.append(m)
        if fixed:
            py_file.write_text(text)
    elif missing:
        # 不修，全部进 needs_manual
        for m in missing:
            if len(m["candidates"]) == 1:
                m = {**m, "note": "可自动修但没启用 --fix"}
            needs_manual.append(m)

    return {
        "ok": (not missing) or (do_fix and not needs_manual),
        "py_file": str(py_file),
        "total": len(components),
        "ok_count": seen_ok,
        "missing": [m for m in missing],
        "fixed": fixed,
        "needs_manual": needs_manual,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("py_file", help="circuit-synth Python 源文件")
    ap.add_argument("--fix", action="store_true", help="自动修单候选的库引用")
    args = ap.parse_args()

    rep = verify_and_fix(Path(args.py_file), do_fix=args.fix)
    print(f"\n=== Footprint 验证报告 ===")
    print(f"  .py 文件: {rep['py_file']}")
    print(f"  footprint 引用总数: {rep['total']}")
    print(f"  ✅ 直接 OK: {rep.get('ok_count', 0)}")
    if rep['fixed']:
        print(f"  🔧 自动修: {len(rep['fixed'])}")
        for f in rep['fixed']:
            print(f"      L{f['line']}: {f['ref']} → {f['new']}")
    if rep['needs_manual']:
        print(f"  ⚠ 需手工: {len(rep['needs_manual'])}")
        for f in rep['needs_manual']:
            cands = f.get('candidates', [])
            cand_str = (f"  候选: {cands}" if cands else "")
            print(f"      L{f['line']} [{f.get('ref_designator')}]: {f['ref']}  ← {f['reason']}{cand_str}")
    print(f"\n=== 总结: {'✅ 全过' if rep['ok'] else '❌ 有问题'} ===")
    if not rep['ok']:
        sys.exit(1)


if __name__ == "__main__":
    main()
