# Output JSON 字段速查

| 想要 | 路径 |
|---|---|
| Footprint 位置 | `pcb.footprints[].x / .y`（**无** `.position` 包装）|
| Zone net | `pcb.zones[].net` 是**整数 net ID**，不是字符串 |
| Track / via | `pcb.tracks[]` / `pcb.vias[]` |
| Findings | `findings[]` flat list，按 `rule_id` / `detector` 筛 |
| EMC 风险分 | `summary.emc_risk_score`（< 50 表示风险显著）|
| Per-net 风险 | `per_net_scores[]` |

完整 schema：
- `python3 <skill-path>/scripts/analyze_pcb.py --schema`
- `python3 <skill-path>/scripts/analyze_gerbers.py --schema`
- 详细 → `references/output-schema.md`
