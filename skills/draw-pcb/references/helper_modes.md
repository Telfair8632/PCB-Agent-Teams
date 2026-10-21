# `_kicad_python_helper.py` 模式参考

实现文件:`scripts/_kicad_python_helper.py`(所有 mode 的宿主,跑在 KiCad bundled python)。
一键路径入口:`scripts/pipeline.py`(**非主推流程**,细节见 `references/pipeline_phases.md`)。

> 在 KiCad bundled Python 里跑（pcbnew API），通过 subprocess + JSON spec 文件被主入口调。
> 不允许字符串拼接 .kicad_pcb 文件。

| mode | 输入 | 动作 |
|---|---|---|
| `create_pcb` | netlist JSON + lib 路径 | `pcbnew.NewBoard` + `FootprintLoad` + `pad.SetNet` + `SaveBoard`；写入 board edge clearance / min clearance / Default netclass clearance design rule |
| `apply_layout` | placements dict + board cfg | 设位置 + 旋转 + Edge.Cuts 板框 + 隔离槽（接受 v2 floorplan 的 `slots[]` 列表 + 单 `slot_x/slot_w` 兜底）。**用 body bbox center 对齐**（处理 anchor 不在 body 中心的 footprint，如 `Package_DIP:DIP-4_W7.62mm` anchor 在 pad 1）；可选末尾跑 pad-pad 冲突扫描（layout v2 默认 `skip_pad_resolve=True`）；隐藏 silk ref text 和 footprint outline（PCB_SHAPE 没 IsVisible，改 layer 到 `Cmts.User`）。**placement 计算本身不在这里**——由 `placement_v2/orchestrator.py`（在工作区 .venv 里跑）算好后传 `placements{}` 进来 |
| `resolve_pad_conflicts` | .kicad_pcb + min_clearance_mm | 独立 pad-pad 兜底扫描——pipeline 在 Phase 2.7 后跑这个，处理 slot-clearance 移动后引入的新冲突。只动 passive 件（R/C/D/L），ICs / connectors 锁定 |
| `classify_fps` | .kicad_pcb | 只读，返回 `{ref: {zone, is_connector, connector_type}}` |
| `check_slot_clearance` | .kicad_pcb (auto_fix=True) | (1) 自动给跨槽 ISO IC 选朝向——试 0/90/180/270° 四个绝对角（绕 body center 旋转再回中，不动板框/隔离槽），选**让 HV/LV pad 到屏障的最小距离最大化**且两组都在正确侧的那个角；都在正确侧时按角度差小优先（确定性）。返回 `iso_rotations[]`，每项带 `barrier_margin_mm`。(2) 自动把 pad 落进 slot 的 footprint 推到正确侧 |
| `ensure_pads_in_board` | .kicad_pcb + margin_mm | 任何 pad/body 落到 Edge.Cuts 外的元件滑回板内 |
| `add_ground_zones` | .kicad_pcb + keywords + layers + `pad_connect` | 给每个 GND-like net 加 zone，**先按 (net,layer) 去重清掉已有 zone**，HV_GND clip 到 slot 左半多边形、LV_GND clip 到 slot 右半多边形、其它 GND 全板矩形；不同 net 不同 priority；支持 `fill_now=True` 跑 ZONE_FILLER。`pad_connect` = `thermal`(默认花焊盘) / `solid`(实心连接,写出 `(connect_pads yes)`) / `none`。绕过 kct zones batch 的 net 解析 bug |
| `validate_zones` | .kicad_pcb (只读) | 校验**没有铜 zone 跨隔离屏障**：检测单条竖直隔离槽 `[left,right]`，对每个已填充 zone 取**填充铜**的 X 跨度，若同一 zone 既有铜在 `x<left` 又有铜在 `x>right` → 跨屏障 → `verdict="fail"` + `crossings[]`。net/电压/网名无关。无槽时 `slot_detected=False` 跳过（不误判）。横向/多段屏障未覆盖 |

| `move_footprints` | .kicad_pcb + `{ref: [x,y,rot]}` | 按 body-bbox center 移动 / 旋转指定 footprint,不碰 Edge.Cuts / 走线 / zone。`move` 工具的底层 |
| `bridge_slots` | .kicad_pcb + barrier 器件 | 重画隔离槽,在每个跨槽 barrier 器件下方留实体桥。`bridge_slot` 工具的底层 |
| `refit_board` | .kicad_pcb + margin + `keep_outline` | Edge.Cuts 外框 + 隔离槽缩到 footprint 实际范围 + margin,返回 `fill_ratio`。`keep_outline: true` → 完全不写文件,只按现有板框回报 `fill_ratio`。`refit_board` 工具的底层 |
| `stitch_zones` | .kicad_pcb + net + 两个层 + pitch | 在**两面填充铜各自内缩(过孔半径+间距)后的交集**内按栅格打过孔,把该网两面缝通;孔另立「孔到孔间距」判据(只判铜会撞进 THT 焊盘的钻孔)。可选往本网大贴片焊盘内打散热孔。**一次一个网**——自动匹配所有 GND 会缝穿隔离屏障。`stitch_zones` 工具的底层 |
| `set_silk_spec` | .kicad_pcb + 字高 / 线宽 | 全板丝印文字统一到 fab 规格,位号放到所在面丝印层,Value 默认不动。**副作用是预期的**:动过的件与库不一致 → `lib_footprint_issues`;字变大 → `silk_overlap` 涨。`set_silk_spec` 工具的底层 |
| `place_silk_refs` | .kicad_pcb + passes / gaps / edge_margin | 贪心摆全部可见位号:每件在本体四周候选环(8 向 × 多档间隙 × 横/竖排)里选加权重叠代价最小的位置。代价从高到低:出板/贴 Edge.Cuts(含**内部隔离槽**,每段 Edge.Cuts 图元外扩 `edge_margin_mm` 当禁区;闭合轮廓只取四条周边带,否则整板全禁) > 压 pad > 压别的位号 > 压别家丝印 > 压本体 > 离本体距离。多 pass 重排(先摆的看不见后摆的)。`place_silk_refs` 工具的底层 |

> 上表 14 个就是 `MODES` 全集(与 `_kicad_python_helper.py` 末尾的 dispatch 表一一对应)。
> 加 mode 必须同步这张表,否则「改脚本 → 读 helper_modes.md」这条路由会漏掉新能力。

## Spec 字段约定

- `pcb_path` (input)、`output_pcb` (default = pcb_path)
- mode-specific 字段见 helper 源码 docstring

## 设计规则注入（create_pcb）

`mode_create_pcb` 在 NewBoard 之后通过 `board.GetDesignSettings()` 写入：

| 字段 | 默认值 |
|---|---|
| `m_CopperEdgeClearance` | 0.5 mm |
| `m_MinClearance` | 0.2 mm |
| Default netclass clearance | 0.2 mm |

同时 patch `.kicad_pro` 的 `design_settings.rules`（KiCad CLI 的 DRC 从 .kicad_pro 读，不从 .kicad_pcb 的 setup 块读）。

## 已知 bug + workaround 清单

| 现象 | 根因 | 修复 |
|---|---|---|
| 老版 sch_to_pcb 生成的 PCB 无效 | 字符串拼接 .kicad_mod，pad/net 断连 | 改用 pcbnew API |
| 板框删除逻辑空操作 | 行解析后 append 全部行，filter 没生效 | pcbnew API 直接 `board.Remove(drawing)` |
| `cfg.creepage` 没生效 | placement 算法没用该参数 | 用 `fp_extent + SPACING` 兜底，足够保守 |
| 旋转元件间距错（用 height 计算 vertical span 但 rot 90 后 width 才是 vertical）| | 全部用 `max(w, h)` 兜底，无视 rotation |
| connector 跟 R 链撞 | J1 起点 y_top + 1，R 链也 y_top + 4，距离不足 | R 链起点改为 J1.bottom + 3 |
| GND zone 累积重复（zones_intersect） | `add_ground_zones` 不清理已有 zones，重跑 pipeline 累积 | helper 先按 (net,layer) 去重清掉再 Add |
| HV_GND × LV_GND zone 重叠 | 两个 net 都用全板矩形 outline 且同 priority | helper 按 net 类型 clip outline + 不同 priority |
| 去耦电容跟 ISO IC 输出 pad 短路 | 老 placement 把 LV_GND 电容贴 U1 输出脚 | apply_layout 末尾的 pad-pad 扫描兜底（layout v2 提前 snap，正常路径下默认 skip）|
| 走线靠板边 (copper_edge_clearance) | board edge clearance 没写进 design rule | create_pcb 注入 0.5mm 规则 |
| ~~KiCad ratsnest 不识别 zone fill 的连接（unconnected 误报）~~ **已证伪，别再当误报忽略** | 实测：铺了铜的 GND 网 0 unconnected，没铺铜的 GND 网照报 → DRC **认** zone fill 的连通 | `unconnected` 是可信信号。Phase E 重铺后它指向真孤岛,处理见 `references/known_issues.md` |
| `BOARD_DESIGN_SETTINGS.SetCopperEdgeClearance` 不存在 (KiCad 10) | KiCad 10 把 setter 删了 | 直接赋 struct 成员 `ds.m_CopperEdgeClearance = pcbnew.FromMM(0.5)` |
| `PCB_SHAPE.IsVisible / SetVisible` 不存在 (KiCad 10) | graphics 没有 visibility 属性 | 改 layer 到 `Cmts.User`（DRC + fab silk export 自动忽略），不要 Remove |
| 多 ISO IC（≥3）固定 step 把后面的 IC clamp 重叠 | iso_y 越界后被 clamp 到 y_bot，多个 IC 叠在一起 | 动态分配：`iso_step = max(ISO_SLOT_HEIGHT, iso_band/n)` |
| 大 cap 列（如 4× decap）超过 ISO step 后撞下一个 IC courtyard | LV-side cap loop 不限数量 | 限 `MAX_CAPS_PER_ISO_SIDE=2`；多余 fall back 到 LV grid |
| Phase 2.7 把元件推开后可能造成新 pad-pad 冲突 | apply_layout 末尾 sweep 跑在 phase 2.7 之前（且 layout 默认 skip） | 单独 Phase 2.75（mode_resolve_pad_conflicts）在 2.7 之后再跑一遍 |
| Pad sweep 把 SOIC IC（U1）当 mover 推飞 37mm | sweep 选 mover 用 area 比较，cap < SOIC，但当 SOIC 也比 connector/SIP-4 小时被推 | sweep 加白名单：只有 R/C/D/L-prefix 才能 mover；U/J/X/Y 锁定 |
| Phase 2.7 把 SIP-4 (DIP-4_W7.62) push 后 body 偏移 ~8mm | _check_slot_clearance.fix() 用 anchor.y 当 target，但 SIP-4 anchor 在 pad 1（顶部）不在 body center | fix() 改用 bbox.GetCenter().y 当 preserved Y，target = (push_x, body_center_y) |
| Sweep 把 cap 推出板外引发 copper_edge_clearance | mover slide 算法只看冲突方向不查 board 边界 | sweep 内每次 move 后 clamp_inside（board edge - inset），final pass 对所有 passives 再 clamp 一次 |
| LV-side cap 列穿过下一个 ISO IC courtyard | per-IC cap loop 不看后面 IC 占位 | cap_clears_next_iso 检查：cy 落在任何 later ISO IC bbox 内则跳过此 cap，让它 fall back 到 LV grid |
| `PCB_VIA::GetWidth()` 在 KiCad 10 必须带 layer 参数，不带就 abort 进程（rc=139 SIGSEGV） | C++ API 改了，没 layer 就 wxASSERT | helper 改用 `via.GetWidth(f_cu)`，try/except fall back 到 `GetDrill()/2 + 0.15` |
| `board.Remove()` 后再 iterate `board.GetFootprints()` 返回 SwigPyObject（崩 `AttributeError: ... no attribute 'Pads'`）| macOS pcbnew 10 SWIG 绑定不稳 | retract 类 mode 必须先一次性快照所有 pad + track 元数据再 Remove，绝不迭代 → Remove → 再迭代 |
| Helper subprocess stdout 被 wx debug 吞掉，pipeline parser 读不到 JSON | macOS wxApp 在某些 LoadBoard/SaveBoard 路径里抢 stdout | helper main() 同时 print 到 stdout + stderr；pipeline `_call_helper_mode` 在两个 stream 里都搜 `{`-prefix line |
| **反方向的同一个坑**：pcbnew 把 `swig/python detected a memory leak of type 'PCB_TRACK *'` 这类告警**打到 stdout**，混在 JSON 后面 | SWIG 绑定在 `board.Remove()` 之后吐 leak 报告，走的是 stdout 不是 stderr | 别用 `lines[-1]` 取 JSON。`placement_v2/orchestrator.py` 取**第一个能解析成 JSON 的行**；`tools/_kicad.py` 取最后一个 `{`-开头行（helper 双写 stdout+stderr 时成立）。新写 mode 照抄其中一种，别自己发明 |
| `zone.GetFilledPolysList(layer).Clone()` 出来的对象一调 `Deflate` 就 `AttributeError: 'SHAPE' object has no attribute 'Deflate'` | `.Clone()` 返回基类 `SHAPE`，把 `SHAPE_POLY_SET` 的 `Deflate` / `BooleanAdd` / `Contains` 全丢了 | 用**拷贝构造**：`pcbnew.SHAPE_POLY_SET(zone.GetFilledPolysList(layer))`（`mode_stitch_zones` 里就是这么写的） |
| 删元素的 mode「看起来跑过其实没跑」，而且 stderr 被 leak 告警刷屏淹没 | 迭代中 `Remove` 让后续 SWIG handle 失效，异常在噪音里看不见 | **凡是要删元素的地方**：开头 `items = [x for x in board.GetTracks()]` 取快照 → 全部判断做完 → 最后统一 `Remove`。判完再删，绝不边迭代边删 |
