# 讨论期设计知识搜索（on-demand）

**触发**：讨论拓扑 / 锚点时，用户问「这块用什么方案 / 什么类别的件 / 有没有参考设计」，**或**你需要参考设计 / app note 来支撑候选——**才搜**。
**不主动每块都搜**：不改 propose 行为，rule 4 照旧（≥2 候选 + trade-off，搜索只给候选加证据，不替用户拍板）。

## 搜什么 / 不搜什么

✅ 搜（设计知识，**locale 无关**）：
- 参考设计 / reference design / app note / design guide
- 拓扑套路（如 `isolated voltage sense frontend topology`）
- 部件**类别**（如 `isolated delta-sigma amplifier family for ±2V`）——是**类别**，不是某颗买定的 MPN

❌ 不搜（→ `component-selecting`，已按 USER.md §0 locale 路由）：
- 价格 / 库存 / 买不买得到 / 在某分销商有没有货
- 选定某颗具体 MPN 当 BOM（那是回路冻结后 component-selecting 的事）

> **为什么 locale 无关**：一份 app note 在哪个地区都一样。买得到 / 本地生态由 `USER.md §0`（能力 + 偏好）+ `component-selecting`（buyable）管——别在这搜 locale，否则就把 component-selecting 重复了一遍。

## 工具顺序

搜索**只用 Tavily**：`mcp__tavily__tavily_search`（`search_depth=advanced`）——已配 user-scope MCP，全项目常驻可用。
读全文：`tavily_extract`（最完整）→ 内置 `WebFetch`（兜底）。

## 输出规矩

- 给**真实可点 URL**（来自搜索结果本身，**禁止编造 / 推断**——同 shortlist URL 铁律）
- 一两句话提炼设计要点，**别贴整页**
- 结论**挂回候选**，例：「候选 A 有现成参考设计背书（URL）；候选 B 没现成参考，未知风险更高」
- 仍 **≥2 候选 + trade-off**——搜索是给候选**加证据**，不是压成一个推荐
