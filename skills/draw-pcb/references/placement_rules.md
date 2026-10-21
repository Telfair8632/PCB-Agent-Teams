# Placement(确定性种子)

**目录**:4 阶段算法(分区 → 板框/槽 → 落位 → 应用) · **v2 schema 字段唯一权威源** ·
设计原则 · 排障。项目 CLAUDE.md 的 placement 字段全集在中段的 yaml 块。

draw-pcb 自带的确定性 4 阶段 placement pipeline——**只做区域划分 + 网格摆件**，不做能量优化。
它是 **agentic 回路的种子(起点)**，不是终点：种子"够用就行"，把它摆到 route-ready
是 SKILL.md A→D 回路的职责，**不甩给 KiCad GUI 人工补摆**。
> 别把种子算法越改越复杂去追求好布局——纯算法布局已验证效果差。种子保持简单稳定，质量靠回路。

## 算法

skill 默认行为，无需开关。项目 CLAUDE.md 可加 `placement:` 段提供 anchors / chains /
isolation_slots / decoupling_pairs 等**项目专属信息**精化结果，不加也能跑出合理默认值。

### 4 阶段 pipeline

```
A. partition  — netlist + footprint values → {ref: region}   (regex + 多 net 投票)
B. floorplan  — region 面积 → 板框 + Edge.Cuts + 隔离槽         (闭式几何)
C. layout     — 区域内网格摆件 + 声明的 pair / chain 提前放近   (确定性，无 SA)
D. writeback  — apply (x,y,rot) 到 .kicad_pcb                  (复用 _kicad_python_helper)
```

每阶段独立可测、确定性（fixed seed → 同输入同输出）、不依赖 LLM、不依赖 SA。

### Phase A — 投票分区

每个 footprint 按它接触的所有 net 跑 region regex，得票最多的 region 胜出。
平局 / 零票 → 用图邻接（共享非电源 net 的邻居投票）兜底。

**value/MPN 投票权重 = 5×单 net 投票**：iso DC-DC（TMA 0505S）两侧都接 +5V，纯 net 投票会归到 LV；value 含 `tma|0505` → ISO 直接锁定。

零票 + 无邻居 → fallback region（默认 'LV'）。

### Phase B — 闭式 floorplan

按 region 面积反推矩形（target aspect=1.4，pack density=0.55）。region 间留 gap，gap 中央**直接画 milling slot 到 Edge.Cuts**。slot 不再依赖 placement 收敛——它是 floorplan 阶段的几何输出。

板框 = region 矩形并集 + margin（默认 2.5mm）。可选 `board_min_w/h` 下限（手焊 / 机壳约束才用）。

### Phase C — 区域网格摆件

```
1. 计算 region 内 cell 大小（max(footprint w/h) × 1.2，最小 4mm）
2. 计算 cols × rows，必要时 re-pack 让 cell 数够装 ref
3. 排序 group：
   - 先：CLAUDE.md 声明的 chains（按声明顺序）
   - 再：CLAUDE.md 声明的 decoupling_pairs（IC 在前，cap 在后）
   - 最后：剩余 singleton
4. 蛇形填充 grid——slot keepout 重叠的 cell 自动跳过
5. 提前放近的 tight-snap：
   - decoupling pair: cap 紧贴 IC 东侧（IC.cx + IC_w/2 + cap_w/2 + 0.5mm）
   - chain: members 沿 region 长轴依声明顺序成直线
```

**没有 SA、没有 cost function、没有 HPWL**——种子故意保持简单。最终位置由 agentic 回路用 `move` 调到 route-ready。

slot keepout 的 hard 保证来自 Phase B：region 矩形天然不包含 slot 区域，layout 自然不会越界。Phase 2.7 / 2.75 / 2.77 兜底 sanity sweep 处理边角 case。

### CLAUDE.md placement schema

skill 不需要任何项目级 config 就能跑。下面字段**全是可选**——有就细化，没有就默认。

```yaml
placement:
  orientation: horizontal            # or 'vertical'
  board_min_w: 60                    # mm，手焊 / 机壳约束才填
  board_min_h: 45
  aspect_ratio: 1.4
  pack_density: 0.55
  board_margin: 2.5                  # 板内 inset(mm)，给走线留 keepout。扁平键，非 board.margin
  region_order: [HV, ISO, LV]        # 区域左→右排布顺序，不填按 default

  anchors:                           # 手动 region 强制覆盖
    J1: HV
    J2: LV

  isolation_slots:
    - between: [HV, ISO]
      width_mm: 4.0
      reason: "AMC1311 reinforced 5kV"
    - between: [ISO, LV]
      width_mm: 4.0

  chains:                            # 声明顺序 = layout 顺序
    - members: [J1, R1, R2, R3, R4, R5]

  decoupling_pairs:                  # cap 会被贴到 IC 东侧 ≤1mm
    - [C8, U1]
    - [C11, U1]

  region_regex:                      # 仅当 default HV/ISO/LV 不适用时
    POWER:  "(?i)(\\+12v|\\+24v|vbat)"
    DIGITAL: "(?i)(d_\\w+|sclk|sda|miso)"
  value_regex:                       # 按 value 而非 net 分区，同上仅特殊 case
    HV: "(?i)(1206|2010)"
```

> **本段是 placement 字段的唯一权威源**（`claude_md_constraints.md` 只管标题 + 围栏的位置约束）。
> 实现:`scripts/placement_v2/orchestrator.py`(总控) + `scripts/placement_v2/partition.py`(A 分区)
> + `scripts/placement_v2/floorplan.py`(B 板框/槽) + `scripts/placement_v2/layout.py`(C 落位)
> + `scripts/placement_v2/__init__.py`。
> 改字段前先对 `placement_v2/orchestrator.py` 的 `cfg.get(...)` 与 `floorplan.py` 的槽解析核一遍——
> 键名对不上时 parser 静默返回 `{}` 走默认，**不报错**。

### 设计原则

- **skill 不含项目特定知识**——HV/LV 不是写死的，default regex 只是常见 case。任何项目都可以重新定义 region。
- **每 phase 单独可调**——A 出错就是 partition 错（看 diagnostics 字段），B 错就是 floorplan，C 错就是 grid / pair snap。不是黑盒。
- **fixed seed**——同一份 CLAUDE.md 跑出来永远同一块板。
- **种子够用就好**——种子只做区域划分 + 预设邻接，不追求"画好看"；摆到 route-ready 交给 agentic 回路。

### 故障排查

| 现象 | 看哪 | 怎么修 |
|---|---|---|
| connector 跑错 region | Phase A diagnostics method=majority/adjacency_fallback | 加 `anchors: {J1: HV}` |
| iso barrier IC 没识别 | Phase A votes={ISO: 0} | 把 IC 加进 `value_regex.ISO` |
| 想让某几颗件挨着 | Phase C 没把它们 snap 到一起 | 加 `decoupling_pairs:` 或 `chains:` 声明 |
| 板太大 | Phase B board.w/h | 调 `pack_density: 0.7` |
| 隔离槽漏画 | Phase B slots=[] | 加 `isolation_slots:` 声明 |
| 元件挤一团 | Phase C cell 太小 | 回路里用 `move` 摊开，不改种子算法 |
