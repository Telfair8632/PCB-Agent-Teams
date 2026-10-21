# 适用性审查 — 5 维度核查 + evidence schema

`component-preparing` ⓪ 阶段的完整参考。本文件给：进 ⓪ 的 gate 条件、per-role 流程、维度细则、verdict 档位、evidence 字段 schema、风险接受规则。

## 进 ⓪ 的前提（gate）

- shortlist 进 preparing 时 user **还没**拍 top pick；⓪ 先用 datasheet 把候选跟项目 §5 锚点件 spec 对齐一遍，再让 user 拍。
- **拿不到项目 `Projects/<name>/CLAUDE.md §5` → 不允许进 ⓪**（§5 是核查的 ground truth，缺它无从对齐）。

## 流程（per role，从 top-1 开始）

1. 单点抓 top-1 datasheet PDF（`bulk_fetch_datasheets.py` 单 MPN 模式；落 `datasheets/`，**先不写 evidence** —— accept 后才落 evidence）
2. LLM 按下方 5 维度核查（**全做不挑**）；每条 verdict 必须带支撑句（datasheet 页码 / 项目 §5 行号）
3. markdown 报告交 user，三选一拍板：
   - **accept / accept_with_risk** → ⓪ 报告并入 evidence `applicability` 字段，进下一步
   - **swap_next** → 取 shortlist[+1]，⓪ 重跑（旧 PDF 留 `datasheets/`，⑤ 阶段 `clean_orphan_datasheets.py --apply` 自动清理）
   - **back_to_selecting** → spec 错位严重，user 自跳 component-selecting 改 longlist

## 5 核查维度（全做不挑）

| 维度 | 跟谁对 | datasheet 抓哪段 |
|---|---|---|
| **电气量** | 项目 CLAUDE.md §5（V/I 范围、isolation rating、gain、BW、accuracy 等）| Abs Max + Recommended Operating + Electrical Characteristics |
| **Pinout / pkg** | circuit-design 假设的封装（pin 数 / pitch / 形态） | Package Drawing + Pin Configuration |
| **工作环境** | 项目 Vcc 范围 + 温度等级（车规 / 工业 / 商业） | Recommended Operating（温度行 + 供电行） |
| **推荐拓扑** | 当前回路 vs datasheet typical application | Application Information / Typical Circuit / 推荐外围件清单 |
| **Warnings / errata** | 项目锚点件假设（startup 行为 / 输入保护 / 已知坑） | Notes / Errata（vendor errata 通常单独 PDF）/ Reliability |

**支撑句要求**：每条 verdict 必须引 datasheet 页码 / 项目 §5 行号。报告里出现没有支撑的 verdict，等于没审。

## Verdict 三档

- **pass** — 5 维度全 pass，无 concern
- **pass_with_concerns** — 多数维度 pass，1-2 条带 concern（如温度等级是工业级而项目要求商业级，但项目实际工作温度落在工业级范围内 → concern 但不致命）
- **fail** — 任一维度 fail（如电气量 abs max 跟项目供电直接冲突，或 pinout 跟 circuit-design 假设不匹配）

## evidence JSON `applicability` 字段

accept 时 LLM 把这块 JSON 写入 per-MPN evidence（`datasheets/component_selecting/<safe_mpn>.json`），无脚本依赖；位置紧挨 `library` / `datasheet` 同级。

```json
{
  "applicability": {
    "reviewed_at": "<ISO8601>",
    "datasheet_revision": "<rev + date，从 PDF 封面或 footer 抓>",
    "verdict": "pass | pass_with_concerns | fail",
    "dimensions": {
      "electrical":  "pass|concern|fail — <一句话 + 支撑（页码 / §5 行号）>",
      "pinout":      "...",
      "environment": "...",
      "topology":    "...",
      "warnings":    "..."
    },
    "user_decision": "accept | accept_with_risk | swap_next | back_to_selecting",
    "user_note":     "<拍板理由；accept_with_risk 时必填>"
  }
}
```

## 风险接受规则

- `verdict=pass` + `user_decision=accept` — 默认路径
- `verdict=pass_with_concerns` + `user_decision=accept_with_risk` — 合法，但 `user_note` 必须写明：(1) 接受了哪几条 concern，(2) 缓解措施（设计兜底 / 测试覆盖 / 替换计划）。release 阶段 design review 会回查这些 note
- `verdict=fail` + `user_decision=accept_with_risk` — **不允许**；fail 必须 swap_next 或 back_to_selecting
- `user_decision=swap_next` — 旧 PDF 留在 `datasheets/` 不删，由 ⑤ 阶段 `clean_orphan_datasheets.py --apply` 自动清理（孤儿判断：没进 evidence JSON 的 PDF 就是孤儿）

## 反例

- ❌ 写「电气量 OK」不引数 → 必须改成「电气量 pass — V_in_max 60V 覆盖项目 §5 第 12 行 35V_max 要求（datasheet p.4）」
- ❌ 把 datasheet typical application 直接套当前回路，不验外围件清单（典型坑：iso amp 的 typical 推荐 LDO 是 5V 而项目用 3.3V → 推荐拓扑维度应判 concern/fail）
- ❌ 跳过 errata（vendor errata 通常跟主 datasheet 分开发布，要额外从 distributor / vendor 官网拿；忽略它 = 留雷）
- ❌ 用 footprint 库前缀「看着像」就过 pinout 维度 → 必须对 pin 顺序、pin function、pitch 三项跟 datasheet pkg drawing 直接 diff
