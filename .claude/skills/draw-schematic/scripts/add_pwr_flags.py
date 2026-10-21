#!/usr/bin/env python3
"""自动给 ERC 抱怨的 power 输入 net 加 PWR_FLAG。

根因链：
  1. circuit-synth 不会自动加 PWR_FLAG
  2. fix_labels.py 还会顺手删所有 power 类符号（避免 #PWR 多重叠 short）
  → 每个项目都会出 `power_pin_not_driven` 错。

策略（v2）：把 PWR_FLAG **重叠放在该 net 的某个真实 pin 上**（用 ksa 拿精确
坐标）。两个 pin 物理重合 = KiCad 视为强连接，不需要 wire 也不需要 label。
PWR_FLAG.pin 是 power_out → 喂电给该 net → ERC 通过。

副作用：会触发 `pin_to_pin` warning（两 pin 同位），但 warning 非阻塞，可接受。

用法（模块）:
    from add_pwr_flags import add_pwr_flags_for_violations
    n, nets = add_pwr_flags_for_violations(sch_path, erc_data, component_nets)

或命令行:
    python add_pwr_flags.py <sch> <erc.json> <nets.json>
"""
import argparse
import json
import math
import random
import re
import sys
from pathlib import Path
from typing import Dict, List

import kicad_sch_api as ksa

# Register lib_external/components.kicad_sym so ksa lookups work for vendored
# parts. Must run before any ksa.get_symbol_info() call. Idempotent.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.register_ksa import register as _register_ksa  # noqa: E402
_register_ksa()


# PWR_FLAG 库文件位置（KiCad 自带）
_KICAD_POWER_LIB_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols/power.kicad_sym",
    "/usr/share/kicad/symbols/power.kicad_sym",
    "/usr/local/share/kicad/symbols/power.kicad_sym",
    r"C:\Program Files\KiCad\10.0\share\kicad\symbols\power.kicad_sym",
    r"F:\KiCad\share\kicad\symbols\power.kicad_sym",
]


def _find_power_lib() -> Path:
    for p in _KICAD_POWER_LIB_CANDIDATES:
        if Path(p).exists():
            return Path(p)
    raise FileNotFoundError("找不到 KiCad power.kicad_sym")


def _extract_pwr_flag_def() -> str:
    """从 power.kicad_sym 提 PWR_FLAG 完整定义块（括号深度匹配）。
    重命名 `(symbol "PWR_FLAG"` → `(symbol "power:PWR_FLAG"` 以匹配 sch 缓存格式。
    """
    text = _find_power_lib().read_text()
    i = text.index('(symbol "PWR_FLAG"')
    depth = 1
    j = i + len('(symbol ')
    while depth > 0 and j < len(text):
        if text[j] == '(':
            depth += 1
        elif text[j] == ')':
            depth -= 1
        j += 1
    block = text[i:j]
    # sch 的 lib_symbols 用 "library:Symbol" 格式
    return block.replace('(symbol "PWR_FLAG"', '(symbol "power:PWR_FLAG"', 1)


def _ensure_pwr_flag_in_lib_symbols(sch_text: str) -> str:
    """如果 sch 的 lib_symbols 段里没有 power:PWR_FLAG，注入它。"""
    if '"power:PWR_FLAG"' in sch_text and re.search(
            r'\(symbol\s+"power:PWR_FLAG"', sch_text):
        return sch_text  # 已经有了

    # 找 lib_symbols 块的闭合 )
    m = re.search(r'\(lib_symbols\b', sch_text)
    if not m:
        raise RuntimeError("sch 没有 lib_symbols 段")
    start = m.end()
    depth = 1
    i = start
    while i < len(sch_text) and depth > 0:
        if sch_text[i] == '(':
            depth += 1
        elif sch_text[i] == ')':
            depth -= 1
        i += 1
    # i 是闭合 ) 之后位置；插入到闭合 ) 之前
    pwr_block = _extract_pwr_flag_def()
    insert_at = i - 1
    return sch_text[:insert_at] + '\n\t\t' + pwr_block + '\n\t' + sch_text[insert_at:]


_TEMPLATE = '''  (symbol
    (lib_id "power:PWR_FLAG")
    (at {x:.2f} {y:.2f} 0)
    (unit 1)
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "{uuid_sym}")
    (property "Reference" "#FLG{idx:03d}" (at {x:.2f} {y_ref:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "PWR_FLAG" (at {x:.2f} {y_val:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at {x:.2f} {y:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (at {x:.2f} {y:.2f} 0)
      (effects (font (size 1.27 1.27)) hide))
    (pin "1" (uuid "{uuid_pin}"))
    (instances
      (project ""
        (path "/{sheet_uuid}" (reference "#FLG{idx:03d}") (unit 1))))
  )
'''

# ERC description 里 ref+pin 的提取（locale-agnostic）
# 兼容中/英/日/德/法等，因为 description 格式总是 "Symbol REF <pin关键字> NUMBER [...]"
# 例（中）"Symbol U1 引脚 1 [VDD1, 电源输入, 图线]"
# 例（英）"Symbol U1 Pin 1 [VDD1, Power input, Line]"
# 例（日）"Symbol U1 ピン 1 [VDD1, 電源入力, ライン]"
# 策略：抓 Symbol 后第一个 token = ref，再抓最后一个 [ 前的最后一个数字 = pin
_DESC_PAT = re.compile(r'Symbol\s+(\S+)\s+\S+\s+(\d+)\s*\[')


def _new_uuid() -> str:
    return ''.join(random.choices('0123456789abcdef', k=8)) + '-' + \
           '-'.join(''.join(random.choices('0123456789abcdef', k=k)) for k in [4, 4, 4, 12])


def _rotate(x: float, y: float, rot_deg: float) -> tuple[float, float]:
    a = math.radians(rot_deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def find_nets_to_flag(erc_data: dict, component_nets: dict) -> Dict[str, tuple[str, str]]:
    """从 ERC json 提取 power_pin_not_driven 涉及的 net。

    返回 {net_name: (ref, pin)} —— 每个 net 一个代表 pin（ERC 报告的那个）。
    后面用这个 (ref, pin) 拿精确坐标，把 PWR_FLAG 放上去。
    """
    nets: Dict[str, tuple[str, str]] = {}
    for sheet in erc_data.get("sheets", []):
        for v in sheet.get("violations", []):
            if v.get("type") != "power_pin_not_driven":
                continue
            for item in v.get("items", []):
                m = _DESC_PAT.search(item.get("description", ""))
                if not m:
                    continue
                ref, pin = m.group(1), m.group(2)
                pins_for_ref = component_nets.get(ref, {})
                net = pins_for_ref.get(str(pin)) or pins_for_ref.get(pin)
                if net and net not in nets:
                    nets[net] = (ref, str(pin))
    return nets


def _pin_position_in_sch(sch, ref: str, pin_num: str) -> tuple[float, float] | None:
    """用 ksa 拿 sch 中某个 (ref, pin) 的实际坐标（mm）。"""
    for comp in sch.components:
        if comp.reference != ref:
            continue
        sym = ksa.get_symbol_info(comp.lib_id)
        if not sym:
            return None
        pin = sym.get_pin(str(pin_num))
        if not pin:
            return None
        px, py, prot = comp.position.x, comp.position.y, comp.rotation
        lx, ly = pin.position.x, pin.position.y
        rx, ry = _rotate(lx, ly, prot)
        # ⚠ KiCad sch 坐标 Y 翻转：real_y = placed.y - lib.y
        return (px + rx, py - ry)
    return None


def add_pwr_flags_to_sch(
    sch_path: Path, net_to_pin: Dict[str, tuple[str, str]]
) -> List[str]:
    """给每个 net 加一个 PWR_FLAG，放在该 net 的代表 pin 上（物理重叠）。"""
    if not net_to_pin:
        return []

    sch = ksa.load_schematic(str(sch_path))
    text = sch_path.read_text().rstrip()
    if not text.endswith(')'):
        raise RuntimeError(f"sch 末尾不是 ')': {sch_path}")

    # 关键：先在 lib_symbols 段注入 PWR_FLAG 定义（否则 KiCad 把 instance
    # 当 broken symbol 跳过，ERC 永远不会消除 power_pin_not_driven）
    text = _ensure_pwr_flag_in_lib_symbols(text)

    # 提 root sheet UUID（每个真元件 instances 用的 path "/<uuid>"）
    m = re.search(r'^\s*\(uuid\s+"([0-9a-f-]+)"\)', text, flags=re.MULTILINE)
    if not m:
        raise RuntimeError(f"sch 找不到 sheet uuid: {sch_path}")
    sheet_uuid = m.group(1)

    blocks: list[str] = []
    placed: list[str] = []
    for i, (net_name, (ref, pin_num)) in enumerate(sorted(net_to_pin.items())):
        pos = _pin_position_in_sch(sch, ref, pin_num)
        if pos is None:
            print(f"⚠ 找不到 {ref}.{pin_num} 的坐标，跳过 net {net_name}", file=sys.stderr)
            continue
        x, y = pos
        # snap 到 1.27mm grid（防止浮点尾数）
        x = round(x / 1.27) * 1.27
        y = round(y / 1.27) * 1.27
        blocks.append(_TEMPLATE.format(
            x=x, y=y,
            y_ref=y - 5.08,
            y_val=y - 2.54,
            idx=i + 1,
            uuid_sym=_new_uuid(),
            uuid_pin=_new_uuid(),
            sheet_uuid=sheet_uuid,
        ))
        placed.append(net_name)

    text = text[:-1] + '\n' + ''.join(blocks) + ')\n'
    sch_path.write_text(text)
    return placed


def add_pwr_flags_for_violations(
    sch_path: Path, erc_data: dict, component_nets: dict
) -> tuple[int, list[str]]:
    """主入口：分析 ERC violations，自动加 PWR_FLAG。

    返回 (count_added, list_of_nets)。
    """
    net_to_pin = find_nets_to_flag(erc_data, component_nets)
    if not net_to_pin:
        return 0, []
    placed = add_pwr_flags_to_sch(sch_path, net_to_pin)
    return len(placed), placed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sch", help="路径到 .kicad_sch")
    ap.add_argument("erc_json", help="kicad-cli sch erc --format json 输出")
    ap.add_argument("nets_json", help="JSON 含 COMPONENT_NETS 映射")
    args = ap.parse_args()

    sch_path = Path(args.sch)
    erc_data = json.loads(Path(args.erc_json).read_text())
    nets = json.loads(Path(args.nets_json).read_text())
    n, flagged = add_pwr_flags_for_violations(sch_path, erc_data, nets)
    if n == 0:
        print("无 power_pin_not_driven 错，未加 PWR_FLAG")
    else:
        print(f"✅ 加了 {n} 个 PWR_FLAG: {', '.join(flagged)}")


if __name__ == "__main__":
    main()
