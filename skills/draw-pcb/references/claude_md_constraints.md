# 项目 CLAUDE.md 约束读取

placement v2 (`scripts/placement_v2/orchestrator.py::parse_claude_md_placement`)
解析项目 CLAUDE.md 里**带标题**的 YAML 块拿提示。位置约束：

1. 标题必须匹配 `## placement` / `## 布局` / `## Placement`（大小写无关）
2. 紧随其后必须是 ` ```yaml ` 围栏代码块
3. 块内键值见下表；找不到则用默认值

```markdown
## placement

\`\`\`yaml
placement:
  board_margin: 2.5
  isolation_slots:
    - between: [HV, LV]
      width_mm: 4.0
      reason: "reinforced isolation"
  anchors:
    U1: ISO
\`\`\`
```

## 字段

**字段表只有一处权威源 → `references/placement_rules.md` 的 v2 schema 段**（与
`placement_v2/orchestrator.py` + `floorplan.py` 逐键对齐）。本文件只管**位置约束**，不复述字段——
两处各写一份必然漂移，本文件此前那份就是错的。

三个最容易写错的点（照错写会**静默**走默认，不报错）：

- 隔离槽的键是 **`isolation_slots`**，槽用 **`between: [区域A, 区域B]` + `width_mm`** 声明。
  **`x_mm` 是输出不是输入**——槽的 X 由 floorplan 按区域排布算出(`floorplan.py:218`)，手填无效。
- 板内 inset 是**扁平** `board_margin`，不是嵌套 `board.margin`。
- 键名不匹配时 `parse_claude_md_placement` 直接返回 `{}` 走默认：**板上一条隔离槽都不会画，
  且全程 `ok: true`**。Phase A 必须打印「placement 块 found/not found + 解析出几条 isolation_slots」，
  数目对不上就是这里错了。

> 板宽 / 板高由 v2 floorplan 按 footprint 总面积估算，无需手填(`board_min_w/h` 只是下限)。
> 没有 `## placement` 块 → 默认值（自动 HV/LV 分区，无 slot，无 chain）。
> **`chains` 和 `decoupling_pairs` 是"提前放近"的声明**——layout 不做能量优化，只按声明 snap。

## CLAUDE.md 自动发现路径（preflight 优先级）

1. `<project_dir>/CLAUDE.md`
2. `<project_dir>/../CLAUDE.md`（典型场景：`Projects/<name>/CLAUDE.md` 在 `kicad/` 父级）
3. rglob 兜底
