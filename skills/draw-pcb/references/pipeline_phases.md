# Pipeline Phase 详细说明

**目录**:phase 总览 · 板框 + 隔离槽几何 · 输出 schema · L2 视觉验证清单 · 典型完整流程。
只有「板框 + 隔离槽几何」一节对主推回路有用,其余描述的是非主推的一键路径。

> ⚠️ **本文件描述的是 `scripts/pipeline.py` 这条一键路径，不是 draw-pcb 的主推流程。**
> 主推流程是 SKILL.md 的**工具箱 + A→D agentic 回路**（判断走回路、机械走脚本）。
> 两者差异要清楚：`pipeline.py` 的 Phase 2.8 是**无脑单面 B.Cu 铺地**，正是 `copper_pour.md`
> 点名的反模式；它也不跑 route-ready 验收、不跑 Phase E。**默认别用它**——读本文件是为了看
> 板框 / 隔离槽几何怎么算、各 phase 的 JSON 长什么样，不是为了照它跑。
>
> draw-pcb 内部 phase（不等于工作区 Phase 0–5；整个 draw-pcb 属于工作区 Phase 4）。
> 所有 phase fail-fast，每步输出 JSON。`pipeline.py` 自身不布线；**自动布线在 SKILL.md 的
> Phase E**（KRT），或由用户在 KiCad GUI 手布——不是"draw-pcb 不做走线"。

```
Phase 0:   Pre-flight       SCH + project CLAUDE.md + KiCad CLI + bundled Python
Phase 1:   SCH → PCB        创建空 .kicad_pcb（pcbnew API）
Phase 2:   Placement        HV/LV/ISO 分区 + 区域网格摆件 + Edge.Cuts 板框 + 隔离槽
                            CLAUDE.md 声明的 decoupling_pairs / chains 提前放近
Phase 2.7: Slot defense     auto-rotate ISO IC（HV/LV pads 反了就 180°）
                            + auto-move 任何 pad 落进 slot 的元件
Phase 2.75: Pad sweep       独立 pad-pad clearance sweep（mode_resolve_pad_conflicts）
                            必须在 2.7 之后跑——slot 移动可能引入 pad 冲突
Phase 2.77: In-board sweep  pad / body 落到板外的元件滑回 Edge.Cuts 内
Phase 2.8: GND zones        默认 B.Cu 单面铺地（HV/LV 物理隔离已足够）
                            HV_GND clip 到 slot 左、LV_GND clip 到 slot 右
                            可通过 helper spec `layers=['F.Cu','B.Cu']` 改双面
Phase 3:   DRC              kicad-cli pcb drc，结构化 JSON
Phase 4:   PDF / SVG        kicad-cli pcb export，给 Claude L2 视觉验证
Phase 5:   EMC              optional，调 check-pcb 的 analyze_emc.py 出风险报告
Phase 6:   Design review    optional，release/scripts/kidoc 出 markdown + PDF
              ↓ 生成完成后走线三选一：Phase E 自动布线(KRT) / 用户 GUI 手布 / 先不布
              ↓ 走完线重跑 DRC + 视觉（自动布线路径见 SKILL.md Phase E，含重铺铜）
Phase 7+ ↓ check-pcb skill：pcb analyzer / EMC / thermal / cross-ref / parasitic SPICE / gerber audit
         ↓ release skill：Design Review / HDD / 文档 + Gerber + vendor 决策
```

## 板框 + 隔离槽几何

```
┌──────────────────────────────────────────┐
│              ↕ 3mm bridge ↕              │
│   HV zone   ┌──┐         LV zone         │
│   ~40%      │槽│         ~60%            │
│             │  │                         │
│              ↕ 3mm bridge ↕              │
└──────────────────────────────────────────┘
              内部 cutout
              （顶/底各保留 3mm 板桥）
```

- **内部 cutout，不切到板外**——避免 ISO IC body 跨在两块独立板上
- **顶/底各 3mm 板桥**——机械刚性 + 板厂铣得动
- HV/LV 通过桥处 PCB 板材连接（机械），通过 slot 电气隔离
- 通过 `pcbnew.PCB_SHAPE` + `SHAPE_T_SEGMENT` 创建——不是字符串拼接

## 输出 schema

```json
{
  "ok": true,
  "phases": {
    "preflight":   {"ok": true, "claude_md": "...", "sch_path": "...", ...},
    "sch_to_pcb":  {"ok": true, "pcb_path": "...", "created": true},
    "placement":   {"ok": true, "footprints_placed": 33,
                    "zones": {"HV": 12, "LV": 15, "ISO": 2},
                    "board": {"w": 75, "h": 55, "slot_x": 33.1, ...}},
    "drc":         {"ok": true, "violation_count": 52, "by_type": {...}},
    "visuals":     {"ok": true, "pdf_path": "...", "svg_path": "..."},
    "emc":         {"ok": true, "findings_count": 11,
                    "by_severity": {"info": 6, "warning": 3, "error": 2}}
  },
  "next_step": "Route it: Phase E auto-route (scripts/tools/route.py, then re-pour zones, then run_drc) or hand-route in the KiCad GUI. Re-run DRC + visuals afterwards."
}
```

## L2 视觉验证清单（Claude 必读 PDF）

- 元件分布合理（不挤角落）
- HV/LV 隔离槽两侧分明
- 连接器在板边
- 高压电阻链纵列排
- 去耦电容靠近 IC（CLAUDE.md 声明的 decoupling_pairs 应该贴 IC）

## 典型完整流程（命令 + 输出示例）

```bash
# 1. 跑完 component-preparing + draw-schematic 后
$ python pipeline.py Projects/<name>/kicad

# 输出（典型）：
=== Phase 0: Pre-flight ===
  ✓ SCH: <name>.kicad_sch
  ✓ PCB: (will create)
  ✓ CLAUDE.md: yes
  ✓ kicad-cli: /Applications/KiCad/KiCad.app/.../kicad-cli
  ✓ KiCad Python: .../python3

=== Phase 1: SCH → PCB ===
  ✓ Created: <name>.kicad_pcb

=== Phase 2: Placement + board outline ===
  ✓ Placed 33 footprints, board 75×55mm

=== Phase 2.7 / 2.75 / 2.77: sanity sweeps ===
  ✓ no pads in slot / no pad conflicts / all in board

=== Phase 2.8: GND copper zones ===
  ✓ Added 3 ground zones (nets: GND,HV_GND,LV_GND)

=== Phase 3: DRC ===
  Violations: 12, Unconnected: 58
  (走线前的 unconnected 是正常的——板还没布)

=== Phase 4: PDF / SVG export ===
  ✓ PDF: <name>.pdf
  ✓ SVG: <name>.svg

✓ draw-pcb pipeline completed
```

接下来：

```
1. Claude Read PDF 做 L2 视觉验证（分区、间距、decoupling 贴紧）
2. 走线三选一：Phase E 自动布线（KRT）/ 用户 GUI 手布 / 先不布
3. 重跑 DRC + 视觉（自动布线路径必须先重铺铜再 DRC，见 SKILL.md Phase E）
4. 切给 check-pcb 做深度检查 / release 出货
```
