# 概念示意图 prompt 模板（走 picture skill，模型 nano）

讨论拓扑时用户说"画一下" / "看图" / "出张图" → 调 picture skill（`--models nano`）出**概念示意图**辅助理解。**不是**准确电路图（那归 draw-schematic / draw-pcb）。

## 预期管理（**用户必须知道**）

- nano-banana 是图像生成模型，**不理解电气连接**——本质是模式匹配
- 实测 v2 模板能画对 ~95% 关键 net，剩 ~5% 是 image-gen 模型的本质上限
- **要 100% 准确的 netlist，必须用 KiCad（Phase 3 `draw-schematic`）真画**——nano-banana 的图只用于讨论可视化辅助
- 价值在"一眼看懂分区结构 + netlist 大致走向"，不在"100% 网表正确"
- **修瑕疵走 edit 路线**（见下"迭代修复协议"），不要 generate 重跑

## 决策规则：v1 vs v2

| 电路特征 | 用哪版 | 理由 |
|---|---|---|
| 元件 ≤ 8 个 + 单域 + 无隔离 | **v1** | prompt 短，"看分区"已够 |
| 元件 > 8 + 多电源域 | **v2** | netlist 走向对学生/同事理解关键 |
| 含隔离屏障（galvanic isolation） | **v2** | 跨界 IC 的 pin↔域映射必须明确（避免 SHTDN 接错电源等典型 datasheet 错） |
| 简单 LDO / 单 op-amp / 单 RC 滤波 | **v1** | 杀鸡用刀 |

## 视觉风格基底（黑白教科书风，**写死不改**，append 到 JSON subject 后）

```
Style: clean technical schematic illustration, IEC/IEEE standard symbols
for resistors capacitors diodes ICs, thin uniform black lines on white
background, orthogonal wire routing on a grid, sans-serif black labels
for every component reference designator AND every wire/net name, NO color
fills, NO color washes, monochrome textbook reference figure aesthetic.
Composition: HV domain left, isolation barrier center (vertical parallel
double black lines), LV domain right, power row at bottom; domain separation
indicated by spacing and text labels (NOT color). Lighting: flat white
background, no shadows.
```

> 历史选择：早期默认信息图风（HV 暖色 / LV 冷色 / 隔离带灰色），实测 87/100 平均分；用户后续偏好黑白教科书风（更通用、打印友好）。**两风格在 netlist 准确度上几乎无差异**，差异只在视觉风格——netlist 准确性靠 v2 pin_connections 字段保证，不靠风格。

## v1: 简化模板（≤ 8 元件 / 单域电路）

只列元件 + 角色，不列 pin↔net 连接。nano-banana 自己根据角色描述蒙连线（基本对，不能保证 net 100%）。

```json
{
  "schematic_concept": "<one-line>",
  "<DOMAIN>_LEFT": {
    "voltage": "<v>",
    "<chain_role>": {
      "exact_count": <N>,
      "required": true,
      "resistors": [{"ref": "R1", "value": "<v>"}, {"ref": "R2", "value": "<v>"}]
    },
    "<other_components>": {...}
  },
  "<barrier_if_any>": {"orientation": "vertical", "style": "parallel_double_lines"},
  "<DOMAIN>_RIGHT": {...},
  "power_section_BOTTOM": {
    "LED1": {"label_color": "GREEN", "current_limit_resistor": "R10", "required": true},
    "LED2": {"label_color": "YELLOW", "current_limit_resistor": "R11", "required": true}
  },
  "ground_rails": {"<HV_GND>": "LEFT", "<LV_GND>": "RIGHT"}
}
```

**v1 关键字段**：`exact_count` + `required: true` 把易丢元素稳住。短关键词比长 critical 句子稳——**长字符串会被印进图当 caption**（实测）。
**LED color**：风格已黑白，LED 实际不上色；`label_color` 字段值会被当文字标签印（"GREEN" / "YELLOW"）。

## v2: 增强模板（≥ 8 元件 / 多域 / 隔离电路）

每个主要 IC + 端子 + 关键无源元件附 `pin_connections` 字段。每条目格式 `"<pin_no_name>": "<net name or other.pin>"`。nano-banana 会按这个画线 + 标 net 名。

```json
{
  "schematic_concept": "<one-line>",
  "render_rule": "every wire labeled with its net name; pins connected EXACTLY as listed in pin_connections",

  "<DOMAIN>_LEFT": {
    "voltage": "<v>",
    "components": {
      "<connector>": {"type": "<screw_terminal_2pin>",
                      "pin_connections": {"1": "<NET_A> net", "2": "<NET_B> net"}},
      "<chain_role>": {
        "exact_count": <N>, "required": true,
        "chain_order_top_to_bottom": ["R1", "R2", "R3", "R4", "R5"],
        "internal_connections": "R1.1=<NET_TOP>, R1.2=R2.1, ..., R5.2=<NET_TAP>",
        "all_values": "<v> each"
      },
      "<R/C with explicit pins>": {
        "value": "<v>", "role": "<short>",
        "pin_connections": {"1": "<NET_A> net", "2": "<NET_B> net"}
      }
    }
  },

  "<barrier_CENTER>": {
    "orientation": "vertical",
    "style": "parallel_double_lines",
    "type": "galvanic",
    "rule": "no wire crosses; <HV_GND> and <LV_GND> are separate"
  },

  "<isolated_IC_REF>": {
    "mpn": "<part>", "package": "<SOIC-8 wide>",
    "position": "straddles_isolation_barrier_with_pins_1to4_on_HV_left_and_pins_5to8_on_LV_right",
    "pin_count": <N>,
    "pin_connections": {
      "1_<NAME>": "<rail or net>",
      "2_<NAME>": "<net>",
      "3_<NAME>": "<net> (tied_LOW)",
      "...": "..."
    }
  },

  "<DOMAIN>_RIGHT": {
    "voltage": "<v>",
    "components": {
      "<R/C with pins>": {"value": "<v>",
                          "pin_connections": {"1": "<net>", "2": "<net>"}}
    }
  },

  "power_section_BOTTOM": {
    "components": {
      "<DCDC>": {"type": "isolated_DCDC",
                  "pin_connections": {"VIN+": "<net>", "VIN-": "<net>",
                                       "VOUT+": "<rail>", "VOUT-": "<rail>"}},
      "<LDO>": {"type": "LDO",
                 "pin_connections": {"VIN": "<net>", "GND": "<net>", "VOUT": "<rail>"}},
      "<switch>": {"type": "slide_switch",
                    "pin_connections": {"1_in": "<net>", "2_out": "<net>"}},
      "<schottky>": {"type": "Schottky_diode", "required": true,
                      "pin_connections": {"A_anode": "<net>", "K_cathode": "<net>"}},
      "<header>": {"type": "header_2pin",
                    "pin_connections": {"1": "<net>", "2": "<net>"}},
      "LED1": {"label_color": "GREEN", "current_limit_resistor": "R10", "required": true,
               "pin_connections": {"A_anode": "via R10 to <rail>", "K_cathode": "<GND>"}},
      "R10": {"value": "<v>", "required": true,
              "pin_connections": {"1": "<rail>", "2": "LED1.A_anode"}}
    }
  },

  "ground_rails": {"<HV_GND>": "LEFT", "<LV_GND>": "RIGHT",
                   "separation": "physical, by isolation barrier"},

  "wire_labeling_rule": "every visible wire shows its net name (list all critical nets here)"
}
```

**v2 关键字段**：

| 字段 | 用途 | 注意 |
|---|---|---|
| `pin_connections` | pin↔net 映射 | net 名以 `<NAME> net` 或 `<NAME> rail` 结尾 |
| `position: "straddles_..."` | 跨界 IC 跨 barrier 的描述 | 偶尔被拆相邻两块（hallucination），结构上仍正确 |
| `internal_connections` | 长串电阻链的 pin↔pin 字符串 | 节省 prompt 长度 |
| `required: true` | 易丢小元件（限流 R、Schottky） | **不要写完整句子**，会被印进图 |
| `wire_labeling_rule` | 顶层指令 | 让 nano-banana 把 net 名标进图 |
| `render_rule` | 顶层指令 | 强调"按 pin_connections 画"，禁止 reorder |
| `label_color: "GREEN"` | LED 文字标签 | 黑白风下不实际填色，只印文字 |

## PCB 概念图：JSON 模板（v1 / v2 决策同上）

```json
{
  "pcb_layout_concept": "<one-line>",
  "view": "top_down_no_3D_perspective",
  "horizontal_split": {
    "LEFT_<HV_DOMAIN>": {
      "physical_clearance_to_right": ">=10mm air gap",
      "components": [
        {"ref": "J1", "type": "screw_terminal", "position": "top_left_edge"},
        {"ref": "<chain>", "exact_count": <N>, "required": true,
         "arrangement": "vertical_column"}
      ]
    },
    "RIGHT_<LV_DOMAIN>": {
      "components": [
        {"ref": "<isolated_IC>", "footprint": "<SOIC-8>",
         "position": "anchored_just_right_of_air_gap",
         "pin_orientation": "HV_pins_facing_left_toward_gap"}
      ]
    }
  },
  "labeling": "every component shows reference designator"
}
```

**PCB 必填**：`"view": "top_down_no_3D_perspective"`——否则 nano-banana 默认画 3D 渲染。

## 调用协议（generate）

```
Step 1: 跟用户对齐回路结构（按 SKILL.md 讨论步骤 拓扑→锚点→周边 正常讨论）
Step 2: 用户说"画一下" / "看图" / "illustrate"
Step 3: 按"决策规则"选 v1 还是 v2
Step 4: 拼 prompt:
        subject_json = json.dumps(<filled template>, indent=2)
        full_prompt = subject_json + " " + <style_base>
Step 5: 调 picture skill 生成（默认 --n 2 看稳定性）：
        python3 ~/skills/picture/pic.py gen "<full_prompt>" \
          --models nano --n 2 --out Projects/<name>/docs/illustrations/
        --models nano = Nano Banana 2；本模板专为它调，别 fan-out 到 seedream/gpt
Step 6: 落地到 Projects/<name>/docs/illustrations/<sch|pcb>_v0.X.png
Step 7: 给用户图链接 + 一句话提醒："概念示意图，元件细节可能与 BOM 略有偏差，
        准确图见 draw-schematic / draw-pcb"
```

## 迭代修复协议（edit）

用户审图后发现局部错误（缺元件 / 连线错 / pin 接错 / 标签错）→ **不要 generate 重跑**，走 edit 路线针对性修复。

**走 picture skill 的 edit 子命令，不要手搓 fal.ai 请求**——端点 / 轮询 / 下载都由 `pic.py` 封装：

| 项 | generate | **edit** |
|---|---|---|
| 命令 | `pic.py gen "<prompt>"` | `pic.py edit "<instruction>" --image <URL或本地路径>` |
| 图参数 | 无 | `--image`（可重复，传多张源图）|
| 张数 | `--n 2`（看稳定性） | 始终返回 1 张 |
| 长宽比 | `--ar` 默认 `1:1` | 默认 `auto`（保持源图比例）|

**调用步骤**：

```
Step 1: 用户审 generate 出的图，列具体错误（"X 没画" / "Y 接到了 Z 但应该接到 W"）
Step 2: 每个错写一条 edit instruction（一次一个错，不要批量改 ≥ 3 处）
Step 3: instruction 模式：
        "<动词> <具体目标>. <连接细节>. Do NOT modify any other component,
        connection, or label in the image — preserve every existing element
        exactly as drawn. Keep the overall monochrome textbook style."
        关键：必带"Do NOT modify ... preserve every existing element exactly as drawn"
Step 4: python3 ~/skills/picture/pic.py edit "<instruction>" \
          --image <上一版图路径> --out Projects/<name>/docs/illustrations/
Step 5: 把出来的图存成下一个版本号:
        sch_v0.X.png → sch_v0.(X+1).png（不覆盖原版本，方便回滚）
Step 6: 用户继续审，直到满意
```

**Edit instruction 写法示例**：

| 场景 | 推荐 instruction |
|---|---|
| 加元件 | "Add a small ceramic cap labeled 'C5 100nF' between U1.VDD2 (pin 8, +3V3 rail) and LV_GND. Place it on the right side of U1, drawn as a standard non-polarized cap symbol. Connect one plate to +3V3 rail, the other to LV_GND rail. Add labels 'C5' and '100nF' next to it." + preserve 句 |
| 改连接 | "Reroute the wire from U1.3 (SHTDN pin) so that it connects to the HV_GND rail instead of the +5V_ISO rail. Update the wire path; keep all other wires unchanged." + preserve 句 |
| 删元件 | "Remove the resistor labeled 'R12' and its two wires; reconnect the two endpoints directly with a single wire labeled '<net>'." + preserve 句 |
| 改标签 | "Change the label '6.8nF' next to C2 to '4.7nF'. Do not change anything else." |

**实测有效性**：edit "Add C5 100nF between U1.VDD2 and LV_GND" → 返回图 C5 准确加到正确位置 + 极性正确 + 其他关键 net 全保留。

## 反模式

- ❌ **用 natural prose 当 subject** —— 实测易丢元素 present 率比 JSON 低 30%
- ❌ **`critical` 字段写完整句子** —— 会被 nano-banana 印进图当 caption。用 `"required": true` 短关键词
- ❌ **`description` 替代 `exact_count` + 显式 list** —— nano-banana 不读字符串里的数字
- ❌ **简单电路上 v2** —— 浪费 prompt 长度 + caption 噪声；v1 已够
- ❌ **复杂隔离电路上 v1** —— pin↔域映射不明确，nano-banana 乱猜（典型：SHTDN 接 +5V_ISO 而非 GND1）
- ❌ **prompt 里给元件值（"10kΩ" / "68pF"）当 ground truth** —— nano-banana 不验证数值；只当 label 印
- ❌ **写项目 datasheet 约束（"must comply with IEC 60664"）** —— nano-banana 不理解标准
- ❌ **风格基底里加颜色** —— 已锁定黑白；加 color wash 会破坏一致性，且对 netlist 准确度无帮助
- ❌ **edit 一次改 ≥ 3 处** —— 越多越乱，每条 instruction 只改 1 处；多处错就跑多次 edit
- ❌ **edit 不写 "Do NOT modify ... preserve" 句** —— 不写就会全图重画，丢失原有元件
- ❌ **edit 改太大结构（如重排整个 LV 域）** —— edit 适合局部小修，结构性大改回 generate 重出
- ❌ **多次重画前不审 prompt** —— 同一 v2 prompt 重跑稳定性 ≥90%；不稳定时改 JSON 字段，不要乱试
- ❌ **把 nano-banana 出图当 review 依据** —— 它是讨论辅助，**不能**当 sch / pcb 检查通过的证据

## 实测结论（已固化进上面的模板 / 反模式）

- **prompt 结构**：JSON 比 natural / markdown / xml 稳——元素 present 率最高、方差最小
- **netlist 准确度**：JSON 加 `pin_connections` 字段后关键 net 几乎全画对（含 pin↔域映射 / datasheet pin fix）
- **风格**：黑白信息图 / 教科书风最准；加 color wash 不提准确度还破坏一致性
- **edit**：局部小修（加 decoupling cap 等）+ preserve 句 → 准确加件且保留原图其余元件
- **模型**：nano-banana 与同级 image-gen 的 netlist 准确度持平（受 image-gen 上限制约），不为此切更慢更贵的模型
