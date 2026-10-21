#!/usr/bin/env python3
"""修 circuit-synth 漏 pin label 的 bug（垂直 R/C 的 pin 2 没 label）。

原理：用 kicad-sch-api 拿到每个 pin 的精确坐标（含旋转 + Y 轴翻转），
对 COMPONENT_NETS 里每个 pin 强制加一个 local label。

用法（作为模块）:
    from fix_labels import fix_labels_for_sch
    fix_labels_for_sch(sch_path, component_nets)

或命令行:
    python fix_labels.py <sch> <nets_json>

nets_json 格式:
    {
        "R1": {"1": "HV+", "2": "HV_DIV1"},
        "C1": {"1": "HV_SENSE", "2": "HV_GND"},
        ...
    }

KiCad sch 坐标系：Y 轴翻转。real_y = placed.y - lib.y（减号）。
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
import math
import random
import re
import sys
from pathlib import Path

import kicad_sch_api as ksa

# Register lib_external/components.kicad_sym before any ksa lookup. Idempotent.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.register_ksa import register as _register_ksa  # noqa: E402
_register_ksa()


def new_uuid() -> str:
    return ''.join(random.choices('0123456789abcdef', k=8)) + '-' + \
           '-'.join(''.join(random.choices('0123456789abcdef', k=k)) for k in [4, 4, 4, 12])


def pin_screen_coord(
    px: float, py: float, prot: float, anchor_x: float, anchor_y: float
) -> tuple[float, float]:
    """元件 (px, py, prot) + lib pin (anchor_x, anchor_y) → 屏幕坐标。

    与 circuit-synth `_add_pin_level_net_labels` 完全一致：
    先 Y 翻转（lib +Y 朝上 vs sch +Y 朝下），再绕组件中心旋转。

      local_x = anchor_x
      local_y = -anchor_y                     # Y 翻转 in lib coord
      rx = local_x*cos(r) - local_y*sin(r)    # 标准旋转
      ry = local_x*sin(r) + local_y*cos(r)
      screen_x = px + rx
      screen_y = py + ry

    rot=0/180 时跟"先转再翻"等价；rot=90/270 时只有这种顺序对得上 KiCad 实际放
    置（早期版本写反了，电气没事是因为本仓项目全是 rot=0）。
    """
    r = math.radians(prot)
    c, s = math.cos(r), math.sin(r)
    local_x, local_y = anchor_x, -anchor_y
    rx = local_x * c - local_y * s
    ry = local_x * s + local_y * c
    return px + rx, py + ry


def fix_labels_for_sch(
    sch_path: Path,
    component_nets: dict[str, dict[str | int, str]],
    drop_hier_labels: bool = False,
) -> int:
    """对 sch_path 加 label 修 circuit-synth bug。返回加的 label 数。

    component_nets: {ref: {pin_num: net_name}}
    drop_hier_labels: 是否删除 (hierarchical_label ...)。默认 False，因为 sub-sch
        中的 hierarchical_label 是 sheet 端口，删了会断 cross-sheet 连接。
        旧版单图模式（circuit-synth 在主 sch 放假 hier_label）才传 True。
    """
    text = sch_path.read_text(encoding="utf-8")

    # Step 1: 删旧 label（必删，fix_labels 会重新放在精确 pin 坐标）。
    # 用深度计数匹配 (label ...) / (hierarchical_label ...) 整块——
    # circuit-synth 0.8.36 pretty-print 多行 + 嵌套 (effects (font (size ...))),
    # 旧 regex `\(effects[^)]*\)` 在嵌套括号上失效，会把旧 label 留下导致 net 合并。
    # 必须 quote-aware——`(property "..." "(label X)")` 这种字符串里的括号不能算。
    def remove_blocks_with_head(t: str, *heads: str) -> str:
        out = []
        i = 0
        n = len(t)
        in_string = False
        while i < n:
            ch = t[i]
            if in_string:
                if ch == '\\' and i + 1 < n:
                    out.append(ch)
                    out.append(t[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    in_string = False
                out.append(ch)
                i += 1
                continue
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue
            matched_head = None
            for h in heads:
                if t.startswith(h, i) and i + len(h) < n and t[i + len(h)] in ' \t\n':
                    matched_head = h
                    break
            if matched_head:
                depth = 1
                j = i + 1
                inner_in_string = False
                while j < n and depth > 0:
                    cj = t[j]
                    if inner_in_string:
                        if cj == '\\' and j + 1 < n:
                            j += 2
                            continue
                        if cj == '"':
                            inner_in_string = False
                        j += 1
                        continue
                    if cj == '"':
                        inner_in_string = True
                    elif cj == '(':
                        depth += 1
                    elif cj == ')':
                        depth -= 1
                    j += 1
                if j < n and t[j] == '\n':
                    j += 1
                i = j
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    heads = ['(label']
    if drop_hier_labels:
        heads.append('(hierarchical_label')
    text = remove_blocks_with_head(text, *heads)

    # Step 1b: 删 circuit-synth 自动放的 #PWR 电源符号（它在同一坐标放多个不同电源 → short）
    # 用括号深度匹配（多行 + 嵌套，regex 不可靠）
    def remove_power_symbols(t: str) -> str:
        out_chars = []
        i = 0
        while i < len(t):
            # 匹配模式 "(symbol\n\t\t(lib_id \"power:" 或 "(symbol\n  (lib_id \"power:"
            m = re.match(r'\(symbol\s+\(lib_id\s+"power:', t[i:])
            if m:
                # 括号深度匹配，找闭合 )
                depth = 1
                j = i + 1
                while j < len(t) and depth > 0:
                    if t[j] == '(':
                        depth += 1
                    elif t[j] == ')':
                        depth -= 1
                    j += 1
                # j 是闭合 ) 之后位置；跳过整个 power symbol
                i = j
                continue
            out_chars.append(t[i])
            i += 1
        return ''.join(out_chars)

    text = remove_power_symbols(text)

    sch_path.write_text(text, encoding="utf-8")

    # Step 2: 用 ksa 拿元件 + lib 精确 pin 位置
    sch = ksa.load_schematic(str(sch_path))
    new_labels = []
    new_nc = []  # (no_connect) for pins present in lib but absent in component_nets

    for comp in sch.components:
        ref = comp.reference
        if ref.startswith("#PWR"):
            continue
        sym = ksa.get_symbol_info(comp.lib_id)
        if not sym:
            print(f"  ⚠ {ref}: 找不到 lib_id={comp.lib_id}", file=sys.stderr)
            continue

        px, py, prot = comp.position.x, comp.position.y, comp.rotation
        mapped_pins = component_nets.get(ref, {})

        # 2a: 写已映射 pin 的 label
        for pin_num, net_name in mapped_pins.items():
            pin = sym.get_pin(str(pin_num))
            if not pin:
                for p in sym.pins:
                    if p.name == str(pin_num):
                        pin = p
                        break
            if not pin:
                print(f"  ⚠ {ref} pin {pin_num}: 库里没找到", file=sys.stderr)
                continue
            sx, sy = pin_screen_coord(px, py, prot, pin.position.x, pin.position.y)
            real_x = round(sx, 4)
            real_y = round(sy, 4)

            new_labels.append(
                f'  (label "{net_name}" (at {real_x} {real_y} 0) '
                f'(effects (font (size 1.27 1.27)) (justify left)) '
                f'(uuid "{new_uuid()}"))'
            )

        # 2b: 没映射的 pin 标 (no_connect) —— 抑制 ERC pin_not_connected。
        # 安全策略两层：
        #   (a) 类型门：只允许 PASSIVE / FREE / UNSPECIFIED / NO_CONNECT；其它类型
        #       禁止自动 NC（掩盖漏接的输出/电源是真 bug，留给 ERC 报）。
        #   (b) 名字兜底：LCSC easyeda2kicad 把所有 pin 标成 PASSIVE（导入丢类型），
        #       类型门对 LCSC 项目失效。再加一道 pin name regex：VCC/VDD/VEE/VSS/
        #       GND/VIN/VOUT/VBUS 等电源/IO 名字一律拒 NC。
        if mapped_pins:
            mapped_keys = {str(k) for k in mapped_pins.keys()}
            try:
                from kicad_sch_api.core.types import PinType
                NC_SAFE = {PinType.PASSIVE, PinType.FREE,
                           PinType.UNSPECIFIED, PinType.NO_CONNECT}
            except Exception:
                NC_SAFE = None
            POWER_NAME_RE = re.compile(
                r'^(V[CDESMP]+|GND[A-Z0-9_]*|AGND|DGND|VBUS|VBAT|VIN[A-Z0-9_]*|VOUT[A-Z0-9_]*|VREF[A-Z0-9_]*|EN|RST|RESET|NRST|NRESET|CLK|MISO|MOSI|SCK|SCL|SDA|TX|RX|D\+|D\-)$',
                re.IGNORECASE,
            )
            for p in sym.pins:
                pid = str(p.number)
                if pid in mapped_keys or p.name in mapped_keys:
                    continue
                ptype = getattr(p, "pin_type", None)
                if NC_SAFE is not None and ptype is not None and ptype not in NC_SAFE:
                    print(f"  ⚠ {ref} pin {pid}({p.name}, {ptype}) 未映射且不是 NC 安全类型，"
                          f"留给 ERC 报", file=sys.stderr)
                    continue
                if p.name and POWER_NAME_RE.match(p.name):
                    print(f"  ⚠ {ref} pin {pid}('{p.name}') 名字像电源/IO，禁止自动 NC，"
                          f"留给 ERC 报", file=sys.stderr)
                    continue
                sx, sy = pin_screen_coord(px, py, prot, p.position.x, p.position.y)
                real_x = round(sx, 4)
                real_y = round(sy, 4)
                new_nc.append(
                    f'  (no_connect (at {real_x} {real_y}) '
                    f'(uuid "{new_uuid()}"))'
                )

    # Step 3: 插入 label + no_connect
    text = sch_path.read_text(encoding="utf-8").rstrip()
    if not text.endswith(')'):
        raise RuntimeError(f"sch 末尾不是 ')': {sch_path}")
    new_blocks = new_labels + new_nc
    text = text[:-1] + '\n' + '\n'.join(new_blocks) + '\n)\n'
    sch_path.write_text(text, encoding="utf-8")
    return len(new_labels)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sch", help="路径到 .kicad_sch")
    ap.add_argument("nets_json", help="JSON 文件含 COMPONENT_NETS 映射")
    ap.add_argument("--drop-hier-labels", action="store_true",
                    help="删除 (hierarchical_label ...) 块。仅旧版单图模式用；hierarchical 项目里 hier_label 是 sheet 端口，禁删。")
    args = ap.parse_args()

    sch_path = Path(args.sch)
    nets = json.loads(Path(args.nets_json).read_text(encoding="utf-8"))
    n = fix_labels_for_sch(sch_path, nets, drop_hier_labels=args.drop_hier_labels)
    print(f"✅ 加了 {n} 个 label 到 {sch_path}")


if __name__ == "__main__":
    main()
