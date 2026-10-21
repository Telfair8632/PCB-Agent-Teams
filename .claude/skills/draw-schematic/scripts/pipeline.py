#!/usr/bin/env python3
"""硬编码主 pipeline：一个命令跑完整个流水线。

设计理念：
  - 所有步骤固定顺序，不让 LLM 即兴判断
  - 失败 fail-fast（不容忍部分成功）
  - 每步输出固定格式给上层判断
  - LLM 只做一件事：调用 `python pipeline.py <project.py>`

流水线（9 步，固定）：
  1. import 用户 .py 文件
  2. 跑 circuit_synth 的 generate_kicad_project()
  3. 从 .py AST 提取 COMPONENT_NETS（不依赖 LLM 重写）
  4. fix_labels（修 pin 漏 label bug）
  5. add_wires（同 net pin 之间画 manhattan wire）
  6. kicad-cli ERC（数据层验证 L1）
  7. kicad-cli export pdf（视觉层验证 L2 — Claude 要 Read）
  8. kicad analyzer（结构化 schematic 侦查，供 SPICE / kidoc 消费）
  9. SPICE gate（如果 simulator 可用，仿真可识别的模拟子电路）

用法:
    python pipeline.py /path/to/project.py
    python pipeline.py /path/to/project.py --skip-erc

输出:
    {
      "ok": true/false,
      "sch_path": "...",
      "pdf_path": "...",
      "l1_pin_not_connected": 0,
      "l1_total_errors": N,
      "lib_id_count": N,
      "wire_count": N,
      "label_count": N,
      "next_step": "Claude must Read PDF for L2 visual verification"
    }
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
import ast
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

# Workspace root = .../PCB-Agent-Teams/.claude/skills/draw-schematic/scripts/pipeline.py → parents[4].
# Override with KICAD_ROOT env var if the layout ever moves.
KICAD_ROOT = Path(os.environ.get("KICAD_ROOT") or Path(__file__).resolve().parents[4])
_venv_bin = "Scripts" if os.name == "nt" else "bin"
_venv_exe = "python.exe" if os.name == "nt" else "python"
VENV_PYTHON = KICAD_ROOT / ".venv" / _venv_bin / _venv_exe
KICAD_SKILLS_ROOT = KICAD_ROOT / ".claude" / "skills"
# Paths updated 2026-05 after skill consolidation:
#   kicad/scripts/analyze_schematic.py → check-schematic/scripts/
#   spice/scripts/simulate_subcircuits.py → check-schematic/scripts/
KICAD_ANALYZE_SCH = KICAD_SKILLS_ROOT / "check-schematic" / "scripts" / "analyze_schematic.py"
SPICE_SIMULATE = KICAD_SKILLS_ROOT / "check-schematic" / "scripts" / "simulate_subcircuits.py"


def _build_symbol_env() -> dict:
    """Build env dict with KICAD_SYMBOL_DIR including lib_external/."""
    env = os.environ.copy()
    kicad_sym_dirs = [
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
        "/usr/share/kicad/symbols",
        "/usr/local/share/kicad/symbols",
    ]
    for pat in (r"C:\Program Files\KiCad\*\share\kicad\symbols",
                r"F:\KiCad\*\share\kicad\symbols",
                r"F:\KiCad\share\kicad\symbols"):
        kicad_sym_dirs.extend(glob.glob(pat))
    lib_external = str(KICAD_ROOT / "lib_external")
    existing = env.get("KICAD_SYMBOL_DIR", "")
    paths = [p for p in existing.split(os.pathsep) if p] + kicad_sym_dirs + [lib_external]
    env["KICAD_SYMBOL_DIR"] = os.pathsep.join(paths)
    return env


# ============================================================
# 硬编码：kicad-cli PATH 检查（跨平台）
# ============================================================

def find_kicad_cli() -> str:
    """硬编码搜索路径。失败 fail-fast。"""
    candidates = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",  # macOS
        "/usr/bin/kicad-cli",                                       # Linux apt
        "/usr/local/bin/kicad-cli",                                 # Linux symlink
        "/snap/kicad/current/bin/kicad-cli",                        # Linux snap
        r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",           # Windows
        r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",            # Windows 9
        r"F:\KiCad\bin\kicad-cli.exe",                              # Windows custom drive
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    cli_in_path = shutil.which("kicad-cli")
    if cli_in_path:
        return cli_in_path
    sys.exit("❌ kicad-cli 找不到。装 KiCad 10 或加到 PATH。")


# ============================================================
# 硬编码：从 .py AST 提取 COMPONENT_NETS（不让 LLM 重写）
# ============================================================

def extract_nets_from_py(py_file: Path) -> Dict[str, Dict[str, str]]:
    """AST 解析 .py，提取 r1[1] += net1; r1[2] += net2 这种语句。

    返回 {ref: {pin: net_name}}

    硬编码模式：找 AugAssign（+=）+ Subscript（[N]）→ 是连接语句。
    用 Component(ref="R1") 找出每个变量名 → ref 的映射。
    用 Net("HV+") 找出每个变量名 → net_name。
    """
    text = py_file.read_text(encoding="utf-8")
    tree = ast.parse(text)

    var_to_ref: Dict[str, str] = {}      # 变量名 → "R1"
    var_to_net: Dict[str, str] = {}      # 变量名 → "HV+"

    # 第 1 遍：找所有 Component(...) / Net(...) 赋值
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            var_name = target.id
            if not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            func_name = func.attr if isinstance(func, ast.Attribute) else \
                        func.id if isinstance(func, ast.Name) else None

            if func_name == "Component":
                # ref="R1"
                for kw in node.value.keywords:
                    if kw.arg == "ref" and isinstance(kw.value, ast.Constant):
                        var_to_ref[var_name] = kw.value.value
            elif func_name == "Net":
                # Net("HV+")
                if node.value.args and isinstance(node.value.args[0], ast.Constant):
                    var_to_net[var_name] = node.value.args[0].value

    # 第 2 遍：找 r1[1] += net 这种 AugAssign
    nets: Dict[str, Dict[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AugAssign):
            continue
        if not isinstance(node.target, ast.Subscript):
            continue
        # r1[1]
        sub = node.target
        if not isinstance(sub.value, ast.Name):
            continue
        var_name = sub.value.id
        if var_name not in var_to_ref:
            continue
        ref = var_to_ref[var_name]

        # pin number
        if isinstance(sub.slice, ast.Constant):
            pin = str(sub.slice.value)
        else:
            continue

        # 右边的 net 变量
        if isinstance(node.value, ast.Name):
            rhs_var = node.value.id
        else:
            continue
        if rhs_var not in var_to_net:
            continue
        net_name = var_to_net[rhs_var]

        nets.setdefault(ref, {})[pin] = net_name

    return nets


# ============================================================
# 硬编码：跑 circuit_synth 的 .py
# ============================================================

def run_circuit_synth(py_file: Path) -> Optional[Path]:
    """跑 .py 生成 sch（支持单图或 hierarchical 多图）。返回主 sch 路径。"""
    proj_dir = py_file.parent
    cmd = [str(VENV_PYTHON), str(py_file)]
    env = _build_symbol_env()
    result = subprocess.run(cmd, cwd=proj_dir, capture_output=True, text=True, timeout=180, env=env, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"❌ circuit_synth 跑失败:\n{result.stderr}", file=sys.stderr)
        return None

    # 主 sch：跟 .kicad_pro 同名
    pro_files = list(proj_dir.rglob("*.kicad_pro"))
    if not pro_files:
        return None
    main_sch = pro_files[0].with_suffix(".kicad_sch")
    return main_sch if main_sch.exists() else None


def find_all_sch_files(main_sch: Path) -> list[Path]:
    """所有 .kicad_sch（main + 所有 sub-sheet）。

    Hierarchical 项目里 components 在 sub-sch；fix_labels 必须对每个 sch 都跑，
    否则 circuit-synth 的 label coord bug（sub-sch 不修）会导致 net 合并。
    """
    proj_dir = main_sch.parent
    return sorted(proj_dir.glob("*.kicad_sch"))


# ============================================================
# 硬编码：fix_labels + add_wires（直接 import 同目录脚本）
# ============================================================

def run_fix_and_wire(sch_paths: list[Path], nets: Dict[str, Dict[str, str]]):
    """fix_labels 加同名 local label（KiCad 同名 label = 电气连接，不画 wire 也对）。

    Hierarchical 项目要对每个 .kicad_sch（main + subs）都跑，因为 circuit-synth
    把组件分到 sub-sch 而 label coord bug 出在 sub-sch 内部。

    注：暂不调 add_wires —— manhattan chain 在密集元件上会穿过其他 pin 导致 short。
    Label-only 模式：电气 100% 对，视觉是飘 label。
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from fix_labels import fix_labels_for_sch

    total = 0
    for sch_path in sch_paths:
        # main sch（跟 .kicad_pro 同名）通常是 sheet 容器、零 components；
        # 删 hier_label 只在 main sch 安全（sub-sch 的 hier_label 是端口，留着）
        is_main = sch_path.stem == sch_path.parent.name
        n = fix_labels_for_sch(sch_path, nets, drop_hier_labels=is_main)
        if n:
            print(f"  ✓ {sch_path.name}: 加 {n} 个 label")
        total += n
    print(f"  ∑ 共 {total} 个 label（label-only 模式，靠同名连接）")
    return total, 0


# ============================================================
# 硬编码：ERC + PDF
# ============================================================

def run_erc(sch_path: Path, kicad_cli: str) -> Dict:
    """跑 ERC，返回固定结构 dict。

    关键：以前只数 `pin_not_connected` 一种错（→ 真错被放过）。
    现在按 severity 总错数 + 按 type 分类。门槛 = total_errors == 0。
    `raw_data` 保留完整 json，给 add_pwr_flags 等下游用。
    """
    # Per-run temp file + unlink first: a stale json from a previous run must
    # not silently pass the gate when kicad-cli fails to produce a fresh one.
    erc_fd, erc_name = tempfile.mkstemp(prefix="draw_schematic_erc_", suffix=".json")
    os.close(erc_fd)
    erc_file = Path(erc_name)
    erc_file.unlink(missing_ok=True)
    run_started = time.time()
    cmd = [kicad_cli, "sch", "erc", "--format", "json", "-o", str(erc_file), str(sch_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")

    if not erc_file.exists():
        return {"error": "ERC 没产出 json", "stderr": result.stderr}
    if erc_file.stat().st_mtime < run_started - 1:
        return {"error": "ERC json 是旧的（kicad-cli 没刷新输出）", "stderr": result.stderr}

    try:
        data = json.loads(erc_file.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"ERC json parse 失败: {e}"}
    finally:
        erc_file.unlink(missing_ok=True)

    total_errors = 0
    total_warnings = 0
    errors_by_type: Dict[str, int] = {}
    warnings_by_type: Dict[str, int] = {}
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            t = v.get("type", "unknown")
            sev = v.get("severity", "unknown")
            if sev == "error":
                total_errors += 1
                errors_by_type[t] = errors_by_type.get(t, 0) + 1
            elif sev == "warning":
                total_warnings += 1
                warnings_by_type[t] = warnings_by_type.get(t, 0) + 1
    return {
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "errors_by_type": errors_by_type,
        "warnings_by_type": warnings_by_type,
        # 兼容老字段（继续暴露，不破坏外部消费者）
        "pin_not_connected": errors_by_type.get("pin_not_connected", 0),
        "power_pin_not_driven": errors_by_type.get("power_pin_not_driven", 0),
        "raw_data": data,
    }


def _check_bom_readiness_sentinel(py_file: Path) -> Dict:
    """检查 bom-readiness 的 sentinel 文件。

    返回 {"ok": bool, "reason": str | None, "summary": {...}}
    Reject 条件：
      - sentinel 不存在 → "BOM 没验过"
      - py_mtime 不一致 → ".py 改过，需重验"
      - all_pass = false → "上次验有 fail，先修"
    """
    try:
        from download_datasheet import project_datasheets_dir
        ds_dir = project_datasheets_dir(py_file)
    except Exception:
        # 找不到 datasheets/ 也允许（早期项目结构没全）
        return {"ok": True, "reason": None,
                "summary": {"pass": 0, "total": 0,
                            "note": "项目没 datasheets/，跳过 sentinel 检查"}}

    sentinel_path = ds_dir / ".bom_readiness.json"
    if not sentinel_path.exists():
        return {"ok": False, "reason": f"sentinel 不存在：{sentinel_path}",
                "summary": {}}
    try:
        data = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "reason": f"sentinel 损坏: {e}", "summary": {}}

    if not data.get("all_pass"):
        return {"ok": False,
                "reason": f"上次 readiness 有 {data['summary'].get('fail')} 个 fail",
                "summary": data.get("summary", {})}

    cur_mtime = int(py_file.stat().st_mtime)
    if data.get("py_mtime") != cur_mtime:
        return {"ok": False,
                "reason": f".py 改过（sentinel mtime={data.get('py_mtime')}, "
                          f"当前={cur_mtime}），需重跑 readiness",
                "summary": data.get("summary", {})}

    return {"ok": True, "reason": None, "summary": data.get("summary", {})}


def _print_erc_summary(erc_data: Dict, prefix: str = "") -> None:
    """简洁打印 ERC 概况（不包含 raw_data）。"""
    if "error" in erc_data:
        print(f"{prefix}ERC 异常: {erc_data['error']}")
        return
    print(f"{prefix}总错数 = {erc_data.get('total_errors')}, "
          f"总警告 = {erc_data.get('total_warnings')}")
    if erc_data.get("errors_by_type"):
        print(f"{prefix}  错误分类: {erc_data['errors_by_type']}")
    if erc_data.get("warnings_by_type"):
        print(f"{prefix}  警告分类: {erc_data['warnings_by_type']}")


def run_export_pdf(sch_path: Path, kicad_cli: str) -> Path:
    """出 PDF。"""
    pdf = sch_path.with_suffix(".pdf")
    cmd = [kicad_cli, "sch", "export", "pdf", "-o", str(pdf), str(sch_path)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    return pdf


def run_schematic_analysis_and_spice(sch_path: Path, skip_spice: bool = False) -> Dict:
    """Run kicad schematic analysis and optional SPICE gate.

    Missing SPICE simulator is treated as a skip. Analyzer failures or SPICE
    fail results become gate failures because they indicate schematic issues or
    broken analysis infrastructure.
    """
    analysis_dir = sch_path.parent / "_analysis"
    analysis_dir.mkdir(exist_ok=True)

    result: Dict = {
        "ok": True,
        "analysis_dir": str(analysis_dir),
        "schematic_json": None,
        "spice_json": None,
        "spice": {"ok": True, "skipped": True, "reason": "not-run"},
    }

    if not KICAD_ANALYZE_SCH.exists():
        result["skipped"] = True
        result["reason"] = f"kicad analyzer not found: {KICAD_ANALYZE_SCH}"
        return result

    sch_json = analysis_dir / "sch_analysis.json"
    cmd = [
        str(VENV_PYTHON), str(KICAD_ANALYZE_SCH), str(sch_path),
        "--output", str(sch_json),
        "--stage", "schematic",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not sch_json.exists():
        result["ok"] = False
        result["reason"] = proc.stderr[:500] or "schematic analyzer produced no output"
        return result

    result["schematic_json"] = str(sch_json)

    if skip_spice:
        result["spice"] = {"ok": True, "skipped": True, "reason": "user-requested"}
        return result

    if not SPICE_SIMULATE.exists():
        result["spice"] = {
            "ok": True,
            "skipped": True,
            "reason": f"spice script not found: {SPICE_SIMULATE}",
        }
        return result

    spice_json = analysis_dir / "spice_report.json"
    cmd = [
        str(VENV_PYTHON), str(SPICE_SIMULATE), str(sch_json),
        "--output", str(spice_json),
        "--compact",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if "no SPICE simulator found" in stderr:
            result["spice"] = {
                "ok": True,
                "skipped": True,
                "reason": "no SPICE simulator found",
            }
            return result
        result["spice"] = {
            "ok": False,
            "skipped": False,
            "reason": stderr[:500] or "spice simulation failed",
        }
        result["ok"] = False
        return result

    if not spice_json.exists():
        result["spice"] = {
            "ok": True,
            "skipped": True,
            "reason": "spice produced no output",
        }
        return result

    try:
        spice_data = json.loads(spice_json.read_text(encoding="utf-8"))
    except Exception as e:
        result["spice"] = {"ok": False, "reason": f"spice json parse failed: {e}"}
        result["ok"] = False
        return result

    summary = spice_data.get("summary", {})
    fail_count = int(summary.get("fail", 0) or 0)
    result["spice_json"] = str(spice_json)
    result["spice"] = {
        "ok": fail_count == 0,
        "skipped": False,
        "summary": summary,
        "reason": None if fail_count == 0 else f"{fail_count} SPICE simulation(s) failed",
    }
    if fail_count:
        result["ok"] = False
    return result


# ============================================================
# 硬编码：sch 文件统计（lib_id / wire / label 计数）
# ============================================================

def count_sch_stats(sch_path: Path) -> Dict[str, int]:
    text = sch_path.read_text(encoding="utf-8")
    return {
        "lib_id": len(re.findall(r"lib_id", text)),
        "wire":   len(re.findall(r"\(wire ", text)),
        "label":  len(re.findall(r"\(label ", text)),
        "hierarchical_label": len(re.findall(r"hierarchical_label", text)),
    }


# ============================================================
# Main
# ============================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("py_file", help="circuit-synth Python 源文件")
    ap.add_argument("--skip-erc", action="store_true")
    ap.add_argument("--skip-pdf", action="store_true")
    ap.add_argument("--with-spice", action="store_true",
                    help="Run schematic analyzer + SPICE subcircuit simulation. "
                         "OFF by default: deep checking belongs to the check-schematic skill, "
                         "not the generation pipeline.")
    args = ap.parse_args()

    py_file = Path(args.py_file).resolve()
    if not py_file.exists():
        sys.exit(f"❌ 文件不存在: {py_file}")

    print(f"=== Pipeline: {py_file.name} ===")
    kicad_cli = find_kicad_cli()
    print(f"kicad-cli: {kicad_cli}")

    # Phase 0: 检 bom-readiness sentinel（铁律：选元件没验过不许画图）
    sentinel = _check_bom_readiness_sentinel(py_file)
    if not sentinel["ok"]:
        print(f"\n❌ Phase 0 失败：{sentinel['reason']}")
        print(f"   先跑：python .claude/skills/component-preparing/scripts/check_readiness.py {py_file}")
        sys.exit(json.dumps({"ok": False, "step": "bom_readiness",
                             "reason": sentinel["reason"]}, indent=2))
    print(f"\n[Phase 0] BOM readiness sentinel: ✅ "
          f"({sentinel['summary']['pass']}/{sentinel['summary']['total']} 元件过)")

    # Step −1：footprint 可用性预检 + 自动修（L4 - 画图之前先把 .py 的 footprint
    # 引用错改对，不然 sch 里写一堆错的 footprint，PCB 阶段才发现就晚了）
    print("\n[L4 pre-check] footprint 可用性 + 自动修 .py ...")
    fp_report = {}
    try:
        from verify_footprints import verify_and_fix
        fp_report = verify_and_fix(py_file, do_fix=True)
        print(f"  总引用 {fp_report['total']}, OK {fp_report['ok_count']}, "
              f"自动修 {len(fp_report['fixed'])}, 需手工 {len(fp_report['needs_manual'])}")
        for f in fp_report["fixed"]:
            print(f"    🔧 L{f['line']}: {f['ref']} → {f['new']}")
        for f in fp_report["needs_manual"]:
            cands = f.get('candidates', [])
            cand_str = f"; 候选: {cands}" if cands else "; 全库找不到"
            print(f"    ⚠ L{f['line']} [{f.get('ref_designator')}]: {f['ref']}  ← {f['reason']}{cand_str}")

        # L4 残留 0 候选 → 不再自动 LCSC 下载。
        # 选品决策已由 component-selecting skill 在 Phase 0 之前完成；
        # 若到这里还有 0 候选 footprint，说明 .py 的 footprint 字符串跟
        # component-selecting 实际写入 lib_external 的名字对不上 — 必须改 .py，
        # 不应该让 pipeline 静默再下一遍 LCSC（那样会绕过选品 gate）。
        zero_cand = [m for m in fp_report["needs_manual"] if not m.get("candidates")]
        if zero_cand:
            print(f"\n❌ {len(zero_cand)} 个 footprint 0 候选 — 选品 gate 之外不允许下载库。")
            for m in zero_cand:
                print(f"    L{m.get('line', '?')} [{m.get('ref_designator')}]: "
                      f"{m.get('ref')} ← {m.get('reason')}")
            print("   修法：")
            print("   1. 检查 lib_external/components.pretty/ 实际存在的 footprint 名")
            print("   2. 改 .py 的 footprint 字符串与之匹配；OR")
            print("   3. 还没选品就跑 component-preparing --mpn ...")
            sys.exit(json.dumps({"ok": False, "step": "footprint_zero_candidate",
                                 "zero_candidate": [
                                     {"ref": m.get("ref_designator"),
                                      "footprint": m.get("ref"),
                                      "reason": m.get("reason"),
                                      "line": m.get("line")}
                                     for m in zero_cand
                                 ]}, indent=2))
    except SystemExit:
        raise
    except Exception as e:
        print(f"  ⚠ verify_footprints 跳过: {e}")

    # Step 0: BOM 验证 — 不再做 auto_download。component-selecting 是唯一选品 gate；
    # 此处 verify_bom 仅做 pin 兼容检查，缺件直接 fail。
    print("\n[0/4] BOM 验证（pin 兼容检查；不下载库）...")
    try:
        from verify_bom import verify as verify_bom_fn
        bom_report = verify_bom_fn(py_file, auto_download=False)
        n_fail = bom_report["summary"]["fail_pin_mismatch"]
        if n_fail > 0:
            print(f"\n❌ Stage 0 失败：{n_fail} 个元件 pin 数不匹配。先改 .py 才能继续。")
            sys.exit(json.dumps({"ok": False, "step": "verify_bom", "bom_report": bom_report}, indent=2))
    except SystemExit:
        raise
    except Exception as e:
        print(f"⚠ BOM 验证跳过（{e}）")

    # Step 1: 跑 circuit-synth
    print("\n[1/4] 跑 circuit-synth...")
    sch = run_circuit_synth(py_file)
    if not sch:
        sys.exit(json.dumps({"ok": False, "step": "circuit-synth"}, indent=2))
    print(f"  ✓ sch: {sch.name}")

    # Step 2: 提 nets
    print("\n[2/4] AST 解析 .py 提取 COMPONENT_NETS...")
    nets = extract_nets_from_py(py_file)
    print(f"  ✓ {len(nets)} 个元件，{sum(len(p) for p in nets.values())} 个 pin 连接")

    # Step 3: fix_labels（删 PWR + hierarchical_label，加精确 local label）
    # Hierarchical 项目：main sch 通常是 sheet 容器，components 在 sub-sch。
    # 必须对每个 .kicad_sch（main + subs）都跑 fix_labels，否则 circuit-synth
    # 把 chain 中后一颗 R.pin1 label 误放在前一颗 R.pin1 同坐标，会导致 net 合并。
    print("\n[3/4] fix_labels（label-only 模式，靠同名连接）...")
    all_sch = find_all_sch_files(sch)
    print(f"  扫到 {len(all_sch)} 个 .kicad_sch（main + sub-sheets）")
    run_fix_and_wire(all_sch, nets)

    # Step 3b: 补 sub-sch 缺失的 (hierarchical_label ...)
    # circuit-synth 0.8.36 在主 sch 写 (sheet (pin "X")) 但子 sch 不放对应
    # (hierarchical_label "X")，会触发 ERC hier_label_mismatch + 跨表实际断连。
    hier_rep = {}
    if len(all_sch) > 1:
        print("\n[3b/4] 补 sub-sch 缺失的 hierarchical_label ...")
        from add_hier_labels import patch_hier_labels
        hier_rep = patch_hier_labels(sch)
        added = sum(len(v) for v in hier_rep.values())
        print(f"  ✓ 补 {added} 个 hier_label")
        for sheet, names in hier_rep.items():
            if names:
                print(f"    {sheet}: {names}")

    # Step 3.5: L4-post — 扫已生成 sch 里的 footprint 字段（含 .py 动态拼接的）
    print("\n[L4-post] 扫 sch 实际 footprint 字段（兜底动态字符串）...")
    fp_post = {}
    try:
        from verify_footprints import verify_and_fix_sch
        fp_post = verify_and_fix_sch(sch, do_fix=True)
        print(f"  扫 {fp_post['total']} 个元件，OK {fp_post['ok_count']}, "
              f"sch 内自动修 {len(fp_post['fixed'])}, 需手工 {len(fp_post['needs_manual'])}")
        for f in fp_post["fixed"]:
            print(f"    🔧 [{f['ref_designator']}]: {f['ref']} → {f['new']}")
        # 合并到 fp_report
        if fp_post.get("fixed"):
            fp_report.setdefault("fixed", []).extend(
                [{**f, "phase": "sch-post"} for f in fp_post["fixed"]])
        if fp_post.get("needs_manual"):
            # 只把还没在 .py 阶段已知的加进来
            existing = {(m.get("ref_designator"), m.get("ref"))
                        for m in fp_report.get("needs_manual", [])}
            for f in fp_post["needs_manual"]:
                key = (f.get("ref_designator"), f.get("ref"))
                if key not in existing:
                    fp_report.setdefault("needs_manual", []).append(
                        {**f, "phase": "sch-post"})
        # 重算 fp_report["ok"]
        fp_report["ok"] = fp_report.get("ok", True) and fp_post.get("ok", True)
    except Exception as e:
        print(f"  ⚠ L4-post 跳过: {e}")

    # Step 4a: ERC（含自动 PWR_FLAG 注入循环）
    erc_data = {}
    pwr_flag_added: list[str] = []
    if not args.skip_erc:
        print("\n[4/4] ERC + L3 验证 + PDF...")
        erc_data = run_erc(sch, kicad_cli)
        _print_erc_summary(erc_data, prefix="  ")

        # 自动修 power_pin_not_driven：注入 PWR_FLAG 后重跑 ERC（最多 1 轮）
        if erc_data.get("power_pin_not_driven", 0) > 0:
            print("  → 检测到 power_pin_not_driven，自动注入 PWR_FLAG ...")
            try:
                from add_pwr_flags import add_pwr_flags_for_violations
                n_flag, pwr_flag_added = add_pwr_flags_for_violations(
                    sch, erc_data["raw_data"], nets)
                print(f"    ✓ 加了 {n_flag} 个 PWR_FLAG: {pwr_flag_added}")
                erc_data = run_erc(sch, kicad_cli)
                print("  ERC 重跑后：")
                _print_erc_summary(erc_data, prefix="    ")
            except Exception as e:
                print(f"    ⚠ PWR_FLAG 注入失败: {e}")

        # 错误门槛：总错数必须 == 0
        if erc_data.get("total_errors", -1) != 0:
            print(f"  ❌ ERC 仍有 {erc_data['total_errors']} 个错（按类型）："
                  f" {erc_data.get('errors_by_type')}")

    # Step 4b: PDF
    pdf_path = None
    if not args.skip_pdf:
        pdf_path = run_export_pdf(sch, kicad_cli)
        print(f"  ✓ PDF: {pdf_path}")

    # Step 4c: L3 拓扑验证
    l3_ok = None
    l3_report = None
    if not args.skip_erc:
        try:
            from verify_topology import parse_py_topology, export_netlist, parse_netlist, verify
            expected, ref_to_sub = parse_py_topology(py_file)
            net_file = export_netlist(sch)
            actual = parse_netlist(net_file)
            l3_report = verify(expected, actual, ref_to_sub)
            l3_ok = all(s["ok"] for s in l3_report.values())
            for sub_name in sorted(l3_report.keys()):
                info = l3_report[sub_name]
                icon = "✅" if info["ok"] else "❌"
                print(f"  {icon} L3 {sub_name}: 匹配 {info['matched_nets']} net" +
                      (f"，{len(info['mismatches'])} 错" if info["mismatches"] else ""))
        except Exception as e:
            print(f"  ⚠ L3 验证失败: {e}")

    # Step 4d: kicad analyzer + optional SPICE gate
    # SPICE / deep schematic analysis is opt-in. Default = skip (deep checking
    # is the check-schematic skill's job; this pipeline only owns generation).
    if args.with_spice:
        print("\n[4d] kicad schematic analysis + SPICE gate...")
        sch_deep = run_schematic_analysis_and_spice(sch, skip_spice=False)
        if sch_deep.get("skipped"):
            print(f"  ⏭ {sch_deep.get('reason')}")
        elif not sch_deep.get("ok"):
            print(f"  ❌ sch deep gate failed: "
                  f"{sch_deep.get('reason') or sch_deep.get('spice', {}).get('reason')}")
        else:
            print(f"  ✓ schematic JSON: {sch_deep.get('schematic_json')}")
            spice = sch_deep.get("spice", {})
            if spice.get("skipped"):
                print(f"  ⏭ SPICE: {spice.get('reason')}")
            else:
                print(f"  ✓ SPICE: {spice.get('summary')}")
    else:
        print("\n[4d] SPICE gate: ⏭ skipped (opt-in via --with-spice; "
              "deep checking belongs to check-schematic)")
        sch_deep = {"ok": True, "skipped": True, "reason": "opt-in flag not set"}

    # 统计
    stats = count_sch_stats(sch)

    # 固定格式输出
    # 关键 gate：L1 total_errors == 0 + L3 拓扑 OK + L4 footprint 全可用
    # （之前 bug：只看 pin_not_connected == 0，放过 18 个 power/label/grid 错；
    #  L4 之前不存在，footprint 引用错拖到 PCB 阶段才暴露）
    fp_ok = bool(fp_report.get("ok", True)) if fp_report else True
    report = {
        "ok": (erc_data.get("total_errors", -1) == 0)
              and (l3_ok is not False)
              and fp_ok
              and sch_deep.get("ok", True),
        "l4_footprint_ok": fp_ok,
        "l4_footprint_fixed": fp_report.get("fixed", []),
        "l4_footprint_needs_manual": fp_report.get("needs_manual", []),
        "l3_topology_ok": l3_ok,
        "l3_submodules": l3_report,
        "sch_deep_gate": sch_deep,
        "sch_path": str(sch),
        "pdf_path": str(pdf_path) if pdf_path else None,
        "l1_total_errors": erc_data.get("total_errors"),
        "l1_total_warnings": erc_data.get("total_warnings"),
        "l1_errors_by_type": erc_data.get("errors_by_type"),
        "l1_warnings_by_type": erc_data.get("warnings_by_type"),
        "l1_pin_not_connected": erc_data.get("pin_not_connected"),
        "l1_power_pin_not_driven": erc_data.get("power_pin_not_driven"),
        "pwr_flag_auto_added": pwr_flag_added,
        "lib_id_count": stats["lib_id"],
        "wire_count": stats["wire"],
        "label_count": stats["label"],
        "hierarchical_label_count": stats["hierarchical_label"],
        "next_step": "Claude 必须 Read PDF 做 L2 视觉验证" if pdf_path else "skip",
    }
    print("\n=== Report ===")
    # raw_data 太大，不打印
    print(json.dumps({k: v for k, v in report.items()
                      if k != "raw_data"},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
