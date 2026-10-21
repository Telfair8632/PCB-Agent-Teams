#!/usr/bin/env python3
"""check_footprint_sanity.py — 封装库数据体检闸门(Phase A, 与 check_chirality 并列)。

转换库(立创/easyeda/Altium 导出、脚本自转)常带四种数据病, 病灶在库文件里,
越晚发现返工越大:

  drill_exceeds_pad  钻孔比铜箔宽(任一轴) — 环宽为负, 钻头直接吃掉焊盘铜,
                     贴片/插件后该脚虚焊。KiCad DRC 后期会报 padstack 类
                     warning, 但那时布局布线已经做完。
  zero_annular       PTH 焊盘 pad size ≈ drill(环宽 < 0.05mm) — 常见于
                     安装孔被画成"无铜环 PTH"(该用 NPTH 或加环)。DRC 后期
                     报 annular_width error, 同样太晚。np_thru_hole 豁免:
                     pad size == drill 正是 NPTH 的标准画法。
  no_courtyard       封装没有任何 CrtYd 图元 — check_placement 的重叠闸门
                     退级到图元 bbox, 且 **KiCad DRC 的 courtyard 检查同样
                     依赖该层, 整库缺失时 DRC 对器件重叠全盲**(这条 DRC
                     永远不会替你报)。
  degenerate_courtyard  CrtYd 存在但 bbox 某轴 < 1mm(画成一条线) — 同样
                     让重叠检测失真。

用法:
  check_footprint_sanity.py <board.kicad_pcb>   # 查板上全部封装(按库名去重)
  check_footprint_sanity.py <dir.pretty>        # 查整个封装库

输出 stdout JSON。exit 0 = 干净或仅 courtyard 类问题, 2 = padstack 类
(drill_exceeds_pad / zero_annular)非空 —— 那是库数据错误, 修库再摆件。
no_courtyard 不算 hard fail(太常见), 但要知道后果: 重叠防线只剩
check_placement 的图元 bbox 回退, `extent_source` 里 `pad_bbox > 0` 的件
必须渲染目检(见 references/known_issues.md courtyard 条)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 环宽低于此值视为"事实上没有环"(区分数据错误与正常小环; fab 工艺最小环
# 宽远大于它, 真边缘案例交 DRC 的 annular_width 判)
ZERO_ANNULAR_MM = 0.05
# courtyard bbox 任一轴低于此值视为退化(与 get_geometry._DEGENERATE_MM 同值)
DEGENERATE_MM = 1.0

PAD_HEAD = re.compile(r'\(pad\s+"([^"]*)"\s+(\w+)\s+(\w+)')
SIZE = re.compile(r'\(size\s+([\d.]+)\s+([\d.]+)\)')
DRILL = re.compile(r'\(drill\s+(oval\s+)?([\d.]+)(?:\s+([\d.]+))?')
GRAPHIC = re.compile(r'\(fp_(?:line|rect|circle|poly|arc)\b')
COORD = re.compile(r'\((?:start|end|center|mid|xy)\s+(-?[\d.]+)\s+(-?[\d.]+)')


def _balanced(text: str, start: int) -> str:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def check_footprint(text: str) -> dict:
    """单个封装文本 → 各检查结果。坐标全在库帧, 无需旋转变换。"""
    bad_drill: list[dict] = []
    zero_ann: list[dict] = []
    for m in PAD_HEAD.finditer(text):
        num, ptype = m.group(1), m.group(2)
        if ptype == "np_thru_hole":
            continue                      # NPTH: size == drill 是标准画法
        block = _balanced(text, m.start())
        dm = DRILL.search(block)
        if not dm:
            continue                      # SMD 无钻孔
        sm = SIZE.search(block)
        if not sm:
            continue
        sx, sy = float(sm.group(1)), float(sm.group(2))
        dx = float(dm.group(2))
        dy = float(dm.group(3)) if dm.group(3) else dx
        ann = min((sx - dx) / 2, (sy - dy) / 2)
        if ann < -1e-3:
            bad_drill.append({"pad": num, "ring_mm": round(ann, 3)})
        elif ann < ZERO_ANNULAR_MM:
            zero_ann.append({"pad": num, "ring_mm": round(ann, 3)})

    xs: list[float] = []
    ys: list[float] = []
    for g in GRAPHIC.finditer(text):
        block = _balanced(text, g.start())
        if "CrtYd" not in block:
            continue
        for c in COORD.finditer(block):
            xs.append(float(c.group(1)))
            ys.append(float(c.group(2)))
    if not xs:
        courtyard = "missing"
    elif (max(xs) - min(xs)) < DEGENERATE_MM or (max(ys) - min(ys)) < DEGENERATE_MM:
        courtyard = "degenerate"
    else:
        courtyard = "ok"

    return {"drill_exceeds_pad": bad_drill, "zero_annular": zero_ann,
            "courtyard": courtyard}


def _iter_board_footprints(text: str):
    """yield (lib_name, ref, block) — 括号配平截块。"""
    for m in re.finditer(r'\(footprint\s+"([^"]*)"', text):
        block = _balanced(text, m.start())
        rm = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', block)
        yield m.group(1), (rm.group(1) if rm else "?"), block


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    target = Path(sys.argv[1])

    # {lib_name: (result, [refs])} — 病灶在库文件, 按库名去重报告
    per_fp: dict[str, tuple[dict, list[str]]] = {}
    if target.is_dir():
        for f in sorted(target.glob("*.kicad_mod")):
            per_fp[f.stem] = (check_footprint(f.read_text(errors="replace", encoding="utf-8")), [])
    else:
        text = target.read_text(errors="replace", encoding="utf-8")
        for name, ref, block in _iter_board_footprints(text):
            short = name.split(":")[-1]
            if short in per_fp:
                per_fp[short][1].append(ref)
            else:
                per_fp[short] = (check_footprint(block), [ref])

    out = {"target": str(target), "checked": len(per_fp),
           "drill_exceeds_pad": [], "zero_annular": [],
           "no_courtyard": [], "degenerate_courtyard": []}
    for name, (r, refs) in per_fp.items():
        loc = {"footprint": name, **({"refs": sorted(refs)} if refs else {})}
        if r["drill_exceeds_pad"]:
            out["drill_exceeds_pad"].append({**loc, "pads": r["drill_exceeds_pad"]})
        if r["zero_annular"]:
            out["zero_annular"].append({**loc, "pads": r["zero_annular"]})
        if r["courtyard"] == "missing":
            out["no_courtyard"].append(name)
        elif r["courtyard"] == "degenerate":
            out["degenerate_courtyard"].append(name)

    out["hard_fail"] = bool(out["drill_exceeds_pad"] or out["zero_annular"])
    n_nocy = len(out["no_courtyard"]) + len(out["degenerate_courtyard"])
    out["verdict"] = ("PADSTACK_BROKEN" if out["hard_fail"] else
                      "NO_COURTYARD" if n_nocy else "CLEAN")
    if n_nocy:
        out["courtyard_note"] = (
            f"{n_nocy}/{out['checked']} footprints have no usable courtyard — "
            "overlap detection falls back to graphics bbox AND KiCad's own "
            "courtyard DRC is blind for these; watch check_placement's "
            "extent_source and render-inspect any pad_bbox footprints")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 2 if out["hard_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
