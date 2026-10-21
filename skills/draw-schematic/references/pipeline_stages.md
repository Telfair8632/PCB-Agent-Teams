# Pipeline Stages — Stage 1–5 详细

> Stage / Phase 是 `draw-schematic` skill 内部编号，不等于工作区 Phase 0–8；工作区里写 `.py` 是 Phase 3，跑 BOM gate 是 Phase 4，生成 `.kicad_sch` 是 Phase 5。

> 历史上 pipeline.py 会**内联**调 kicad analyzer + SPICE gate（`_analysis/sch_analysis.json`、`spice_report.json`）作为生成期 pre-flight。这些深度检查现在归属 **`check-schematic`** skill。pipeline.py 仍可作为快速 sanity 跑这两个，但**完整 design review 必须由 check-schematic 出**——不要靠 pipeline 输出当 review 用。

---

## Stage 1 — 读 CLAUDE.md 提 BOM

每个项目 `Projects/<name>/CLAUDE.md` 必须有：
- **完整 BOM 表**（编号 / 数量 / MPN / 封装）
- **完整原理图段**（ASCII art 或表格描述每个 net）
- **接口定义**（J1/J2/J3 等的 pin 含义）

读完后构建：
- 元件清单 `[(ref, mpn, value, footprint), ...]`
- net 拓扑 `{net_name: [(ref, pin), ...]}`

---

## Stage 2 — 元件库 / footprint 对齐（只消费 gate）

**铁律**：draw-schematic 不做选型、不查库存、不下载新库。每个真 MPN 必须已经由 `component-preparing` 写好 evidence + `datasheets/.bom_readiness.json` sentinel。

> ⚠️ **lib_external 的所有规则**（命名空间/单库约定、写入入口、同 MPN 二次 vendor 的覆盖坑、清理 GC、3D model 路径坑、git 策略）→ **见 `lib_external/CONVENTIONS.md`**。本文件不重复。

本阶段三件事：
1. 读 component-preparing 写的 `.bom_readiness.json` sentinel，确认 `all_pass=true` 且 `.py` mtime 没变。
2. 用 `verify_footprints.py --fix` 修库名拼写/单候选命名差异。
3. 若 footprint 0 候选或多候选，直接 fail，让用户回 `component-selecting` / `component-preparing` 修。

**已知陷阱**：
- `component-selecting` 可能按 locale 选择 DigiKey/Mouser/element14 等来源；不要在 draw-schematic 里硬编码 LCSC。
- vendor 库数据 pin 顺序可能跟厂商 datasheet 不一致 → component-selecting / component-preparing 必须先校验，draw-schematic 只消费结果。

---

## Stage 3 — 写 .py

**默认单图**：一个 `@circuit` 函数把所有元件写进去。完整模板 → `examples/README.md`。

> **为什么单图为默认**：
> 1. `fix_labels.py` 用 kicad-sch-api 精确 pin 坐标重写 label，已经压住 circuit-synth 上游的 label coord bug（known-bugs.md #2），元件数量不是单图的硬上限。
> 2. **检查工具受益**：`check-schematic` 的 `analyze_schematic.py` / `detect_rc_filters` / `simulate_subcircuits.py` 都假设"一个名字一个 net"。hierarchical 下 KiCad 会把同名 net 拆成 `name`（顶层）+ `/<uuid>/name`（子图）两份，检测器看不到 R-C 共享 net 的对，信号链 fc 验证全失效。单图无此问题。
> 3. **少一整套 patch**：hierarchical 上游有 `_is_net_hierarchical` 写错（known-bugs.md #3）+ sheet_pin↔hier_label 不连等问题，本仓 `add_hier_labels.py` 56 个补丁。单图不需要这些。
>
> **视觉密集**：单图 38+ 元件 label 会飘 — 这是 label-only 模式的副作用（pipeline 不画 wire 防穿 pin）。用户在 KiCad GUI 拖元件二次美化（一次性 10-20 分钟）。**电气 100% 对**（pipeline L3 拓扑校验保证）。

**仅在以下情况**才拆 hierarchical（每子图 ≤ 15 元件）：
- 单图 ≥38 元件且实测 fix_labels 仍漏修出现 net 错合并
- 业务需求：IP 复用 / 子板分文件交付 / 团队多人并行编辑

切到 hierarchical 时 pipeline 自动迭代所有子 sch 跑 fix_labels + 自动补 hier_label，但要接受检测器精度下降。

---

## Stage 4 — Pipeline 脚本顺序

| Step | 脚本 | 职责 |
|---|---|---|
| 0 | `scripts/verify_footprints.py --fix` | **L4 预检**：扫 .py 所有 footprint 字符串 → 验存在性 → 库名错或命名差异自动改 .py |
| 1 | `circuit_synth` 跑 .py | 出基础 sch（用修过的 .py，确保 sch 里 footprint 字符串干净）|
| 2 | `scripts/fix_labels.py` | 删 PWR + hierarchical_label，加精确 local label |
| 3 | `kicad-cli sch erc --format json` | L1 第一遍，按 type+severity 分类计数 |
| 4 | `scripts/add_pwr_flags.py`（auto, 仅 power_pin_not_driven > 0 时）| 给 ERC 抱怨的 power 输入 net 自动加 PWR_FLAG，重跑 ERC |
| 5 | `kicad-cli sch export pdf` + `verify_topology.py` | L2 出图 + L3 拓扑 |

**不画 wire**：靠 KiCad 同名 label 自动连接（label-only 模式，电气 100% 等价于 wire，视觉是飘 label）。

**PWR_FLAG 自动注入**（v2 改进）：fix_labels.py 会删所有 `power:` 类符号（避免 #PWR 多重叠 short），副作用是 PWR_FLAG 也被删 → 每个项目都会出 `power_pin_not_driven` 错。Pipeline v2 检测到此错时：
1. 解析 ERC json 提取所有 `power_pin_not_driven` 涉及的 (ref, pin)
2. 查 COMPONENT_NETS 拿到 net 名
3. 在 sch 右上角空地批量插 `power:PWR_FLAG` + 同名 local label
4. 重跑 ERC 一次

**永远不要**自己手动逐个加 PWR_FLAG —— 这一步现在硬编码自动跑。

---

## Stage 5 — 三层视觉验证（铁律，每次都跑）

### L1 数据层（铁律：用 JSON + total_errors）

❌ **不要再用 grep 单一类型错误**（之前血泪：grep `pin_not_connected` 正好 0，但 `power_pin_not_driven` 有 4 个 / `endpoint_off_grid` 有 22 个被全部放过）

✅ **必须用 `--format json` 拿结构化输出，按 severity 数总错数**：

```bash
SCH=path/to/proj.kicad_sch
/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli sch erc \
  --format json -o /tmp/erc.json "$SCH"

python3 -c "
import json
d = json.loads(open('/tmp/erc.json').read())
errs = warns = 0
by_type = {}
for sh in d.get('sheets', []):
    for v in sh.get('violations', []):
        sev = v.get('severity'); t = v.get('type')
        if sev == 'error':   errs += 1
        if sev == 'warning': warns += 1
        by_type.setdefault((sev, t), 0)
        by_type[(sev, t)] += 1
print(f'errors={errs}, warnings={warns}')
print(f'by_type={by_type}')
"

echo "wire 段:  $(grep -oE '\(wire ' "$SCH" | wc -l)"
echo "label:    $(grep -oE '\(label ' "$SCH" | wc -l)"
echo "lib_id:   $(grep -oE 'lib_id' "$SCH" | wc -l)"
```

**门槛（缺一不可）**：
- `errors == 0`（**总错数**，不是单一类型）
- `lib_id ≥ 元件数`
- `label ≥ net 数`

**Pipeline 自动跑这一步**（`pipeline.py` v2），LLM 只读输出 dict 的 `l1_total_errors == 0`。

### L2 视觉层

```bash
PDF="${SCH%.kicad_sch}.pdf"
/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli sch export pdf -o "$PDF" "$SCH"
# Claude 用 Read 工具读 PDF —— 内置渲染图片 → 视觉判断
```

**Claude 必须真看图**，**不能只看 ERC 数字就报告完成**：
- 元件分布是否合理（不挤角落）
- 元件之间有 wire 连接
- label 不重叠堆积
- 总体跟"标准电路图"印象一致

### L3 拓扑层

读项目 CLAUDE.md 的"完整原理图"段，逐 net 跟生成的 PDF 比对：
- HV+ / HV_DIVx / HV_GND 拓扑一致
- HV/LV 隔离区分清楚
- 差分对（OUTP/N, ADC_P/N）紧贴
- 去耦电容靠近 IC

**任一项不通过 → 不交付**。

### L4 footprint 可用性层（铁律：sch 阶段就验，不拖到 PCB）

为啥独立成层：ERC 把 footprint 引用错只算 warning（电气上确实没事）。但 PCB 阶段 F8 (Update PCB from Schematic) 时 KiCad 会报 `Footprint not found` → 元件不进板 → 飞线全断 → 没法布线。**必须在 sch 阶段就堵死**。

```bash
".venv/bin/python" \
  scripts/verify_footprints.py <project.py> --fix
```

输出三类：
- ✅ **直接 OK**：lib + footprint name 都存在 → 不动
- 🔧 **自动修**：恰好 1 候选 → sed 改 .py（典型情况：库名拼错、SOIC-8W vs SOIC-8、value=EG1218 → 找到 lib_external 里 SW-TH_EG1218）
- ⚠ **需手工**：候选 > 1（pitch 差异等）或 0 候选（库里真没有）→ 列给用户

**4 层 fuzzy 策略**（验证脚本内部）：
1. 同名跨库（库名拼错）
2. 归一化 fuzzy（SOIC-8W ↔ SOIC-8 的 W 后缀差异）
3. value (MPN) 子串 → lib_external 文件名匹配
4. 原 footprint 名的"型号 token"匹配（如 MKDS-5-2、SOIC-8）

**Pipeline gate**：L4 needs_manual 非空 → `ok = false`，明确让用户去补 lib_external 或选候选。不让 sch 阶段假装通过，到 PCB 阶段才崩。

---

## L4-bis：footprint 0 候选时的处理

**触发条件**：L4 verify 报某元件"全库找不到（0 候选）"。说明：
- KiCad 自带库 + lib_external 都没有
- value (MPN) 子串也匹配不到任何 vendored footprint

**处理规则**：
- pipeline 不自动下载新库，直接 `ok=false`。
- 回到 `component-preparing` 重新选择/提交该 MPN，或者换一个当前 locale 可买且库完整的替代型号。
- 再跑 `bom-readiness`，只有 sentinel `all_pass=true` 后才能回 draw-schematic。

**铁律**：
- LLM 不要凭记忆猜 MPN（Phoenix/TI 命名不规则，每年新增型号）。
- LLM 不要不查就改 .py 的 value（可能买不到，或买错型号烧板）。
- MPN 修改属于设计层变更：同步改项目 CLAUDE.md 的 BOM 表，并让用户审。

---

## L4-tris：datasheet 只消费 evidence

每个真 MPN 的 datasheet 必须由 `component-preparing` 写进项目 `datasheets/` 并记录到 `datasheets/component_selecting/<MPN>.json`。draw-schematic 不下 datasheet；缺文件时由 component-preparing 的 BOM gate fail-fast。

**文件命名建议**：保留 MPN 作为文件名的一部分，便于 component-preparing 和人工 grep 找到。
