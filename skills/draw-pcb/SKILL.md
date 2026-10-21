---
name: draw-pcb
description: >-
  KiCad PCB component-placement expert (Phase 4) — an AI toolbox plus an agentic placement loop,
  not a one-shot pipeline. ALWAYS invoke this skill when placing components into a .kicad_pcb from
  a frozen schematic + project CLAUDE.md, doing HV/LV/ISO region partitioning, isolation-barrier
  placement, GND copper pour, or checking that connectors face outward and can actually be
  plugged in. Do not string-concatenate .kicad_pcb, place before reading placement_brief, default
  rot=0 on isolation/polarized parts, assume a connector hugging the board edge is therefore
  oriented correctly, or treat check_placement's score as an objective to minimize. Use this skill
  first. Optional Phase E auto-routes via the vendored KiCadRoutingTools (KRT). Deep review
  (analyzer / EMC / thermal / cross-ref) belongs to check-pcb.
  Triggers: 画 PCB / PCB 布局 / 摆元件到 PCB / 区域分区 / 隔离屏障 / GND zone / 连接器朝向 /
  连接器插不进 / 自动布局 / 自动布线 / draw pcb / place components / connector mating direction /
  generate pcb layout / run pcb placement / route pcb.
routes-exempt: scripts/vendor/**
---

# Draw PCB — AI 驱动的 KiCad 元件布局

不是一键流水线,是**工具箱 + agentic 回路**:AI 用工具看懂电路、按电路把元件摆开、验证、迭代。
**draw-pcb 独立产出 route-ready 布局**——摆件 + 板框 + GND 铺铜全部摆到位,**不需要在 GUI 里二次补摆**。
布局做好后,**可选 Phase E 用自带的 KiCadRoutingTools(KRT)自动布线**——产出全连通 + DRC 干净的成品板。

> 分工:种子分区 / 板框 / 隔离槽 / `bridge_slot` / zone 几何 / DRC 这些**机械且要稳定**的走脚本;
> **摆哪、转多少、铺哪面、线宽多少**这种电路判断走 agentic 回路(`add_zones` 铺不铺 / 铺哪面
> 属判断,不是无脑收尾)。纯算法布局已验证效果差,回路才是质量来源。

## 核心信条(读懂这四条再动手)

1. **布局是电路判断,不是铺格子。** 好布局来自理解回路——去耦电容贴 IC、电流回路面积小、信号链按序、隔离器件横跨屏障。**先 `placement_brief` 看懂电路,再摆。**
2. **`check_placement` 是合法性闸门,不是目标函数。** 它只答"合不合法"(重叠 / 间距 / 越界 / 非隔离件穿屏障),**绝不**把回路 / EMC / 美观折进一个分数让 AI 最小化——那是用 LLM 重造已删掉的 SA。质量靠 brief + 渲染图判断。
3. **旋转由真实引脚坐标定,不靠封装名猜。** 隔离 / 极性器件**禁止默认 rot=0**——同一个 SOIC-8 引脚可能左右排也可能上下排。先 `get_geometry` 读每个 pad 的真实 x/y,再按 `barrier_devices` 的 pad→net 转到对应域。
4. **交付物是 route-ready 布局,不是"够用就好"。** 退出条件不是 `score=100`,是下面"route-ready 验收"那张清单——draw-pcb 自己把布局摆到能直接布线,GUI 不补摆。

## 不做什么(交棒)

sch 没画好 → `draw-schematic`;深度审图 / EMC / thermal / cross-ref → `check-pcb`;出 Gerber → `release`。
**已摆好的板只审不改**(「这布局有没有问题」)→ `check_chirality` + `check_footprint_sanity`(外来板的库先体检)
→ `placement_brief` + `check_placement`(**必带 --barrier-x + --decoupling-pairs**)
+ `check_connector_access` + `run_drc` + 有铜再加 `check_zones` + `render` 看图,
**禁跑 `init_layout`**——它全量重算,会静默抹掉已有布局且不可逆(板上有 `locked` 件会拒跑)。

## 工具箱(全集;调用形式 + 输出 schema 见 `references/tools.md`)

| 工具 | 干什么 |
|---|---|
| `init_pcb` | sch → 空 `.kicad_pcb` |
| `check_chirality` | 手性闸门:双排蛇形 IC 顺时针=库镜像(群体判据) |
| `check_footprint_sanity` | 库数据体检:钻孔>铜箔 / 零环宽 PTH / 缺 CrtYd |
| `placement_brief` | 抽电路事实:域 / barrier 器件 + pad→net / cap-IC 链接 / chains / net-pad |
| `init_layout` | 确定性区域初始解,当种子 |
| `get_geometry` | 每件 ref/center/courtyard bbox/pad/net |
| `move` | 移动/旋转(target = body-bbox 中心) |
| `check_placement` | 合法性闸门:重叠 / 间距 / 越界 / 穿屏障 → `hard_fail` |
| `check_connector_access` | 可用性闸门:开口朝板外 / 贴板边 / 通道没挡 → `hard_fail` |
| `render` | 标注 PNG(courtyard/重叠/屏障线/飞线) |
| `refit_board` | Edge.Cuts + 槽缩到元件范围;`fill_ratio`。锁框用 `--keep-outline` |
| `bridge_slot` | 隔离槽在跨槽 barrier 器件下留实体桥(元件摆定 + refit 后跑) |
| `add_zones` | GND 铺铜;按 `(net,layer)` 分别调,幂等 |
| `check_zones` | **验铜没跨隔离屏障**——铜跨槽同网 DRC 报不出,唯一防线 |
| `stitch_zones` · `set_silk_spec` | 缝合过孔(铺铜后)/ 丝印字高线宽 |
| `place_silk_refs` | 贪心摆位号:压 pad/丝印/彼此/贴板边(含内部槽)最小化 |
| `set_design_rules` | fab 规则写进 `.kicad_pro`(DRC 只认这) |
| `run_drc` | kicad-cli DRC,几何裁判。`violation_count=0` = 无**未豁免**违例(静默套用 `.kicad_pro` 的 `drc_exclusions`,当闸门用前先核对豁免列表) |
| `route` | 自动布线(vendored KRT)→ `_routed.kicad_pcb` |
| `net_metrics` | 每网长度 / 过孔 / 线宽 / 差分对 delta,**唯一量具** |

参数 + 输出 schema → `references/tools.md`

## 布局回路(A→D,**每步必打印可见输出**)

```
A 理解电路   init_pcb → check_chirality(镜像=停,先修库)
   → check_footprint_sanity(padstack 坏=停,先修库)→ placement_brief
   → 读项目 CLAUDE.md 的 placement 意图
   打印:手性+sanity 回执 / 域划分 / barrier 器件 / 去耦对 / chains —— 不打印不算做过
B 按电路布局  init_layout 出种子 → AI 按 brief 摆:去耦贴 IC、回路收紧、
   隔离器件按引脚定向、chain 按序 → move 落子
   打印:这一轮移了哪些件 + 每个为什么
C 验证迭代   check_placement(闸门,**必带 --barrier-x + --decoupling-pairs**,漏 flag = 闸门关掉)
   + check_connector_access(可用性闸门:朝向 / 贴边 / 插拔通道)
   + render(看图)。隔离器件朝向两个闸门都不管,自己 get_geometry 逐 pad 复核(为什么 → loop.md)
   打印:hard_fail / 违例清单 / 对照 brief 看飞线判断回路紧不紧
   → 修被判断出的具体问题,回到 B(**从第 2 步进,不重跑 init_layout**),重复
D 收尾       set_design_rules(fab 规则→.kicad_pro,一次;DRC 只认这)→ refit_board
   → bridge_slot → add_zones → check_zones → set_silk_spec(有丝印规格时)
   → place_silk_refs(跑了字号就跟着摆位号)→ run_drc → render 终图
   add_zones 不是无脑收尾:铺哪面 / 哪个 GND 网 / 铺不铺,按 references/copper_pour.md
   逐面逐区判断(安规 veto 优先);多 GND 网用 --nets 按域分开调用
   打印:板框尺寸 / fill_ratio / 铺了哪些 (net,layer) + 为什么 / DRC 违例数 / 终图 /
   route-ready 验收逐条结果
```

## Phase E — 自动布线(可选,过 route-ready 验收后)

```
route → add_zones(重铺,复现 D 的每个 net·layer)→ stitch_zones(双层板缝通两面地)
   → run_drc → check_zones → net_metrics(声明过等长/过孔/宽度约束时必量)
   route 不裸跑:先按 references/routing_strategy.md 给 net 分类定配方
   打印:net 分类 + 配方参数 + 为什么;布完打印:布通网数 / vias / 重铺 zone /
   DRC 违例 + unconnected —— 不打印不算布过
```

> **顺序不可换**:`route` 会自动**剥掉副本上的铺铜**再布(留着 KRT 会判该网"已连"整网跳过,
> 重铺后碎成孤岛);布完必须 `add_zones` 重铺,**再**跑 `run_drc`——没重铺时板上没铜,那次 DRC 不作数。
> 此处 `unconnected` 必须为 0(Phase D 那条"unconnected 是预期,忽略"只对未布线的板成立)。
> 退出条件 5 条 + 换配方最多 3 轮 / 铁律 / 配方 flag → `references/routing.md`。**别裸跑吃默认。**

## route-ready 验收(回路退出条件)

退出 = 9 项逐条全过(hard_fail / DRC / **check_zones 铜没跨屏障** / 隔离旋转 / 贴边 / 去耦 ≤2mm /
courtyard 复核 / fill_ratio / 终图三域+链序)——**清单全文 + 各项阈值 → `references/loop.md`**
(SKILL.md 这行只是点名,阈值一个都不在这里),打印逐条结果。
任一条不过 → 回 B 修;硬上限 6 轮 + keep-best,到顶报告剩余项交用户,**不把布局补摆甩给 GUI**。

## 前置依赖

KiCad 10 + `kicad-cli` + bundled `pcbnew` Python + 工作区 `.venv`;`draw-schematic` ERC pass。
`.bom_readiness.json` gate 上游 `draw-schematic` 已挡,不复查。
Phase E 需 KRT Rust 模块已编译(`build_router.py`,需 `cargo`;**缺 cargo 报用户,别自己装**)。
下游:`check-pcb`(深度检查)→ `release`。

## 红线

- ❌ 字符串拼接 `.kicad_pcb` / `.kicad_mod`——一律走工具(`_kicad_python_helper.py` 的 mode)
- ❌ 不跑 `placement_brief` 就摆——等于盲摆,回路 / 隔离 / 引脚全靠猜
- ❌ 连接器 / 开关摆板内——必须贴板边(`placement_brief` 的 `edge_devices`),否则线缆够不着
- ❌ `score=100` 就报完成——合法 ≠ 好,**必须 Read 渲染图**对照 brief 看回路;
  但**图只发现问题、不宣布通过**,判过要量对账,视角要看得见待验物
- ❌ 回路外单独 move / rotate 完直接报完成——重跑清单见 `loop.md`
- ❌ Edge.Cuts 多闭合环 / passive silk 印 MPN / pad 旋转几何 → `known_issues.md`

## references(何时读 → 读哪份)

- 跑回路前**必读** → `references/loop.md`(A→D 详细 / 验收全文 + **全部阈值** / 旋转推理 / keep-best)
- 调工具前 → `references/tools.md`(flag + 输出 schema)
- 跑 Phase E 前 → `references/routing.md`(铁律 / 退出条件) + `references/routing_strategy.md`(逐网配方)
- 跑 `add_zones` 前 → `references/copper_pour.md`(五轴否决 / 多 GND 网 / 重铺)
- 写改项目 placement 块 → `references/placement_rules.md`(**字段唯一权威源**)
  + `references/claude_md_constraints.md`(位置约束)
- 闸门干净但 DRC 挂 / 板框 / silk / 铜孤岛 → `references/known_issues.md`
- 改脚本 → `references/helper_modes.md` + `references/pipeline_phases.md`(**非主推**一键路径)
