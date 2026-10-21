# 跟其他 skill 的边界

本 skill 只做 Phase 1 回路结构讨论。下面是「不做什么 / 谁做」完整对照。

| 这个 skill 不做 | 谁做 |
|---|---|
| 创建项目目录 | `project-init` |
| 严格选品（library / locale active / 三件套 / verdict / shortlist JSON） | `component-selecting`（**回路冻结后**调用，本 skill 不调用严格模式）|
| 资产抓取（datasheet / library / evidence / lib_external 写入） | `component-preparing`（component-selecting 出 shortlist 后 user 拍板 → 触发抓取，**不再回到本 skill**）|
| 单 MPN 快速查询（active + 库存） | `component-selecting-JP --mpn`（本 skill **可调用**作辅助）|
| 项目级 BOM gate（fidelity 三检 / 采购 BOM CSV / sentinel） | `component-preparing` |
| 把 .py 跑成 .kicad_sch | `draw-schematic` |
| SPICE 仿真验证 | `check-schematic` |
| EMC 风险分析 | `check-pcb` |
