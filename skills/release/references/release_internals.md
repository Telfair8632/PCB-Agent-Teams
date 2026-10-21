# release umbrella 内部结构

> SKILL.md 是指南针；详细子工序构成 / 命令行 / 输出目录 / vendor 决策表 / 文档生成详情都在这里。
> 历史职责合并：原 `kidoc` / `kicad`（fab export 部分）/ `fab` 三个 skill 已合并入 release（2026-05）。

## 子工序构成

```
release/
├── SKILL.md                     ← 入口（compass）
├── scripts/
│   ├── build_release.py         ← 主编排（聚合 + 转格式 + 打包）
│   ├── export_gerbers.py        ← Gerber/Drill/CPL/生产 BOM 导出（原 kicad skill）
│   ├── fab_release_gate.py      ← release 前预检（原 kicad skill）
│   ├── coverage_scan.py         ← 各 vendor 覆盖率扫描
│   ├── distributor_csv.py       ← DigiKey / Mouser / LCSC 上传格式转换
│   ├── render_order_guide.py    ← ORDER_GUIDE.md 渲染
│   └── kidoc/                   ← 文档生成子模块（原 kidoc skill scripts）
│       ├── kidoc_scaffold.py
│       ├── kidoc_generate.py
│       ├── kidoc_diagrams.py
│       ├── kidoc_orchestrator.py
│       ├── kidoc_narrative.py
│       └── ...
├── references/
│   ├── distributor_csv_formats.md  ← 各家 BOM 上传列名规范
│   ├── jlcpcb.md                   ← JLCPCB 工艺百科（原 fab skill）
│   ├── pcbway.md                   ← PCBWay 工艺百科（原 fab skill）
│   ├── kidoc/                      ← kidoc 文档类型 + 章节配置 + 渲染选项
│   │   ├── document-structure.md
│   │   ├── rendering-notes.md
│   │   └── report-*.md（每种文档类型的章节模板）
│   └── release_internals.md        ← 本文件
└── templates/                   ← ORDER_GUIDE / kidoc 报告模板
```

## 命令行用法

```bash
VENV=".venv/bin/python"

# 标准模式：完整 release（gate 检查 + 全部子工序）
$VENV skills/release/scripts/build_release.py Projects/<name>

# 跳过 Gerber 重导（复用上次结果）
$VENV skills/release/scripts/build_release.py Projects/<name> --skip-fab-export

# 只验证 gate，不写文件
$VENV skills/release/scripts/build_release.py Projects/<name> --dry-run

# 同 ts 已存在时强制覆盖
$VENV skills/release/scripts/build_release.py Projects/<name> --force

# 单工序：只导 Gerber
$VENV skills/release/scripts/export_gerbers.py Projects/<name>/kicad/<name>.kicad_pcb

# 单工序：只生成 HDD PDF
$VENV skills/release/scripts/kidoc/kidoc_scaffold.py \
  --project-dir Projects/<name>/kicad --type hdd \
  --output Projects/<name>/release/<ts>/docs/HDD.md
```

## Gate 行为（入场必满足）

- `Projects/<name>/datasheets/.bom_readiness.json` 存在 + `all_pass=true`
- `.kicad_pcb` mtime ≤ sentinel `verified_at`（PCB 改过就 fail）
- check-pcb verdict.json（待 check-pcb 落 verdict 协议后切换）

任一 fail → 退出码 1 + 提示用户回去跑相应 skill。

## BOM 复核（轻量 gate，与 component-preparing 互补）

component-preparing 已写好 sentinel + 采购 CSV。release 入场时：

| 状态 | 行为 |
|---|---|
| sentinel 存在 + all_pass + mtime ≤ pcb mtime | ✅ 直接跳过，复用现有 CSV |
| sentinel 缺 / stale / fail | ❌ 提示用户回 component-preparing 重跑（不在本 skill 自动重跑） |
| 采购 CSV 缺但 sentinel pass | ⚠ 自动重生成 CSV（仅平移，不重新评估）|

> 复核 ≠ 重做。release 不做 fidelity ABC 三检（那是 component-preparing 的职责）。

## 输出目录结构

```
Projects/<name>/release/<ts>/
├── ORDER_GUIDE.md              ← 用户第一眼看的跳转指南
├── coverage_matrix.md          ← 各 vendor 覆盖率
├── fab_options.md              ← JLCPCB vs PCBWay 决策表
├── release_manifest.json       ← 上游产物时间戳追溯
├── pcb_fab/                    ← 给 fab 厂上传
│   ├── gerbers/                # Gerber + Drill + drill map
│   ├── assembly/<board>_positions.csv         # CPL
│   ├── assembly/<board>_assembly_bom.csv      # 生产 BOM
│   └── <board>_fab.zip
├── procurement/                ← 给 distributor 上传
│   ├── bom_<project>.csv       (主，采购 BOM)
│   ├── digikey_bulk.csv        (DK BOM Manager)
│   ├── mouser_bom.csv          (Mouser BOM Tool)
│   └── lcsc_bom.csv            (LCSC BOM)
├── datasheets/                 ← 焊接 / debug 时查 pin
├── docs/                       ← kidoc 产出（HDD / CE / Design Review / ICD / Manufacturing PDF）
└── release_<ts>.zip            ← 总包
```

`<ts>` 格式：`YYYYMMDD_HHMMSS`（JST）。

## Vendor 路由（Gerber 上传选哪家）

| 场景 | 推荐 | 理由 |
|---|---|---|
| 元件全在 LCSC + 量产 / 原型 | **JLCPCB** | basic parts 不收上料费，最便宜 |
| 元件分散多 vendor（DK/Mouser 都有） | **PCBWay** | turnkey assembly，全球采购 |
| 多层板 / HDI / 阻抗控制 | **PCBWay** | 工艺更全 |
| 普通双层 / 4 层 | **JLCPCB** | 价格优势 |
| 客户在欧美 | **PCBWay** | 全球仓更多 |
| 客户在中国 | **JLCPCB** | 国内发货便宜快 |

> 详细工艺规格（最小线宽 / 最小孔径 / 板厚 / 铜厚 / 装配方式 / 起订 / 价格）→
> `references/jlcpcb.md` / `references/pcbway.md`

## 文档生成（原 kidoc 职责）

| 类型 | 文件 | 关键章节 |
|------|------|---------|
| `hdd` | Hardware Design Description | 系统总览、电源、信号、模拟、热、EMC、PCB、机械、BOM、测试、合规 |
| `ce_technical_file` | CE Technical File | 产品 ID、基本要求、协调标准、风险评估、DoC |
| `design_review` | Design Review Package | 跨分析器评分、findings、action items |
| `icd` | Interface Control Document | 接口列表、connector pinout、电气特性 |
| `manufacturing` | Manufacturing Transfer Package | 装配总览、PCB fab notes、装配指引、测试流程 |
| `schematic_review` / `power_analysis` / `emc_report` | 单视角报告 | 见 `references/kidoc/` |

> kidoc 子模块工作流（scaffold → 填 narrative → regenerate → 渲染 PDF）+
> 章节配置 / 自定义 spec / 渲染选项 → `references/kidoc/document-structure.md` +
> `references/kidoc/rendering-notes.md`

## 跟其他 skill 的关系

```
[check-pcb pass]
   ↓
[release umbrella，本 skill]
   ├─ 调 scripts/export_gerbers.py（Gerber/CPL/生产 BOM）
   ├─ 读 component-preparing sentinel + 采购 CSV（消费）
   ├─ 读 component-selecting evidence JSON（消费）
   ├─ 调 scripts/kidoc/kidoc_*.py（HDD / CE / Design Review PDF）
   ├─ 读 references/jlcpcb.md / pcbway.md（vendor 决策）
   └─ 读 check-pcb verdict.json（gate 协议，待落地）
```
