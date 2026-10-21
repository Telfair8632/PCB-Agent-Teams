# 已知限制

| 限制 | 缓解 |
|---|---|
| **courtyard 缺失/退化让闸门漏判重叠** | 两种病同根:某些 footprint 的 courtyard 退化成一条线(w 或 h≈0),而**转换库(立创/easyeda 转 KiCad)常整库没有 CrtYd 层**。`get_geometry` 三级回退:courtyard(非退化)→ **silk/fab/图元 bbox ∪ pads** → pad-bbox(仅此级打 `geometry_uncertain`);`check_placement` 的 `metrics.extent_source` 统计三级各多少件。规则:**被打 `geometry_uncertain` 的件,按它的 footprint 名推真实体积再摆**(如 `CP_Radial_D10.0mm` = 10mm 径向电解,真实 courtyard ≈11×11mm,远大于 pad-bbox 估的几 mm)。⚠ **库缺 CrtYd 时 `run_drc` 兜不住**——KiCad 的 courtyard DRC 同样依赖该层,实测实体互插 2mm+ 照样 0 error;此时闸门的图元 bbox 回退是唯一自动防线,`pad_bbox > 0` 必须渲染目检。 |
| **pad 的 `at` 角是绝对角，`size` 是未旋转库值** | 板文件里 pad 的位置是 footprint 相对值（未旋转），但**角度已经含了 footprint 的旋转**（pcbnew 存盘写 `GetOrientation()`）。所以：**绝对 pad 角 = pad 自己那个角，不要再加 footprint 角**——加了等于把 90° 件转成 180°。而 `size` 永远是库里的未旋转尺寸，rot 90 的件在板上是"库高 × 库宽"。`extract_footprints` 已产出 `bbox_w/bbox_h`（board 空间 AABB = 铜 ∪ 孔，与 pcbnew `PAD::GetBoundingBox` 对齐），`get_geometry` 的 `pads[].w/h` 即此值。规则：**任何拿 pad 几何跟板坐标比的判断（间距 / 包含 / 重叠）只能用 bbox 字段**；`width/height` 只用于面积和 pitch 这类旋转无关量。回归测试 `tests/test_pad_geometry.py`。 |
| **隔离槽 vs THT 器件物理冲突** | barrier 器件若是 THT SIP(pad pitch < 槽宽,如 SIP-4 pitch 2.54mm vs 槽宽 4.0mm),贯穿槽会切穿 pad → DRC `copper_edge_clearance`。**Phase D 的 `bridge_slot` 工具已自动解决**:重画槽,在每个跨槽 barrier 器件下方留实体桥(器件自身即隔离屏障,桥宽 ≥ 器件体高时仍满足爬电)。`bridge_slot` 须在元件摆定后跑。 |
| 某些 footprint anchor 不在 body 中心（如 `Package_DIP:DIP-4_W7.62mm` anchor 在 pad 1），直接 SetPosition 会让 pad 落进 slot | apply_layout 用 bbox center 对齐 + Phase 2.7 防御 |
| 4-pin 单列模块（B0505/IB0505 等 SIP-4）的 .py 里 net 接法可能跟 footprint pin 1 起算方向相反 | Phase 2.7 自动检测 ISO IC 的 HV/LV pad 位置不符就旋转 180° |
| **Phase E 重铺后 GND 铜被走线切成孤岛**（DRC 报 `unconnected: 填充区[GND] ↔ 填充区[GND]`，常伴 `starved_thermal`）| **`route` 默认剥铜已从根上堵掉，正常路径下不该再出现**；只有加 `--keep-zones` 才会重现。成因见 `references/routing.md`「为什么先剥铜」。附带纠正一条旧误解：**KiCad DRC 认 zone fill 提供的连通**（实测铺了铜的 GND 网 0 unconnected、没铺铜的照报）——所以 `unconnected` 是**可信信号，不是误报**，任何时候都别当噪音忽略。真出现了：补 stitching via 把碎岛缝到对面完整的地平面 |
| 板尺寸算法粗糙（默认 70×55） | CLAUDE.md 明确写 `45×35mm` 等下限可调小 |
| ISO IC 旋转不能按封装名假设 | **不要**认为"SOIC = pin 在左右两排"——实测 `SOIC-8_L7.5-W5.9-...` 的 8 pad 是上下两排,rot=0 时 HV·LV 两排都横跨竖直屏障。规则:`get_geometry` 读真实 pad x/y,逐个 rot 试 + 复核(loop.md「barrier 旋转推理」)。⚠️ `check_slot_clearance` 那个兜底**只跑在 `pipeline.py` 一键路径上**,工具箱 A→D 回路不经过它 —— 在回路里转错了没有任何东西会纠正 |
| 不处理子电路重复（同一子电路多路复制）| 单板单功能 OK；多模块需手动 |
| 种子(init_layout)网格摆件不会画得"漂亮" | 种子只做区域分区 + 预设邻接;摆到 route-ready 由 agentic A→D 回路负责,**不甩 GUI 补摆**(GUI 只走线) |
| **Edge.Cuts 多闭合环**（Quilter / 部分 fab parser 拒收 "Multiple boards in file"）| **`place_components.py` 写板框时如果 placement_v2 收敛后 board_bbox > `board_min_w/h`，会在主板矩形外侧叠一个 `0.5×N mm` 残骸条形矩形。Phase 2 收尾**必须**校验 Edge.Cuts 只有 **1 个闭合环**——多余环 fail-fast 删掉。**实测血泪 case** placement 后 `.kicad_pcb` 含两个 rect（主板 142×72 + 0.5×66 残骸 @ x=155），上 Quilter 报 "Multiple boards in the file"，schematic / project parse 都通过，唯独 board parse 红。规则：板框只能有 1 个闭合 polygon，shapes 个数 ∈ {4 lines, 1 rect, 1 polyline} 之一，超出立刻 fail。|
| **Quilter 上传想要 placement+routing 而不只是 routing**（**仅当用户点名要用 Quilter 时才走**——它是第三条外部路线，与主推流程冲突：parking grid 故意把非锚点件堆在网格里，等于丢掉 draw-pcb 辛苦摆的布局，跟信条 4「交付 route-ready 布局」相反。默认路线是 Phase E 自动布线或 GUI 手布，别主动提议它）| Quilter 文档 (`docs.quilter.ai/using-quilter/prepare-your-input-board-file`) 明确："**placed components will not be modified by Quilter during layout**"。**正确做法（修正版）**：Quilter 区分锁定与否**用 KiCad footprint 的 `locked` 属性**，**不是位置 (0,0)**。①(0,0) 会让 footprint pad 跑到负坐标 → Quilter parser 报 **"Pre-placed components have off-board pins"** 直接拒收。②正确流程：所有元件位置必须**全部在板内**——anchor（J*/SW*/隔离 IC）保留真坐标 + `fp.SetLocked(True)`；其他件停在板内一个 parking grid（如 `(12,12)` 起 6×5 / 4mm 间距），`fp.SetLocked(False)`。Quilter 只动 unlocked 件。③生成专用副本 `<name>_for_quilter.kicad_pcb`，**不要污染原 placement_v2 文件**；同名复制 `.kicad_sch` / `.kicad_pro` 让 3 文件名一致。④验证：grep `(locked yes)` 数量 == anchor 数量；DRC 会报 parking grid 内 courtyard overlap（预期）；Edge.Cuts 仍是 1 个闭合环。|
| **Silkscreen value 印 MPN 不印电学量**（焊接 / 调试时无法肉眼读懂板）| **`.py` 里 passive 件（R*/C*/L*/D*/LED*）的 `value=` 必须填电学量**（`1M` / `68pF` / `330R` / `10uF` / `LED_GREEN`），不许填 distributor 编码（`TNPW12061M00BEEA` / `C0805C104K5RACTU` / `RC0805FR-07330RL` / `LTST-C171GKT` 这种）。原因：value 字段直接被 circuit-synth 写到 .kicad_sch 的 Value field，再被 sch_to_pcb 同步到 .kicad_pcb 的 footprint Value，最后印到 F.SilkS 上——板子打回来人眼第一秒要的是「这是 1M 还是 100k」，**不是采购编号**。MPN 留给 BOM 工具读 `Properties.MPN` / `Properties.Datasheet` / footprint Field。**IC（U*/复杂 D*）保留 MPN 是允许的**——型号本身就有电学语义（AMC1311BDWVR / SS14 / SMAJ440A 一看就知道是啥）。**Phase 2 收尾自动审计**：扫所有 passive ref 的 value，正则检测 `^[A-Z]{2,}[0-9]+[A-Z]+` 这种 distributor 编码立即报警；建议形如 `R10 value="RC0805FR-07330RL" → 改成 "330R"`。规则：电学量优先 / MPN 进 Properties / 板上看得懂 = 死规定。|

## 设计上不在 draw-pcb 范围内的事

- 元件选型、采购——`component-selecting-JP`
- 原理图 .kicad_sch 渲染——`draw-schematic`
- BOM 验货 / 库可用性——`component-preparing`
- 仿真验证子电路——`check-schematic`（SPICE 子电路仿真）
- 详细 DFM / fab 出片——`release` umbrella（厂商专项判据在它自己的 references 里，别跨 skill 深链）
- 评审已存在 PCB——`check-pcb`
- **深度布线质量评审**（阻抗 / 串扰 / 回流路径）——`check-pcb`；draw-pcb 的 Phase E 只负责布通 + DRC 干净
