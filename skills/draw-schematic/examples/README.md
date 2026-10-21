# Examples

## 推荐：用真实项目作为参考

每个项目目录 `Projects/<name>/kicad/` 里都应有：
- `<name>.py` — circuit-synth 源码
- `<Generated_Project>/` — pipeline 跑出的产物（自动生成，可删可重生）

要看一个完整、跑通的项目作模板，找任何已经跑过 pipeline 的项目（项目级 CLAUDE.md 标注的）。

## 模式选择

| 模式 | 何时用 | 理由 |
|---|---|---|
| **单图（默认）** | 任何项目，无论元件数 | `fix_labels.py` 已压住上游 label collision bug（#2）；检查工具 `analyze_schematic.py` / `detect_rc_filters` / `simulate_subcircuits.py` 都为单图优化（hierarchical 下 net 会被分片成 `name` + `/uuid/name`，detector 失效） |
| Hierarchical（兜底） | 单图 ≥38 元件实测仍 net 错合并；或业务需要 IP 复用 / 子板分交付 | 拆 sub-`@circuit`，每片 ≤15。pipeline 自动迭代子 sch 跑 fix_labels + 自动补 hier_label，但检查工具精度下降 |

### 单图模式（默认）

```python
from circuit_synth import Component, Net, circuit

@circuit(name="My_Circuit")
def my_circuit():
    HV = Net("HV+")
    GND = Net("GND")
    r1 = Component(symbol="Device:R", ref="R1", value="500k",
                   footprint="Resistor_SMD:R_1206_3216Metric")
    r1[1] += HV; r1[2] += GND
    # ... 所有元件全部写在一个函数里，不管多少

if __name__ == "__main__":
    c = my_circuit()
    c.generate_kicad_project(project_name="my_circuit", force_regenerate=True)
```

### Hierarchical 模式（兜底）

子电路接 net 参数；顶层只声明全局 net + 调子电路：

```python
@circuit(name="hv_input")
def hv_input(HV_PLUS, HV_GND, AMC_IN):
    j1 = Component(symbol="...", ref="J1", value="...", footprint="...")
    j1[1] += HV_PLUS;  j1[2] += HV_GND
    # 子图局部 net 用 Net("...") 在子函数内声明
    # 顶层共享 net 通过函数参数传入

@circuit(name="iso_amp_block")
def iso_amp_block(AMC_IN, HV_GND, LV_GND, V5V_ISO, V3V3, ADC_P, ADC_N):
    u1 = Component(symbol="...", ref="U1", value="...", footprint="...")
    # ...

@circuit(name="top")
def top():
    HV_PLUS = Net("HV+"); HV_GND = Net("HV_GND"); LV_GND = Net("LV_GND")
    AMC_IN = Net("AMC_IN"); V5V_ISO = Net("V5V_ISO"); V3V3 = Net("V3V3")
    ADC_P = Net("ADC_P"); ADC_N = Net("ADC_N")
    hv_input(HV_PLUS, HV_GND, AMC_IN)
    iso_amp_block(AMC_IN, HV_GND, LV_GND, V5V_ISO, V3V3, ADC_P, ADC_N)
```

`pipeline.py` 自动迭代所有 .kicad_sch（main + subs）跑 fix_labels；跨表 net 由 `add_hier_labels.py` 自动补 hierarchical_label。**不要**手动加 hier_label。

视觉密集是接受的代价（电气保证 100% 对，pipeline L3 拓扑校验保证）；用户在 KiCad GUI 拖元件二次美化。

### 项目专属信息存哪

| 内容 | 位置 |
|---|---|
| `<project>.py` 源码 | `Projects/<name>/kicad/` |
| 生成产物 | `Projects/<name>/kicad/<Generated>/`（可删可重生）|
| BOM 表 + 拓扑 ASCII 图 | `Projects/<name>/CLAUDE.md` |
| 占位决定（用什么替代真元件）| `Projects/<name>/kicad/SUBSTITUTIONS.md` |
| 项目专属 footprint | `Projects/<name>/kicad/lib_local/`（不入工作区共享）|

工作区共享：`PCB-Agent-Teams/lib_external/components.*`（所有项目通用元件库）。
