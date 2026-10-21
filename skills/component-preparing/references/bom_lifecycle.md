# BOM 文件生命周期（迁自工作区 CLAUDE.md）

三类 BOM 不互相覆盖，作用不同：

| 阶段 | 文件 | 谁写 | 用途 |
|---|---|---|---|
| 选品证据 | `Projects/<name>/datasheets/bom_v01.csv` | `component-selecting-JP` | 候选/已选 MPN、locale vendor evidence、library/datasheet 证据；供后续 gate 复检 |
| 采购 BOM | `Projects/<name>/datasheets/bom_<project>.csv` | `component-preparing`（`scripts/check_readiness.py`）| 按 MPN 聚合的 distributor 下单表；买料用，不含 placement 语义 |
| 生产 BOM | `Projects/<name>/release/<ts>/pcb_fab/assembly/<board>_assembly_bom.csv` | `release/scripts/export_gerbers.py` | 给 JLCPCB/PCBWay 等 fab assembly 用；与 CPL/position file 配套 |
| CPL / 位置文件 | `Projects/<name>/release/<ts>/pcb_fab/assembly/<board>_positions.csv` | `release/scripts/export_gerbers.py` | Pick-and-place 坐标；生产 BOM 的装配伙伴文件 |

## 转换链

```
component-selecting bom_v01.csv
   ↓  evidence + 项目 CLAUDE.md BOM + circuit-synth .py
component-preparing → bom_<project>.csv + .bom_readiness.json sentinel
   ↓  sch/pcb 已生成并通过 check-* gate
release umbrella
   ├─ release/scripts/export_gerbers.py → production BOM + CPL + Gerber/Drill ZIP
   └─ vendor 决策（JLCPCB vs PCBWay，references/jlcpcb.md / pcbway.md）
```

**采购 BOM ≠ 生产 BOM**：前者给 distributor 下单买料，后者给 fab 厂贴片装配。
