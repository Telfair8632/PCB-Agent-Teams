# 关键规则（讨论时必须遵守）

## 1. 不要重做用户已锁定的部分

入场先问"哪些已定"。已定的直接继承，不重新推演 / 重新对比。

**反例**：用户已锁定某隔离放大器 + HV 分压拓扑，本 skill 仍跑完整流程，重写 CLAUDE.md 详细版 + 跑严格 sub-agent — 多花一整轮无意义工作。

## 2. CLAUDE.md 简洁版：让 AI 能明白结构即可

写到项目 CLAUDE.md 的内容**只需要让未来对话的 AI 能明白回路结构是什么**。
- ASCII 拓扑：能看清"输入 → 处理 → 输出 + 隔离边界 + 电源域"即可
- 锚点件：写清楚名称 + 角色，不必每个件都加 datasheet 引用
- legacy 已有的详细内容（参数验算 / 安规 checklist / 哲学）：**直接继承，不要重新展开**

## 3. 选品仅作辅助查询，严格审查留到回路冻结后

讨论期间需要选品参考时：
- ✅ 允许：`select_component.py --mpn <MPN>` 看 active + 库存大概数字
- ✅ 允许：fetch 单个 vendor URL 看库存
- ❌ 禁止：`--parametric-discover --library-check` 全套（这是 Phase 2.0 严格审查的事）
- ❌ 禁止：要求用户拍板 BOM 时落 evidence JSON

回路完全冻结后，**主动告知用户**："回路已对齐，下一步跑 component-selecting 对每个非通用件做严格 longlist + library + locale active 三件套审查；shortlist 出来后进 component-preparing 落资产，再进 draw-schematic。"

## 4. 不要直接给"答案"

讨论需要选取舍时**给 ≥2 个候选 + 各自 trade-off**，让 user 自己拍。零经验用户最容易被一个权威的"就用 X"误导。

## 5. 解释要带原理，不要"业界一般这样"

读 `USER.md §5` 的 PCB 经验值定解释深度（经验为零 → 概念从头讲）。任何深度都带物理原因，例：
- ❌ "差分对要等长"
- ✅ "差分对要等长，因为 P/N 长度差变成时间差 → 共模噪声混进差分信号 → CMRR 下降"

## 6. 跟 USER.md 在手资源 cross-check（仅当讨论触及测试 / 焊接 / 采购）

任何方案落地前简单核对：能测吗？能焊吗？能买吗？任意一项 ❌ → 换方案。**不需要每次都列三个问题给用户**，只在相关时提。

## 7. Step 0 扫盲只问相关项

如果需求触及 USER.md `[待填]` 项，第一句话就问。**不相关的 [待填] 项不要问**。

## 8. 不硬编码 locale / vendor 名

任何 component-selecting 调用都读 USER.md §0 拿 locale。本 skill 提示里禁止写"日本" / "DigiKey JP" / "¥" 等字面值。

## 9. §5 BOM 表 ID 用 snake_case 功能名

§5 表 ID 列用人眼可读的功能名，**禁止 R1 / U2 / C1 电气编号**——电气编号是 Phase 3 sch 阶段才有，BOM 阶段引用 role 必须无需查表。下游 component-selecting / component-preparing / STATUS / change log 一律用同一个 snake_case ID 回引。

- ✅ `iso_amp` / `hv_div_top` / `c_in_np0` / `hv_terminal`
- ❌ `U2` / `R1` / `C1`

## 10. 落盘前给「初期可行性评估」

落盘前给 §5 spec 三态完整性检查（零裸空格）+ 一个 self-judgment 的可行性评估。

- ✅ 标明「**初期评估**」，纯逻辑推演（不变量 / 预算闭合 / 隔离 / 拓扑完整性）
- ❌ 说得像验证过——真验证（SPICE / DRC / EMC）在 Phase 3.5 / 4.5
- 详细 6 维 + 三态 verdict（GO / GO-with-risks / BLOCKED）→ `discussion_workflow.md`

## 11. 选品退回本 skill 只看结构动没动

选品撞墙退回时，判据是回路**结构**变没变，不是哪颗件换了。

- 🟢 纯换等效件（结构不变）→ **不回**本 skill，选品内部处理
- 🟡 松了安规 spec（隔离 / 耐压 / CTI）→ 回本 skill bless
- 🔴 动了拓扑结构 → circuit-design 重开
- 回退状态以 STATUS.md + §5 状态列为单一真相源 → `discussion_workflow.md`

## 12. 讨论期设计知识搜索按需触发

用户问方案 / 类别，或你要参考设计背书时**才搜**。

- ✅ 搜设计知识（参考设计 / app note / 拓扑套路 / 部件**类别**），**locale 无关**，给真实 URL，仍 ≥2 候选
- ❌ 搜价格 / 库存 / 买不买得到（那是回路冻结后 component-selecting 的事）
- 触发 / 搜什么 / 工具 / URL 规矩 → `design_search.md`
