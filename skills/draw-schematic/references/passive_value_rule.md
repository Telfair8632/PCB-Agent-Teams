# Passive Value 字段死规定

**适用范围**：draw-schematic 写 .py 时；draw-pcb Phase 2 收尾 silk audit 时。

## 规则

`Component(value=...)` 字段会被 circuit-synth 同步到 .kicad_sch 的 Value field，再被 sch_to_pcb 同步到 .kicad_pcb 的 footprint Value，**最后印到 F.SilkS 上人眼可见**。所以这个字段不是"BOM 字段"——是"板上印什么字"。

| ref 类型 | value 字段必须填 | value 字段**不许**填 |
|---|---|---|
| **R*** | `1M` / `20k` / `330R` / `100R` | ❌ `TNPW12061M00BEEA` / `RC0805FR-07330RL`（distributor SKU）|
| **C*** | `100nF` / `10uF` / `68pF NP0` / `6.8nF X7R` | ❌ `C0805C104K5RACTU` / `GRM21BR61E106KA73L`（distributor SKU）|
| **L*** | `4.7uH` / `10mH` | ❌ `LQH32CN470K53`（distributor SKU）|
| **LED*** | 颜色 / 角色（`GREEN` / `YELLOW` / `LED_PWR`）| ❌ `LTST-C171GKT`（distributor SKU）|
| **D*** 通用 | `D_Schottky` / `D_Zener_5V1` 或保留型号 | distributor SKU 不行 |
| **U*** / **D*** 复杂件 | ✅ MPN 允许（型号本身=电学语义，e.g., `AMC1311BDWVR`、`SS14`、`SMAJ440A`） | — |
| **J*** / **SW*** / **F*** | ✅ MPN 允许（机械件型号=形态/pitch 语义，e.g., `KF128-5.08-2P`、`MF-R110`） | LCSC C-编码等纯 distributor SKU 不行 |

**为什么 J/SW/F 用 MPN 而 R/C/L 不用**：
- R/C/L 的 value 是**电学量**，silk 印 `1M` 让人秒懂；MPN 是 12 字符乱码。
- J/SW/F 的"电学量"就是型号本身——`KF128-5.08-2P` 直接说出 pitch + position 数，silk 印这个比 `HV_INPUT` 多了焊接选型信息（5.08mm pitch 不是 5.0/3.5）。
- 工具链层面：`component-preparing/check_readiness.py` 把 value 当 MPN 查 evidence/datasheet 索引；J/SW 填 role name 会让 readiness gate fail。MPN 是 single source of truth。

## MPN 放哪

- **选项 A（推荐）**：写到 `Component(properties={"MPN": "TNPW12061M00BEEA", "Datasheet": "..."})` 字典里 → 进 KiCad properties → BOM 工具读
- **选项 B**：MPN 留在项目级 `docs/bom.md` + evidence JSON 里（已经在这——bom-readiness 校验时按 ref 索引）

## 为什么死

板子打回来焊接 / 调试时人眼第一秒要的是「这是 1M 还是 100k」**不是采购编号**。板上印 `TNPW12061M00BEEA` 没人能瞬间认出是 1MΩ；印 `1M` 谁都看得懂。这是 PCB 工业 30 年的肌肉记忆。

## Pipeline gate（正则检测）

写 / 改 .py 时**必须扫一遍** passive value，正则 `^[A-Z]{2,}\d+[A-Z]+\d*` 命中（distributor 编码模式）→ 立刻报警 + 给修正建议。

注：MPN（如 `AMC1311BDWVR` / `KF128-5.08-2P`）跟 distributor SKU（如 LCSC `C7509570`）的区分不能靠正则——前者厂商命名千变万化。实操上 component-preparing 写 evidence JSON 时记录 MPN，本规则只防"R/C 填了 distributor SKU"这一类典型错。

draw-pcb Phase 2 silk audit 用同一条正则扫 .kicad_pcb 的 footprint Value field。

## 血泪 case

**实测 case（2026-05）** — 某项目 .py 把 passive value 全部填了 MPN，sch + PCB silk 上是一堆 `TNPW12061M00BEEA` / `GRM21BR61E106KA73L` 之类的 12 字符长串，板子打回来根本没法肉眼调试，重写 .py 改电学量再 regen 才修好。
