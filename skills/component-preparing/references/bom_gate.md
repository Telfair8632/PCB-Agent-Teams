# BOM gate（Phase 2.5 收尾步骤）

> SKILL.md 是指南针；详细工作流 + fidelity 四检 + sentinel 协议都在这里。
> 入口：`scripts/check_readiness.py`（原 `bom-readiness/scripts/check_readiness.py`，2026-05 合并入本 skill）。

## 工作流（在 ①–⑤ 完成后执行）

```
全 MPN 落地后：
   ⑥ BOM gate（写 sentinel + 采购 BOM CSV）
       python3 skills/component-preparing/scripts/check_readiness.py \
         Projects/<name>/kicad/<name>.py
       → datasheets/.bom_readiness.json (all_pass + py_mtime)
       → datasheets/bom_<project>.csv（采购 BOM，distributor 下单用）
```

内部步骤：

```
输入：项目 .py（circuit-synth DSL）
   ↓
Step 1：AST 解析 .py 提取 (ref, value/MPN, footprint, symbol) 元件列表
   ↓
Step 2：元件三类分流
   ├─ generic：R/C/L/D/LED 等 + value 是规格描述（"100nF"）
   │   → 只查 library；不查 stock；不需 datasheet
   ├─ connector：物理连接器/开关 OR 通用 symbol + 真 MPN
   │   → 查 library + datasheet（如有真 MPN）；不查 stock
   └─ ic：真 IC / 模块（U1/U2 等真元件）
       → 全部查：library + stock + datasheet
   ↓
Step 3：5 项任务并行
   ├─ ① Library（深度判定 defer 给 component-preparing 前置步骤的 evidence）
   ├─ ② 可买性 evidence（仅 ic）：verdict=pass/warn_single_source +
   │    rollback_incomplete=false + vendor.status=active + vendor.url 存在
   ├─ ③ Datasheet evidence：<project>/datasheets/<MPN>.pdf 真实存在 + 页数 > 0
   ├─ ④ BOM CSV 生成：<project>/datasheets/bom_<project>.csv（按 MPN 分组）
   └─ ⑤ datasheets/ 目录管理：标记孤儿 / 缺失
   ↓
Step 4：写 sentinel
   <project>/datasheets/.bom_readiness.json
   {
     "verified_at": "ISO timestamp",
     "py_file": "...",
     "py_mtime": <epoch>,
     "components": [...],
     "datasheets_management": {"orphans": [...], "missing": [...]},
     "bom_csv": "...",
     "summary": {"total": N, "pass": N, "fail": N},
     "all_pass": true
   }
   ↓
Step 5：报告 + 退出码（pass=0 / fail=1）
```

## Fidelity 四检（硬门槛，任一 fail → all_pass=False → draw-schematic 入场被挡）

| 子检查 | 抓什么（血泪案）|
|---|---|
| **A. MPN 一致性** | .py `value` 字段 vs 项目 CLAUDE.md / docs/bom.md MPN 列。例：.py 写 "B0505S-1WR2" 但 BOM 写 "IB0505XT-1WR3" → fail。BOM 是 ground truth，.py 必须对齐。 |
| **B. 封装类一致性** | footprint 库前缀（`Package_DIP:` = TH / `Resistor_SMD:` = SMD）vs datasheet PDF 文字（"SMD 封装" / "SIP" / "DIP-N" / "贴片"）。例：footprint=DIP-4 (TH) 但 datasheet 是 SMD → fail。 |
| **C. 占位符伪装** | `ref` 是 U* 但 `symbol` 是通用占位（`Connector_Generic:Conn_01x04`、`Device:R/C/L/D`）+ `value` 是真 MPN。例：U2 用 Conn_01x04 装真 IC → fail。 |
| **D. pin 数一致性** | 连接器 `symbol` 的 `Conn_NxM` pin 数（行×列）vs component-selecting evidence 的 `key_parameters.positions`。两边都能解析出 pin 数才比对，任一侧缺失则跳过（不误报）。例：symbol `Conn_01x01`（1 pin）但 MPN 实际 2 pin → fail。 |

实现细节硬编码在 `scripts/check_readiness.py`，LLM 只读 sentinel 的 `all_pass` bool。

## 自动注入 MPN 属性（all_pass 后）

`all_pass=True` 时 check_readiness.py 默认调 `scripts/inject_mpn_props.py`，把 evidence 里已验证的 MPN / Datasheet / Manufacturer 作为 kwargs 补回 .py 的 `Component()` 调用：

- 幂等：已有 `MPN=` kwarg 的 `Component()` 跳过，重跑不重复注入
- 只补属性、不改电路结构；注入后刷新 sentinel `py_mtime`，避免 pipeline phase-0 误判「.py changed since sentinel」
- 目的：让生成的 .kicad_sch 带实例属性，过 check-schematic 的 SS-001（MPN coverage < 50%）/ DS-001
- 通用 R/C/L/D（value 即规格、无真 MPN、无 datasheet evidence）静默跳过
- `--no-inject-mpn` 关闭

## Datasheet 真实性规则

- 禁止创建 0-page / 1-byte / fake-but-plausible PDF 让 gate 通过
- 通用排针 / 通用 R/C/L/D 没有真 MPN 时可以 `datasheet=None`，不需要伪造
- 真 IC / 模块 / 明确 MPN 的连接器必须有真实 datasheet 或 component-selecting evidence；否则 fail-fast
- `datasheets/` 最终只应保留当前 BOM 中 MPN 的 PDF；落选件 PDF 由 component-selecting 提交后清理

## Sentinel 失效条件（draw-schematic 检 sentinel 时 reject）

- 文件不存在 → "BOM 没验过，先跑 component-preparing"
- `py_mtime` 跟当前 .py mtime 不一致 → "BOM 改过了，重跑 component-preparing"
- `all_pass=false` → "BOM 验过有 fail，修了再跑 component-preparing"

## Component-selecting evidence contract（gate 复检的 input）

check_readiness.py 不直接查询 LCSC/DigiKey/Mouser，也不下载库或 datasheet。
对真 IC / 模块，它只接受：

- `datasheets/component_selecting/<safe_mpn>.json` 存在且可解析
- `verdict` 是 `pass` 或 `warn_single_source`
- `rollback_incomplete=false`
- `vendor.status=active` 且 `vendor.url` 存在
- library/datasheet 的磁盘结果能和 evidence 对上
- datasheet PDF 必须是真 PDF 且页数 > 0；0-page / 1-byte stub 一律视为 missing
- `datasheets/component_selecting/` 根目录不得含 `_pending_*.json`、`lib_pending`、`fail`、`pending_llm_fetch`、`rollback_incomplete=true` 证据；这些只能在 `_scratch/`

缺任一项 → fail-fast，让用户先回 ①–⑤ 补 vendoring 或换 MPN。

## 命令行用法

```bash
VENV=".venv/bin/python"

# 标准模式：写 sentinel，gate 住 draw-schematic
$VENV skills/component-preparing/scripts/check_readiness.py \
  Projects/<name>/kicad/<name>.py

# 审查模式：复查老项目，不写 sentinel、不查库存、不下 datasheet
$VENV skills/component-preparing/scripts/check_readiness.py \
  Projects/<name>/kicad/<name>.py --audit
# 输出 datasheets/.bom_readiness_audit_<ts>.json，不影响 draw-schematic gate
```

**审查模式规则**：
- 自动 implies `--skip-stock` + `--no-csv`
- 不写 `.bom_readiness.json` sentinel
- 仍跑全部 fidelity ABCD + datasheets 目录扫描
- 退出码 = 0/1（无问题/有问题），适合 CI 跑

## 输出格式（pipeline 消费）

成功：
```json
{"all_pass": true, "components": [...], ...}
```

失败：
```json
{"all_pass": false, "fails": [
  {"ref": "U1", "mpn": "AMC1311DWV",
   "issue": "component-selecting evidence missing",
   "suggestion": "回 ①–⑤ 补 vendoring 或换已通过 locale vendor gate 的 MPN"}
]}
```

## 不重复造轮子

复用 draw-schematic/scripts/ 里的：
- `verify_footprints.py`：library 索引 + 4 层 fuzzy 匹配
- `download_datasheet.py`：仅复用 `project_datasheets_dir()` 做路径解析
