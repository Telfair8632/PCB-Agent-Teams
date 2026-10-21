#!/usr/bin/env python3
"""L3 拓扑层验证：用 KiCad 网表跟 .py 源码逻辑对比，按子模块分组报告。

原理：
  1. 从 .py AST 提取 expected：每个元件 (ref, pin) → net_name
  2. 从生成的 .net（KiCad 网表）提取 actual：每个 (ref, pin) 实际属于哪个 net
  3. 不比 net 名（KiCad 可能加 /Subsheet/ 前缀），比 **每个 net 的 (ref, pin) 集合等价**
  4. 按 .py 的 @circuit 函数（子模块）分组，逐个子模块报告

输出：
  {
    "ok": true/false,
    "submodules": {
      "HV_Input": {"ok": true, "matched_nets": 7, "mismatches": []},
      "Isolator": {...},
      ...
    },
    "summary": "5/5 子模块拓扑一致"
  }

用法:
    python verify_topology.py <project.py>
"""
import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
if not Path(KICAD_CLI).exists():
    KICAD_CLI = shutil.which("kicad-cli") or ""
if not KICAD_CLI:
    import glob as _glob
    for _pat in (r"C:\Program Files\KiCad\*\bin\kicad-cli.exe",
                 r"F:\KiCad\*\bin\kicad-cli.exe",
                 r"F:\KiCad\bin\kicad-cli.exe"):
        _hits = sorted(_glob.glob(_pat))
        if _hits:
            KICAD_CLI = _hits[-1]
            break


# ============================================================
# AST 解析：提取 expected COMPONENT_NETS + 子模块归属
# ============================================================

def parse_py_topology(py_file: Path) -> tuple[dict, dict]:
    """返回 (expected_nets, ref_to_subcircuit)
    - expected_nets: {ref: {pin: net_name}}
    - ref_to_subcircuit: {ref: subcircuit_name}
    """
    text = py_file.read_text()
    tree = ast.parse(text)

    expected: dict[str, dict[str, str]] = {}
    ref_to_sub: dict[str, str] = {}

    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        # 找带 @circuit 装饰器的函数
        is_circuit = False
        sub_name = func.name
        for dec in func.decorator_list:
            if isinstance(dec, ast.Call):
                fn = dec.func
                if (isinstance(fn, ast.Name) and fn.id == "circuit") or \
                   (isinstance(fn, ast.Attribute) and fn.attr == "circuit"):
                    is_circuit = True
                    for kw in dec.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                            sub_name = kw.value.value
            elif isinstance(dec, ast.Name) and dec.id == "circuit":
                is_circuit = True
        if not is_circuit:
            continue

        # 解析函数体
        var_to_ref = {}
        var_to_net = {}

        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                var = target.id
                if not isinstance(node.value, ast.Call):
                    continue
                f = node.value.func
                fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                if fname == "Component":
                    for kw in node.value.keywords:
                        if kw.arg == "ref" and isinstance(kw.value, ast.Constant):
                            var_to_ref[var] = kw.value.value
                            ref_to_sub[kw.value.value] = sub_name
                elif fname == "Net":
                    if node.value.args and isinstance(node.value.args[0], ast.Constant):
                        var_to_net[var] = node.value.args[0].value

        # 函数参数也是 net（subcircuit 入参 = 父 net 传进来）
        for arg in func.args.args:
            var_to_net[arg.arg] = arg.arg  # 占位：等顶层调用时再 resolve

        # 找 r1[1] += net
        for node in ast.walk(func):
            if not isinstance(node, ast.AugAssign):
                continue
            if not isinstance(node.target, ast.Subscript):
                continue
            sub = node.target
            if not isinstance(sub.value, ast.Name):
                continue
            var = sub.value.id
            if var not in var_to_ref:
                continue
            ref = var_to_ref[var]
            if isinstance(sub.slice, ast.Constant):
                pin = str(sub.slice.value)
            else:
                continue
            if isinstance(node.value, ast.Name):
                rhs = node.value.id
                net_name = var_to_net.get(rhs, rhs)
                expected.setdefault(ref, {})[pin] = net_name

    # 现在 expected 里有些 net_name 是参数名（如 "HV_pos"），需要 resolve 到顶层调用
    # 找顶层 @circuit 函数（参数最少 / 调用其他子电路）
    top_func = None
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        for dec in func.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "circuit") or \
               (isinstance(dec, ast.Name) and dec.id == "circuit"):
                # 看函数体里有没有调用其他 @circuit 函数
                for n in ast.walk(func):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                        # 调用了别的函数（不是 Component/Net）→ 可能是顶层
                        if n.func.id not in ("Component", "Net", "circuit"):
                            top_func = func
                            break
                if top_func:
                    break
        if top_func:
            break

    if top_func:
        # 顶层里 Net("...") 赋值
        top_var_to_net = {}
        for node in ast.walk(top_func):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                    f = node.value.func
                    fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                    if fname == "Net":
                        if node.value.args and isinstance(node.value.args[0], ast.Constant):
                            top_var_to_net[target.id] = node.value.args[0].value

        # 找子函数调用：sub_name(arg1=top_net, ...)，arg 名 → net 名 映射
        sub_call_args: dict[str, dict[str, str]] = {}
        for node in ast.walk(top_func):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            sub_call = node.func.id
            # 找该 sub 函数的形参名
            sub_func = next((f for f in ast.walk(tree)
                             if isinstance(f, ast.FunctionDef) and f.name == sub_call), None)
            if not sub_func:
                continue
            param_names = [a.arg for a in sub_func.args.args]
            arg_map = {}
            for i, a in enumerate(node.args):
                if isinstance(a, ast.Name) and a.id in top_var_to_net:
                    arg_map[param_names[i]] = top_var_to_net[a.id]
            for kw in node.keywords:
                if isinstance(kw.value, ast.Name) and kw.value.id in top_var_to_net:
                    arg_map[kw.arg] = top_var_to_net[kw.value.id]
            sub_call_args[sub_call] = arg_map

        # 用 sub_call_args resolve expected 里的参数名
        for ref, pinmap in expected.items():
            sub_name = ref_to_sub.get(ref)
            if not sub_name:
                continue
            # sub_name 是 @circuit name=...，需要找对应的 def 名字
            def_name = None
            for f in ast.walk(tree):
                if not isinstance(f, ast.FunctionDef):
                    continue
                for dec in f.decorator_list:
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and kw.value.value == sub_name:
                                def_name = f.name
                if def_name:
                    break
            if def_name and def_name in sub_call_args:
                arg_map = sub_call_args[def_name]
                for pin, net in list(pinmap.items()):
                    if net in arg_map:
                        pinmap[pin] = arg_map[net]

    return expected, ref_to_sub


# ============================================================
# 网表解析：提取 actual {ref: {pin: net_name}}
# ============================================================

def export_netlist(top_sch: Path) -> Path:
    """跑 kicad-cli 出 KiCad 格式 netlist。"""
    out = top_sch.with_suffix(".net")
    cmd = [KICAD_CLI, "sch", "export", "netlist", "-o", str(out), str(top_sch)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if not out.exists():
        raise RuntimeError(f"netlist 没生成: {result.stderr}")
    return out


def parse_netlist(net_file: Path) -> dict[str, dict[str, str]]:
    """KiCad netlist 是 sexpression（多行格式）。提取 net → (ref, pin)。"""
    text = net_file.read_text()
    actual: dict[str, dict[str, str]] = defaultdict(dict)

    # 找 (nets ... ) 段落，里面才有 (net (code ...) (name ...) (node ...) ...)
    nets_match = re.search(r'\(nets\s+(.*?)\n\t\)\s*\)', text, re.DOTALL)
    if not nets_match:
        return {}
    nets_block = nets_match.group(1)

    # 用括号深度匹配，逐个抽出每个 (net ...) 子段
    i = 0
    while i < len(nets_block):
        if nets_block[i:i+5] == "(net\n" or nets_block[i:i+5] == "(net ":
            # 找匹配的 )
            depth = 1
            start = i + 1
            j = i + 4  # 跳过 "(net"
            while j < len(nets_block) and depth > 0:
                if nets_block[j] == '(':
                    depth += 1
                elif nets_block[j] == ')':
                    depth -= 1
                j += 1
            block = nets_block[start:j-1]

            # 提 name
            nm = re.search(r'\(name\s+"([^"]*)"\)', block)
            if not nm:
                i = j
                continue
            net_name = nm.group(1)
            # 提所有 (node (ref "X") (pin "Y") ...)
            for nodem in re.finditer(
                r'\(node\s+\(ref\s+"([^"]+)"\)\s+\(pin\s+"([^"]+)"\)',
                block, re.DOTALL
            ):
                ref, pin = nodem.group(1), nodem.group(2)
                actual[ref][pin] = net_name
            i = j
        else:
            i += 1

    return dict(actual)


# ============================================================
# 比对：按子模块分组
# ============================================================

def normalize_net_name(name: str) -> str:
    """KiCad 加 /Subsheet/ 前缀，去掉。"""
    return name.split("/")[-1].lstrip("/")


def verify(expected: dict, actual: dict, ref_to_sub: dict) -> dict:
    """按子模块分组，对每个 (ref, pin) 验证 net 是否一致（按 net 等价类）。

    等价类比较：构造 expected 和 actual 的 net → set((ref, pin)) 映射，
    对每个 expected net，找 actual 里有相同 set 的 net。
    """
    # 构造 expected 的 net → set
    exp_groups: dict[str, frozenset] = {}
    grouper: dict[str, set] = defaultdict(set)
    for ref, pinmap in expected.items():
        for pin, net in pinmap.items():
            grouper[net].add((ref, pin))
    exp_groups = {k: frozenset(v) for k, v in grouper.items()}

    # 构造 actual 的 net → set（normalize 后）
    act_groups: dict[str, set] = defaultdict(set)
    for ref, pinmap in actual.items():
        for pin, net in pinmap.items():
            act_groups[normalize_net_name(net)].add((ref, pin))
    act_groups = {k: frozenset(v) for k, v in act_groups.items()}

    # 找每个 expected net 在 actual 里的等价 set
    submodules: dict[str, dict] = defaultdict(lambda: {"ok": True, "matched_nets": 0, "mismatches": []})

    # 倒排：(ref, pin) → expected net_name
    pin_to_exp_net: dict[tuple, str] = {}
    for net, pins in exp_groups.items():
        for ref, pin in pins:
            pin_to_exp_net[(ref, pin)] = net

    # 倒排：(ref, pin) → actual net_name
    pin_to_act_net: dict[tuple, str] = {}
    for net, pins in act_groups.items():
        for ref, pin in pins:
            pin_to_act_net[(ref, pin)] = net

    # 找出每个 ref 的子模块
    sub_pins: dict[str, list] = defaultdict(list)
    for ref, sub in ref_to_sub.items():
        for pin in expected.get(ref, {}):
            sub_pins[sub].append((ref, pin))

    # 对每个子模块，验证
    for sub_name, pins in sub_pins.items():
        # 该子模块涉及的 expected nets
        exp_nets_in_sub = {pin_to_exp_net[(r, p)] for r, p in pins if (r, p) in pin_to_exp_net}
        for exp_net in exp_nets_in_sub:
            exp_set = exp_groups[exp_net]
            # 在 actual 里找等价 set
            matched_act = None
            for act_net, act_set in act_groups.items():
                if act_set == exp_set:
                    matched_act = act_net
                    break
            if matched_act:
                submodules[sub_name]["matched_nets"] += 1
            else:
                # 找最接近的 actual set（diff 最小）
                best_diff = None
                best_act_net = None
                best_missing = set()
                best_extra = set()
                for act_net, act_set in act_groups.items():
                    common = exp_set & act_set
                    if not common:
                        continue
                    missing = exp_set - act_set
                    extra = act_set - exp_set
                    diff_size = len(missing) + len(extra)
                    if best_diff is None or diff_size < best_diff:
                        best_diff = diff_size
                        best_act_net = act_net
                        best_missing = missing
                        best_extra = extra
                submodules[sub_name]["ok"] = False
                submodules[sub_name]["mismatches"].append({
                    "expected_net": exp_net,
                    "expected_pins": sorted([f"{r}/{p}" for r, p in exp_set]),
                    "best_actual_match": best_act_net,
                    "missing_in_actual": sorted([f"{r}/{p}" for r, p in best_missing]),
                    "extra_in_actual": sorted([f"{r}/{p}" for r, p in best_extra]),
                })

    return dict(submodules)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("py_file", help="circuit-synth Python 源文件")
    args = ap.parse_args()

    py_file = Path(args.py_file).resolve()
    proj_dir = py_file.parent

    print("=== L3 拓扑验证 ===\n")

    # 解析 .py
    expected, ref_to_sub = parse_py_topology(py_file)
    print(f"📋 .py 期望: {len(expected)} 元件，{sum(len(p) for p in expected.values())} pin 连接")
    print(f"   子模块: {sorted(set(ref_to_sub.values()))}")

    # 找顶层 sch
    pro_files = list(proj_dir.rglob("*.kicad_pro"))
    if not pro_files:
        sys.exit("❌ 没找到 .kicad_pro")
    top_sch = pro_files[0].with_suffix(".kicad_sch")
    if not top_sch.exists():
        sys.exit(f"❌ 顶层 sch 不存在: {top_sch}")

    # 出网表
    print(f"\n🔧 出网表: {top_sch.name}")
    net_file = export_netlist(top_sch)

    # 解析网表
    actual = parse_netlist(net_file)
    print(f"📊 网表实际: {len(actual)} 元件, {sum(len(p) for p in actual.values())} pin 连接")

    # 比对
    submodules = verify(expected, actual, ref_to_sub)

    # 报告
    all_ok = all(s["ok"] for s in submodules.values())
    print(f"\n=== 子模块拓扑报告 ===")
    for sub_name in sorted(submodules.keys()):
        info = submodules[sub_name]
        icon = "✅" if info["ok"] else "❌"
        print(f"\n{icon} {sub_name}")
        print(f"   匹配 {info['matched_nets']} 个 net")
        if info["mismatches"]:
            for mm in info["mismatches"]:
                print(f"   ❌ net '{mm['expected_net']}'")
                print(f"      期望: {mm['expected_pins']}")
                if mm['missing_in_actual']:
                    print(f"      网表缺: {mm['missing_in_actual']}")
                if mm['extra_in_actual']:
                    print(f"      网表多: {mm['extra_in_actual']}")

    print(f"\n=== 总结 ===")
    n_pass = sum(1 for s in submodules.values() if s["ok"])
    print(f"{n_pass}/{len(submodules)} 子模块拓扑一致")
    print(f"\n{'✅ L3 通过' if all_ok else '❌ L3 失败'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
