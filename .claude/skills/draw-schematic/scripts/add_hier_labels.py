#!/usr/bin/env python3
"""补 sub-sch 缺失的 (hierarchical_label ...)。

circuit-synth 0.8.36 在主 sch 里写
  (sheet (property "Sheetfile" "<sub>.kicad_sch") (pin "X" bidirectional ...))
但在 <sub>.kicad_sch 里只写 (label "X")，不写 (hierarchical_label "X")。
KiCad ERC 因此报 `hier_label_mismatch`：sheet pin 找不到对应的 hier_label。

本脚本扫主 sch 的 (sheet ...) 块，对每个 sub-sch 把缺失的 (hierarchical_label ...)
追加进去。位置不影响电气（KiCad 按名匹配）；为了不撞元件，统一放在左上角递增 Y。

用法（作为模块）:
    from add_hier_labels import patch_hier_labels
    n = patch_hier_labels(main_sch_path)
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
import random
import re
from pathlib import Path


def _new_uuid() -> str:
    return ''.join(random.choices('0123456789abcdef', k=8)) + '-' + \
           '-'.join(''.join(random.choices('0123456789abcdef', k=k)) for k in [4, 4, 4, 12])


def _parse_balanced(text: str, start: int) -> tuple[str, int]:
    """从 text[start]=='(' 起，返回整块 + 块结束 index（exclusive）。"""
    assert text[start] == '('
    depth = 1
    j = start + 1
    n = len(text)
    while j < n and depth > 0:
        if text[j] == '(':
            depth += 1
        elif text[j] == ')':
            depth -= 1
        j += 1
    return text[start:j], j


def _extract_sheet_blocks(main_text: str) -> list[tuple[str, list[tuple[str, str, float, float]]]]:
    """从主 sch 提 (sheet ...) 块。

    返回 [(sheetfile, [(pin_name, shape, pin_x, pin_y), ...]), ...]
    """
    out = []
    i = 0
    n = len(main_text)
    while i < n:
        idx = main_text.find('(sheet', i)
        if idx == -1:
            break
        if idx + 6 >= n or main_text[idx + 6] not in ' \t\n':
            i = idx + 6
            continue
        block, end = _parse_balanced(main_text, idx)
        i = end

        m = re.search(r'\(property\s+"Sheetfile"\s+"([^"]+)"', block)
        if not m:
            continue
        sheet_file = m.group(1)

        # pins: (pin "name" shape ... (at X Y angle))
        # 用 _parse_balanced 准确切每个 (pin ...) 块
        pins = []
        j = 0
        while j < len(block):
            k = block.find('(pin "', j)
            if k == -1:
                break
            pblock, pend = _parse_balanced(block, k)
            j = pend
            head = re.match(r'\(pin\s+"([^"]+)"\s+(\w+)\s', pblock)
            atm = re.search(r'\(at\s+([\d.\-eE]+)\s+([\d.\-eE]+)', pblock)
            if head and atm:
                pins.append((head.group(1), head.group(2),
                             float(atm.group(1)), float(atm.group(2))))
        out.append((sheet_file, pins))
    return out


def _existing_hier_labels(text: str) -> set[str]:
    return set(re.findall(r'\(hierarchical_label\s+"([^"]+)"', text))


def _find_label_block(text: str, name: str) -> tuple[int, int, float, float] | None:
    """找第一个 (label "name" ...) 整块（多行 + 嵌套），返回 (start, end, x, y) 或 None。

    fix_labels 写出的 label 是单行格式 `(label "X" (at X Y 0) ...)`，但同一文件
    可能也存在 circuit-synth 多行格式（已被 fix_labels 删除，这里兜底）。
    """
    pattern = re.compile(r'\(label\s+"' + re.escape(name) + r'"\s+\(at\s+([\d.\-eE]+)\s+([\d.\-eE]+)')
    m = pattern.search(text)
    if not m:
        return None
    # 起点 = '(label' 的 '(' ；从那里做深度匹配
    start = m.start()
    depth = 1
    j = start + 1
    n = len(text)
    while j < n and depth > 0:
        if text[j] == '(':
            depth += 1
        elif text[j] == ')':
            depth -= 1
        j += 1
    return start, j, float(m.group(1)), float(m.group(2))


def patch_hier_labels(main_sch: Path) -> dict:
    """对主 sch 同目录的 sub-sch 补缺失的 hier_label，
    并在主 sch 的 sheet pin 坐标补 (label "X")（让同名跨表连通）。

    返回 {sub_file: [added_names], "_main_added": [names_added_on_main]}
    """
    main_text = main_sch.read_text(encoding="utf-8")
    sheets = _extract_sheet_blocks(main_text)
    proj_dir = main_sch.parent
    report: dict[str, list[str]] = {}

    # 主 sch：每个 sheet pin 拉一段短 wire 出来，wire 末端放 (label "name")，
    # 同名 label 跨子表自动连通。直接在 sheet pin 坐标放 label 不行——KiCad ERC
    # 把它判定为 label_dangling，必须连在 wire/pin 上。
    # 全部 sheet pin angle=0 指向右，wire 向右拉 STUB_LEN mm；wire 末端和 label
    # 坐标必须用同一 round（python float 加法漂会让 ERC unconnected_wire_endpoint）。
    # Idempotent：先把 sheet-pin → +STUB_LEN 的旧 stub wire 全删掉，再统一重写。
    # 不能只跳过已存在的 wire：fix_labels 会删 label 但不删 wire，第二轮 wire 在
    # label 不在 → label 永久丢失。
    STUB_LEN = 2.54

    def _coord(v: float) -> str:
        return f"{round(v, 4):g}"

    # 收集所有 sheet pin 的 (x_str, y_str, x2_str, y_str) 作为"我加过的 stub"指纹
    own_stubs: set[tuple[str, str, str, str]] = set()
    for sheet_file, pins in sheets:
        for name, _shape, x, y in pins:
            own_stubs.add((_coord(x), _coord(y), _coord(x + STUB_LEN), _coord(y)))

    # 删主 sch 中匹配指纹的旧 (wire ...) 块（深度计数 + quote 保护）
    def _strip_matching_wires(t: str) -> str:
        out = []
        i = 0
        n = len(t)
        in_string = False
        while i < n:
            ch = t[i]
            if in_string:
                if ch == '\\' and i + 1 < n:
                    out.append(ch); out.append(t[i + 1]); i += 2; continue
                if ch == '"':
                    in_string = False
                out.append(ch); i += 1; continue
            if ch == '"':
                in_string = True; out.append(ch); i += 1; continue
            if t.startswith('(wire', i) and i + 5 < n and t[i + 5] in ' \t\n':
                # 找匹配 )
                depth = 1
                j = i + 1
                inner_str = False
                while j < n and depth > 0:
                    cj = t[j]
                    if inner_str:
                        if cj == '\\' and j + 1 < n:
                            j += 2; continue
                        if cj == '"':
                            inner_str = False
                        j += 1; continue
                    if cj == '"':
                        inner_str = True
                    elif cj == '(':
                        depth += 1
                    elif cj == ')':
                        depth -= 1
                    j += 1
                block = t[i:j]
                pm = re.search(
                    r'\(pts\s+\(xy\s+([\d.\-eE]+)\s+([\d.\-eE]+)\)\s+\(xy\s+([\d.\-eE]+)\s+([\d.\-eE]+)\)\)',
                    block,
                )
                if pm and (pm.group(1), pm.group(2), pm.group(3), pm.group(4)) in own_stubs:
                    if j < n and t[j] == '\n':
                        j += 1
                    i = j
                    continue
            out.append(ch); i += 1
        return ''.join(out)

    main_text = _strip_matching_wires(main_text)

    main_new_blocks = []
    main_added: list[str] = []
    for sheet_file, pins in sheets:
        for name, _shape, x, y in pins:
            x_str = _coord(x)
            y_str = _coord(y)
            x2_str = _coord(x + STUB_LEN)
            main_new_blocks.append(
                f'\t(wire\n'
                f'\t\t(pts (xy {x_str} {y_str}) (xy {x2_str} {y_str}))\n'
                f'\t\t(stroke (width 0) (type default))\n'
                f'\t\t(uuid "{_new_uuid()}")\n'
                f'\t)'
            )
            main_new_blocks.append(
                f'\t(label "{name}"\n'
                f'\t\t(at {x2_str} {y_str} 0)\n'
                f'\t\t(effects (font (size 1.27 1.27)) (justify left))\n'
                f'\t\t(uuid "{_new_uuid()}")\n'
                f'\t)'
            )
            main_added.append(f"{name}@{sheet_file}")
    if main_new_blocks:
        main_text_stripped = main_text.rstrip()
        if main_text_stripped.endswith(')'):
            main_text = main_text_stripped[:-1] + '\n' + '\n'.join(main_new_blocks) + '\n)\n'
            main_sch.write_text(main_text, encoding="utf-8")
    report["_main_added"] = main_added

    for sheet_file, pins in sheets:
        sub_path = proj_dir / sheet_file
        if not sub_path.exists():
            print(f"  ⚠ sheet 引用 {sheet_file} 不存在，跳过")
            continue
        sub_text = sub_path.read_text(encoding="utf-8")
        existing = _existing_hier_labels(sub_text)

        missing = [(name, shape) for name, shape, _x, _y in pins if name not in existing]
        if not missing:
            report[sheet_file] = []
            continue

        # 策略：把已有 (label "name") 中的第一个原地替换成 hier_label，
        # 这样 hier_label 落在 pin 坐标上，避免 ERC 的 label_dangling。
        # 找不到现成 label 的（极少）才追加在左上角（那条会 dangling，由 ERC
        # 报出来给用户看）。
        added = []
        appended_blocks = []
        cur_y = 10.0
        for name, shape in missing:
            found = _find_label_block(sub_text, name)
            if found:
                start, end, x, y = found
                replacement = (
                    f'(hierarchical_label "{name}" '
                    f'(shape {shape}) '
                    f'(at {x} {y} 0) '
                    f'(effects (font (size 1.27 1.27)) (justify left)) '
                    f'(uuid "{_new_uuid()}"))'
                )
                sub_text = sub_text[:start] + replacement + sub_text[end:]
                added.append(name)
            else:
                appended_blocks.append(
                    f'\t(hierarchical_label "{name}"\n'
                    f'\t\t(shape {shape})\n'
                    f'\t\t(at 10.16 {cur_y:.2f} 180)\n'
                    f'\t\t(effects\n'
                    f'\t\t\t(font\n'
                    f'\t\t\t\t(size 1.27 1.27)\n'
                    f'\t\t\t)\n'
                    f'\t\t\t(justify right)\n'
                    f'\t\t)\n'
                    f'\t\t(uuid "{_new_uuid()}")\n'
                    f'\t)'
                )
                cur_y += 2.54
                added.append(f"{name}*dangling*")

        if appended_blocks:
            sub_text = sub_text.rstrip()
            if not sub_text.endswith(')'):
                print(f"  ⚠ {sheet_file} 末尾不是 ')', 跳过追加")
            else:
                sub_text = sub_text[:-1] + '\n' + '\n'.join(appended_blocks) + '\n)\n'

        sub_path.write_text(sub_text, encoding="utf-8")
        report[sheet_file] = added

    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("main_sch", help="路径到主 .kicad_sch（跟 .kicad_pro 同名）")
    args = ap.parse_args()

    main_sch = Path(args.main_sch)
    if not main_sch.exists():
        raise SystemExit(f"❌ 主 sch 不存在: {main_sch}")
    rep = patch_hier_labels(main_sch)
    total = sum(len(v) for v in rep.values())
    print(f"✅ 补了 {total} 个 hier_label，分布：")
    for sheet, names in rep.items():
        if names:
            print(f"  {sheet}: {names}")


if __name__ == "__main__":
    main()
