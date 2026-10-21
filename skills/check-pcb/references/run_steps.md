# Workflow — Step 0–8 详细

建议顺序，按需跳过。

## 开跑前 — roster_steps.py（数据存在性花名册）

```bash
python3 <skill-path>/scripts/roster_steps.py <project_root>   # 含 kicad/ + analysis/ 的项目目录
```

机械测量 .kicad_pcb / schematic.json / pcb.json / gerbers / ngspice / 网络 / MPN，
打印 9 步 READY / NO-DATA / TOOL-GATED + 理由；缺上游产物的 NO-DATA 行附解锁命令。
数据有无以它为准，scope（用户只要某项）由模型裁剪；跨域三项 NO-DATA 要解锁不要跳。

## Step 0 — 确认 sch.json 存在

```bash
ls analysis/<run_id>/schematic.json   # 由 check-schematic 产出
```

不存在就**先跑 check-schematic**（跨域三项 EMC / thermal / cross 需要它）。纯 pcb 检查可在没有 sch.json 时降级跑。

## Step 1 — pcb analyzer（**强制 `--full`**，跨域检查依赖 per-track 坐标）

```bash
python3 <skill-path>/scripts/analyze_pcb.py design.kicad_pcb --full \
  --analysis-dir analysis/
```

## Step 2 — sch↔pcb cross-reference

```bash
python3 <skill-path>/scripts/cross_analysis.py \
  --schematic analysis/<run_id>/schematic.json \
  --pcb       analysis/<run_id>/pcb.json \
  --analysis-dir analysis/
```

对不上的 ref / footprint / value 立即标 BLOCKER。

## Step 3 — Gerber 检查（如果导出了 fab 输出）

```bash
python3 <skill-path>/scripts/analyze_gerbers.py <release_dir>/gerbers/ \
  --analysis-dir analysis/
```

## Step 4 — Thermal hotspot

```bash
python3 <skill-path>/scripts/analyze_thermal.py \
  --schematic analysis/<run_id>/schematic.json \
  --pcb       analysis/<run_id>/pcb.json \
  --analysis-dir analysis/
```

## Step 5 — EMC pre-compliance（**总是跑**，44 条规则）

```bash
python3 <skill-path>/scripts/analyze_emc.py \
  --schematic analysis/<run_id>/schematic.json \
  --pcb       analysis/<run_id>/pcb.json \
  --analysis-dir analysis/ \
  --market eu                  # 可选：us/eu/automotive/medical/military
# 装了 ngspice 时加 --spice-enhanced：PDN / EMI 滤波器精度 ↑
```

## Step 6 — Parasitic-aware SPICE（可选）

```bash
python3 <skill-path>/scripts/extract_parasitics.py \
  analysis/<run_id>/pcb.json \
  --output analysis/<run_id>/parasitics.json
# 实际仿真在 check-schematic 跑（保持 SPICE 引擎归属一致）
python3 <check-schematic-skill-path>/scripts/simulate_subcircuits.py \
  analysis/<run_id>/schematic.json \
  --parasitics analysis/<run_id>/parasitics.json \
  --output analysis/<run_id>/spice_parasitic.json
```

何时跑：高阻反馈（>100kΩ）、LC 滤波 / RF matching、长模拟走线、高频电路。

## Step 7 — Lifecycle 审计（有联网 + MPN 时）

```bash
python3 <skill-path>/scripts/lifecycle_audit.py analysis/<run_id>/schematic.json \
  --temp-range industrial   # 可选：commercial/industrial/extended/automotive/military 或 'min,max'
# 无 --analysis-dir；positional 必须是 analyzer JSON 文件，传目录会 IsADirectoryError
```

查 obsolete / NRND / temp range mismatch。

## Step 8 — 写完整 design review

对照 `references/report-generation.md` 的最低 checklist：每个 analyzer 跑没跑都要交代；blockers 表 / verification basis / false-positive triage / skipped-analysis 四个 section 都要在。
