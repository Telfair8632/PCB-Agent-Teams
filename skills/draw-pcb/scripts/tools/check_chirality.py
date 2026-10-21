#!/usr/bin/env python3
"""check_chirality.py — 封装手性(镜像)闸门。

镜像封装是 DRC 盲区: y 翻转保持一切距离不变, 间距 / 网长 / 布通率 / 覆铜
全部照常通过, 错误要到贴片后才暴露(非对称件对不上焊盘)。常见来源:
从库帧 y 朝上的 EDA(立创专业版 / Altium / Eagle)导入封装时漏翻 y,
或自绘封装时照着底视图画。

判据(在 KiCad 官方库全集 15430 个封装上标定, 2026-07):

  cw_serpentine(候选镜像, 逐件必须处置):
    双排蛇形编号 + 排内 pitch <= 2.8mm + 引脚 >= 6 的封装(SOIC / SOP /
    SSOP / DIP / ESOP 类几何签名, 不靠封装名), 引脚环 1..N 从正面看
    必须逆时针(JEDEC 约定)。顺时针 → 进这个名单。
    官方库标定的已知合法顺时针来源(所以不能无脑挡):
      - 名字带 Reverse / ClockwisePinNumbering 的故意反向变体
        (本工具按名自动豁免, 单列 suppressed_variants)
      - 显示模组 / 板对板连接器 / 模块电源等厂家编号件(官方库残留
        约 15 个, 占蛇形类 ~1%)
    pitch 上限是标定值: 变压器(pitch >= 4mm, 官方库 24 个合法顺时针)
    全部被它排除, 而 JEDEC 双排封装 pitch <= 2.54mm 无一漏判。

  hard_fail(exit 2)是**群体判据**, 不是单件:
    cw_serpentine >= 2 且 数量 > serpentine_ok(逆时针蛇形)。
    y 翻转是系统性的 — 镜像库里所有蛇形件同时反, 这正是它的签名;
    单件顺时针更可能是合法厂家编号, 列名单交回路处置, 不自动挡。

  cw_suspect(参考): 编号连续 >= 3、非蛇形、顺时针。连接器 / 开关的
    编号跟厂家触点, 无逆时针约定(官方库 ~12% 合法顺时针), 永不挡。

  背面封装(B.Cu)绕向要求相反(翻面本来就是镜像)。

用法:
  check_chirality.py <board.kicad_pcb>      # 查板上全部封装
  check_chirality.py <dir.pretty>           # 查整个封装库
输出: stdout JSON。exit 0 = 干净, 2 = hard_fail(群体判据成立)。

处置规则(回路侧, 见 references/loop.md):
  cw_serpentine 名单非空时, 逐件对照 datasheet / 官方同款定性;
  确认是合法厂家编号才可继续, 理由要落在可见输出里。
  hard_fail = 来源库整体镜像, 修库重转, 不要在板上改封装。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 双排识别阈值(官方库标定): 排间距 >= 3x 排内散布, 且 >= 0.5mm
ROW_GAP_RATIO = 3.0
ROW_GAP_MIN_MM = 0.5
# 排内相邻引脚 pitch 上限: JEDEC 双排封装 <= 2.54mm; 变压器 / 大端子
# 模块 >= 4mm(官方库 24 个合法顺时针变压器全靠这条排除)
SERP_PITCH_MAX_MM = 2.8
# 引脚环面积 / bbox 面积低于此值视为退化(近共线), 不判
DEGENERATE_AREA_RATIO = 0.15
# 蛇形类最少引脚数(3~5 脚双排如 SOT-23 也符合签名, 但连接器偶然
# 符合的概率不可忽略, 降为 suspect)
HARD_MIN_PINS = 6
# 名字带这些词 = 厂家故意反向编号的变体, 自动豁免
VARIANT_NAME = re.compile(r"reverse|clockwise", re.I)

PAD_HEAD = re.compile(r'\(pad\s+"([^"]*)"\s+(\w+)\s+(\w+)')
AT = re.compile(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)')


def _pads_from_text(text: str) -> dict[int, tuple[float, float]]:
    """{pin_number: (x, y)}，同号多 pad 取质心(裂开的 EP)。非数字编号→跳过该 pad。"""
    acc: dict[int, list[tuple[float, float]]] = {}
    for m in PAD_HEAD.finditer(text):
        num = m.group(1)
        if not num.isdigit():
            continue
        at = AT.search(text, m.end(), m.end() + 160)
        if not at:
            continue
        acc.setdefault(int(num), []).append((float(at.group(1)), float(at.group(2))))
    return {n: (sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps))
            for n, ps in acc.items()}


def _shoelace(pts: list[tuple[float, float]]) -> float:
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return a / 2


def _dual_row_serpentine(pins: dict[int, tuple[float, float]]) -> bool:
    """双排蛇形编号: 引脚沿一排升序走到头, 掉头沿另一排降序走回。

    ESOP 类带中央 EP(最大号 pad 在质心附近)时, 剔掉 EP 再试一次。
    """
    ns = sorted(pins)
    if ns != list(range(1, len(ns) + 1)) or len(ns) < 4:
        return False
    pts = [pins[n] for n in ns]

    for axis in (0, 1):
        u = [p[axis] for p in pts]
        order = sorted(range(len(u)), key=lambda i: u[i])
        # 最大间隙劈成两排
        gaps = [(u[order[i + 1]] - u[order[i]], i) for i in range(len(order) - 1)]
        gap, cut = max(gaps)
        A = set(order[:cut + 1])
        B = set(order[cut + 1:])
        if len(A) != len(B):          # SOIC/DIP 两排永远等长
            continue
        spread = max(
            max(u[i] for i in A) - min(u[i] for i in A),
            max(u[i] for i in B) - min(u[i] for i in B),
        )
        if gap < ROW_GAP_MIN_MM or gap < ROW_GAP_RATIO * max(spread, 1e-9):
            continue
        v_axis = 1 - axis
        # 排内 pitch 上限(排除变压器 / 大端子模块)
        pitch_ok = True
        for row in (A, B):
            vs = sorted(pts[i][v_axis] for i in row)
            if len(vs) >= 2:
                steps = [vs[i + 1] - vs[i] for i in range(len(vs) - 1)]
                if sorted(steps)[len(steps) // 2] > SERP_PITCH_MAX_MM:
                    pitch_ok = False
        if not pitch_ok:
            continue
        row1 = sorted((i for i in (A if 0 in A else B)), key=lambda i: pts[i][v_axis])
        row2 = sorted((i for i in (B if 0 in A else A)), key=lambda i: pts[i][v_axis])
        k = len(row1)
        # row1 按 v 升序或降序恰为 1..k, row2 反方向恰为 k+1..2k
        for r1 in (row1, row1[::-1]):
            if [i + 1 for i in r1] != list(range(1, k + 1)):
                continue
            # 蛇形: row2 的方向与 row1 相反
            r2 = row2[::-1] if r1 == row1 else row2
            if [i + 1 for i in r2] == list(range(k + 1, 2 * k + 1)):
                return True
        continue

    # EP 重试: 最大号在其余引脚质心附近(相对 bbox 尺度)
    n = len(ns)
    if n >= 7:
        rest = {k: v for k, v in pins.items() if k != n}
        rpts = list(rest.values())
        cx = sum(p[0] for p in rpts) / len(rpts)
        cy = sum(p[1] for p in rpts) / len(rpts)
        ex, ey = pins[n]
        span = max(max(p[0] for p in rpts) - min(p[0] for p in rpts),
                   max(p[1] for p in rpts) - min(p[1] for p in rpts), 1e-9)
        if abs(ex - cx) < 0.25 * span and abs(ey - cy) < 0.25 * span:
            return _dual_row_serpentine(rest)
    return False


def judge(pins: dict[int, tuple[float, float]], front: bool,
          name: str = "") -> tuple[str, float]:
    """→ (verdict, signed_area)。verdict ∈ cw_serpentine / serpentine_ok /
    suppressed_variant / cw_suspect / ccw_ok / not_judgeable。
    signed_area 在 KiCad y 向下帧: 正面逆时针 = 负。"""
    ns = sorted(pins)
    if len(ns) < 3 or ns != list(range(1, len(ns) + 1)):
        return "not_judgeable", 0.0
    pts = [pins[n] for n in ns]
    a = _shoelace(pts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bbox = (max(xs) - min(xs)) * (max(ys) - min(ys))
    if bbox <= 0 or abs(a) / bbox < DEGENERATE_AREA_RATIO:
        return "not_judgeable", a
    ccw_from_front = (a < 0) if front else (a > 0)   # 背面翻面, 要求反过来
    serp = _dual_row_serpentine(pins) and len(ns) >= HARD_MIN_PINS
    if serp:
        if ccw_from_front:
            return "serpentine_ok", a
        if VARIANT_NAME.search(name):
            return "suppressed_variant", a
        return "cw_serpentine", a
    return ("ccw_ok" if ccw_from_front else "cw_suspect"), a


def _iter_board_footprints(text: str):
    """yield (name, layer, block_text) — 括号配平截块。"""
    for m in re.finditer(r'\(footprint\s+"([^"]*)"', text):
        depth = 0
        i = m.start()
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = text[m.start():i + 1]
        lm = re.search(r'\(layer\s+"([^"]*)"', block)
        yield m.group(1), (lm.group(1) if lm else "F.Cu"), block


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    target = Path(sys.argv[1])
    items = []          # (display_name, front, pads)
    if target.is_dir():
        for f in sorted(target.glob("*.kicad_mod")):
            items.append((f.stem, True, _pads_from_text(f.read_text(errors="replace", encoding="utf-8"))))
    else:
        text = target.read_text(errors="replace", encoding="utf-8")
        refs: dict[str, int] = {}
        for name, layer, block in _iter_board_footprints(text):
            rm = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', block)
            disp = f"{rm.group(1)}:{name.split(':')[-1]}" if rm else name
            items.append((disp, layer.startswith("F"), _pads_from_text(block)))
            refs[disp] = refs.get(disp, 0) + 1

    out = {"target": str(target), "checked": 0, "cw_serpentine": [],
           "suppressed_variants": [], "suspects": [], "serpentine_ok": 0,
           "ccw_ok": 0, "not_judgeable": 0}
    for disp, front, pads in items:
        verdict, a = judge(pads, front, name=disp)
        out["checked"] += 1
        if verdict == "cw_serpentine":
            out["cw_serpentine"].append({"footprint": disp, "pins": len(pads),
                                         "signed_area": round(a, 2), "front": front})
        elif verdict == "suppressed_variant":
            out["suppressed_variants"].append(disp)
        elif verdict == "cw_suspect":
            out["suspects"].append({"footprint": disp, "pins": len(pads),
                                    "signed_area": round(a, 2)})
        else:
            out[verdict] += 1
    # 群体判据: 镜像库的蛇形件全体同时反向; 单件顺时针更可能是厂家编号,
    # 列名单交回路逐件处置(loop.md), 不自动挡。
    n_cw = len(out["cw_serpentine"])
    out["hard_fail"] = n_cw >= 2 and n_cw > out["serpentine_ok"]
    out["verdict"] = ("MIRRORED_LIBRARY" if out["hard_fail"] else
                      "VERIFY_LISTED" if n_cw else "CLEAN")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 2 if out["hard_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
