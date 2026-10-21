# 入场强制：探测已建子电路

**入场第二件事**（紧跟"哪些已经定了"那一问之后），**强制**走这条决策树。不是"建议读"，是"必须跑"——只有跑完才能开始任何拓扑/锚点讨论。

```
project 目录下找 .kicad_sch
├─ 找不到 .kicad_sch
│   → 全新项目，没东西可探测，跳过本节，进 Step 0/1 正常讨论流程
│
├─ 找得到 .kicad_sch
│   ├─ Projects/<name>/analysis/schematic.json 已存在且新于 .kicad_sch
│   │   → 直接读 JSON，跳到「列已建子电路」
│   │
│   └─ JSON 不存在 / 已过期
│       → 必须先跑：
│         python3 skills/check-schematic/scripts/analyze_schematic.py \
│             Projects/<name>/kicad/<name>.kicad_sch \
│             --output Projects/<name>/analysis/schematic.json
│       → 跑完再读
```

## 「列已建子电路」步骤

扫 `findings[]` 按 `detector` 字段（`detect_voltage_dividers` / `detect_rc_filters` / `detect_lc_filters` / `detect_opamp_circuits` / `detect_crystal_circuits` / `detect_decoupling` / `detect_integrated_ldos` / `detect_current_sense` / `detect_bridge_circuits` / `detect_transistor_circuits` 等 16 类）分组，把结果列给用户：

> "看 schematic.json 你现在已经搭了：1 个 LDO（U1）/ 2 个分压（R1-R2、R5-R6）/ 1 个 RC 滤波（R3+C2）/ 1 个差分放大（U2）。**这些不重做**。今天讨论缺口是什么？"

把 detected 子电路当作"已锁定输入"，**直接跳过它们的拓扑/锚点讨论**，只跟用户对齐"还缺什么"。

## 反例

- ❌ 项目里 `.kicad_sch` 已存在，本 skill 不去 invoke `analyze_schematic.py` 就直接画新拓扑——丢失"已建什么"信息，重新选锚点浪费一整轮
- ❌ JSON 已过期（`.kicad_sch` mtime > json mtime），本 skill 拿陈旧 detection 当真——基于错误现状给建议
- ❌ 入场只问用户"哪些已定"，不交叉核对 JSON——用户记错或漏说什么子电路就跟着错
