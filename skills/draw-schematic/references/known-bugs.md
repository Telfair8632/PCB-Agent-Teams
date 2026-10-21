# 已知 bug + workaround 大全

## 🔴 #1 最常见 bug：Y 轴翻转陷阱（坐标系不对，pin 没连上）

> 借鉴 kenchangh/kicad-schematic 的精华——**这是 90% "pin not connected" 的根因**。

### 根因

KiCad 的两套坐标系**Y 轴方向相反**：

| 文件 | Y 轴方向 | 来历 |
|---|---|---|
| **`.kicad_sym`**（symbol 库定义）| Y 朝上（数学约定）| LIB 里 pin 在元件 body 上方 → y > 0 |
| **`.kicad_sch`**（实际原理图）| Y 朝下（屏幕约定）| 屏幕坐标系，y 越大越往下 |

转换时**必须取反 Y**，否则 label/wire 离 pin 5-50mm 远 → 大量 `pin_not_connected` + `label_dangling`。

### 公式表（4 个旋转角度，按这个写不会错）

设：
- 元件 placed 在 schematic 的 `(sx, sy)`，旋转角 `rot`
- pin 在 lib 里相对 origin 的坐标 `(px, py)`

| rot | schematic 实际 pin 位置 |
|---|---|
| **0°**   | `(sx + px, sy - py)`  ⚠️ Y 减 |
| **90°**  | `(sx + py, sy + px)` |
| **180°** | `(sx - px, sy + py)` |
| **270°** | `(sx - py, sy - px)` |

### Python 实现

```python
def pin_abs(sx: float, sy: float, rot: float, px: float, py: float) -> tuple[float, float]:
    """4 旋转的精确变换。永远用这个，不要手算。"""
    rot = rot % 360
    if rot == 0:   return (sx + px, sy - py)
    if rot == 90:  return (sx + py, sy + px)
    if rot == 180: return (sx - px, sy + py)
    if rot == 270: return (sx - py, sy - px)
    raise ValueError(f"非标准旋转: {rot}")
```

或用 `kicad-sch-api` 拿（推荐——内部已处理）：
```python
import kicad_sch_api as ksa
sym = ksa.get_symbol_info("Device:R")
pin_lib = sym.get_pin("1").position  # lib 坐标
# 然后套 pin_abs() 公式
```

---

## 上游 circuit-synth 状态（github.com/circuit-synth/circuit-synth）

读源码确认：本仓的 fix_labels.py / add_hier_labels.py 不是绕路而是必要补丁，因为
上游有几处未修的设计 bug，工作区 patch 是唯一可行做法。

| 位置 | 状态 | 影响 |
|---|---|---|
| `_is_net_hierarchical` | 0.12.1 写死 `return False`、main 写死 `return True`；正确逻辑（按"net 是否跨子表使用"判断）写在文件里但被注释掉了 | 0.12.1 hierarchical 项目跨表全断；main 反过来单图项目大量错 |
| 串联 R chain 的 label coord | vanilla 0.12.1 输出实测 `HV_DIV1` 落在 R1.pin1 而非 R1.pin2 坐标（应在 y=39.37，实际放到 y=31.75）→ 跟前一颗 pin1 撞坐标 → KiCad 合并 net | net 大面积错合并；fix_labels 用 lib pin 真实坐标重写 |
| 旋转公式 | `local_y = -anchor_y` 然后再旋转（Y 翻转**在**旋转之前）。本仓 fix_labels.pin_screen_coord 已对齐 | rot=0/180 等价；rot=90/270 只有上游顺序对（早期 fix_labels 写反过，本仓 rot=0 项目没暴露） |
| 跨表连接（sheet pin ↔ hier_label） | 上游不写 `(hierarchical_label)`，主 sch 也不在 sheet pin 拉 wire+label | 跨表 net 物理不连通；本仓 add_hier_labels 自动补 |
| 相关 PR | [#582](https://github.com/circuit-synth/circuit-synth/pull/582)（power net 自动检测 + power 跳过 hier_label）2025-10 起开着没合，且不动 `_is_net_hierarchical` 的核心逻辑 | 等不到上游修，本仓继续维护 patch |

> 等上游把 `_is_net_hierarchical` 真正按"shared with parent or used by children"做对，
> add_hier_labels.py 大部分逻辑就可以删；目前没那一天。

---

## 🔴 #2 circuit-synth：label 错放（chain 中 pin label 跟前一颗 pin 同坐标）

### 现象

`generate_kicad_project()` 输出的 sch 里，串联 R/C chain 后一颗的 pin label 被
错放到前一颗 pin 同坐标（如 `(label "HV_DIV1" (at 30.48 31.75))` 跟
`(label "HV_PLUS" (at 30.48 31.75))` 同点）→ KiCad 把不同 net 当成同一 net 合并。

实测：38 元件单图，13 个 net 被合成 8 个；hierarchical 项目每个 sub-sch 内部
独立出现一遍 → 跨表也错。

### 修复

`scripts/pipeline.py` Step 3 自动跑 `fix_labels.py`，对**每个 .kicad_sch（main + 所有
sub-sheet）**都重新放置 label：

1. 用深度计数（不是 regex）删旧 `(label ...)`，避免 pretty-print 的嵌套
   `(effects (font (size ...)))` 让 regex 失效
2. 用 `kicad-sch-api` 拿每个 component pin 的精确坐标（含旋转 + Y 翻转）
3. 在精确坐标重写 `(label "<net_name>" ...)`
4. 同时给 lib 里存在但 `.py` 没接的 pin 写 `(no_connect)` —— 抑制 ERC
   pin_not_connected（用户在 .py 不写 `comp[N] += ...` 视为故意悬空，例如
   switch 的 NC 端、模块备用 pin、机械固定脚）

> 历史脚本只对 main sch 跑 fix_labels；hierarchical 项目里 components 全在 sub-sch，
> main sch 是 sheet 容器（0 元件）→ fix 没机会运行 → net 仍合并。已修。

---

## 🔴 #3 hierarchical_label 在单 sheet vs 多 sheet 模式行为不同

### 现象

- **单 sheet（旧）**：circuit-synth 把 hier_label 当 net label 用 → KiCad 当作未连接子图引脚 → 64 ERC 错。
- **多 sheet（hierarchical）**：sub-sheet 之间靠 `(hierarchical_label ...)` 端口跨表连接；如果删了，跨表 net 直接断开。

### 修复

`fix_labels.py` 的 `drop_hier_labels` 参数：

| 模式 | drop_hier_labels | 用法 |
|------|------------------|------|
| 单 sheet | True | 当作 net label 转换成普通 label（兼容旧项目） |
| 多 sheet sub-sch | False（默认） | 保留 hier_label 作为 sheet 端口 |

`pipeline.py` 自动判断：仅对"sch 文件 stem == 项目目录名"的主 sch 传 True，sub-sch 一律
False。

### 多 sheet 跨表连接的额外修

circuit-synth 0.8.36 在主 sch 写 `(sheet (pin "X" bidirectional ...))` 但 sub-sch
里**不放对应的 (hierarchical_label "X")** → ERC `hier_label_mismatch`，跨表实际断连。

`scripts/add_hier_labels.py` 自动补：
1. 扫主 sch 每个 (sheet ...) 块的 (pin "X" (at Sx Sy)) 列表
2. 对每个 sub-sch，把已有 `(label "X")` 中的第一个原地替换成
   `(hierarchical_label "X" ...)`（落在 pin 坐标，不会 dangling）
3. 主 sch 上每个 sheet pin 拉一段 2.54mm 短 wire，wire 末端放
   `(label "X")`——同名 label 跨子表自动连通
4. 坐标统一 `round(v, 4)` 防 python float 漂导致 `unconnected_wire_endpoint`

---

## 🔴 #4 PDF 渲染空白图框（chechk 假阳性）

### 现象

工具调用回 `success: true`，`list_schematic_components` 显示 31 元件，但 KiCad 打开 sch / 渲染 PDF 是**完全空白图框**。

### 根因

某些 MCP 工具（如 mixelpixx KiCAD-MCP-Server）只往 `lib_symbols` 段（库模板）写定义，**没真正放置元件实例**到 sch 主体。

### 验证

```bash
SCH=path/to/proj.kicad_sch
echo "lib_id 数: $(grep -oE 'lib_id' "$SCH" | wc -l)"  # 必须 ≥ 元件数
echo "wire 数:   $(grep -oE '\(wire ' "$SCH" | wc -l)"
echo "label 数:  $(grep -oE '\(label ' "$SCH" | wc -l)"
```

如果 `lib_id` < 元件数 → 元件没真正放置，工具回 success 是假的。

---

## 🔴 #5 LCSC 元件 pin 顺序跟厂商 datasheet 不一致

### 现象

easyeda2kicad 下载的元件是从 LCSC/EasyEDA 中国数据库来的，**pin 顺序可能跟厂商官方 datasheet 不一致**。

实测：
- IB0505XT-1WR3：LCSC 给 6 pin，但 datasheet 实际 4 pin（中间 2 pin NC）
- EG1218：LCSC 给 3 pin SPDT，部分版本只有 2 pin

### 防范

下载后**必须做**：
```python
# 1. 数 pin
pins = sym.list_pins()
print(f"{mpn}: {len(pins)} pin")

# 2. 对照 datasheet 的 pin 1-N 含义表
# 3. 写到 .py 的 COMPONENT_NETS 时，按 datasheet pin 编号映射
```

如果 LCSC pin 跟 datasheet 不一致 → fallback 用 KiCad 自带占位 + 记录到 SUBSTITUTIONS.md。

---

## 🟡 #6 元件挤一团（自动布局烂）— hierarchical 仅作为兜底

### 现象

>15 元件项目，circuit-synth 自动布局把元件按行密集堆在 A4/A0 顶部 1/8 区域。
单图 + label-only 模式下视觉密集（label 飘在元件附近）。**电气仍是对的**——
`pipeline.py` 跑 `fix_labels` 用 kicad-sch-api 真坐标重写 label，bug #2 的
label coord collision 已经压住，单图 38+ 元件电气也不会错合并。

### 处理顺序

1. **默认走单图**——一个 `@circuit` 把所有元件写进去，跑 pipeline。
2. KiCad GUI 打开，手动拖元件分散布局（一次性 10–20 分钟）。
3. **仅当**实测 ERC 出现 net 错合并（pin_to_pin 异常合并 / 拓扑 verify_topology fail）→ 切 hierarchical。

切 hierarchical 时每子图 ≤15 元件，pipeline 自动迭代所有 sub-sch + 自动补 hier_label：

```python
@circuit(name="HV_Input")
def hv_input(HV_pos, HV_GND, HV_sense):
    # 子电路 1（≤15 元件）
    ...

@circuit(name="Top")
def top():
    HV_pos = Net("HV+"); HV_GND = Net("HV_GND"); HV_sense = Net("HV_sense")
    hv_input(HV_pos, HV_GND, HV_sense)
```

### 为什么 hierarchical 不是默认

- **检查工具退化**：`check-schematic` 的 analyzer 把同名 net 拆成 `name`（顶层）+ `/<uuid>/name`（子图）两个 net，`detect_rc_filters` 看不到 R-C 共享 net 对，信号链 fc 验证全失效（必须用 `aa_filter_sim.py` 自己 coalesce 才补回来）。
- **多一套 patch 链**：bug #3（`_is_net_hierarchical` 上游写错）+ sheet_pin↔hier_label 不连等问题都依赖 `add_hier_labels.py` 56 个补丁，单图不需要。
- **PCB / SPICE 导出**：`kicad-cli sch export netlist --format spice` 单图出 flat .cir，hierarchical 出嵌套结构跟下游 ngspice 对接更绕。

---

## 🔴 #7 工具假阳性（不验证就报告"完成"）

### 教训

mixelpixx 工具调用 30+ 次都回 `success: true`，最后看 PDF 才发现是空白。**只看工具回报数字 = 被骗**。

### 铁律

**每次报告"完成"前必须做 3 层验证**（在 SKILL.md 的 Stage 5）：
- L1 数据：pin_not_connected = 0
- L2 视觉：用 Read 工具读 PDF，**真看图判断**
- L3 拓扑：跟项目 CLAUDE.md ASCII 图段逐 net 比对

任一不通过 → 不交付。
