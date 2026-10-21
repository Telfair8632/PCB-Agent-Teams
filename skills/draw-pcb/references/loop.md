# 布局回路详细 — A→D + 判断标准

**打印格式**:每条回执行首加 `[draw-pcb]` 前缀(如 `[draw-pcb] Phase C round 2: hard_fail=false ...`)。
SKILL.md 说"不打印不算做过",没有统一前缀这条自我要求就没法回头核验。

**目录**:总图 · Phase A 理解电路 · Phase B 按电路布局 · Phase C 验证迭代 · Phase D 收尾 ·
**route-ready 验收(10 项 + 全部阈值)** · 停止条件 + keep-best · 完成报告。
验收清单和阈值在文件后半段——SKILL.md 只点名不给数,**必须读到底**。

SKILL.md 的回路速览的展开。读这份再跑回路。

> 分工:机械且要稳定的(分区 / 板框 / 槽 / zone 几何 / DRC)走脚本;
> **摆哪、转多少、铺哪面、线宽多少**这种电路判断走回路——回路才是质量来源。

## 总图

```
A 理解电路 ──→ B 按电路布局 ──→ C 验证迭代 ──┐
                    ↑                          │
                    └──────── 修问题 ←─────────┘
                                              │ route-ready 验收逐条全过
                                              ▼
                                          D 收尾
```

布局的对象是 `Projects/<name>/kicad/<name>/<name>.kicad_pcb`。所有工具在 `scripts/tools/`,
用工作区 `.venv` 的 python 跑。

## Phase A — 理解电路

1. `init_pcb <project>/kicad/<name>` — sch → 空 `.kicad_pcb`(全部元件落在原点)。已存在则跳过。
1b. `check_chirality <pcb>` — **封装手性闸门,拿到板先跑**(尤其封装来自外部 EDA 转换 /
   自绘时)。镜像封装 DRC / 布通率 / 网长全绿但贴片装不上,越晚发现返工越大。
   `MIRRORED_LIBRARY` → 停,修来源库重转,**不摆一个件**;`VERIFY_LISTED` → 逐件对照
   datasheet 定性,理由打印出来再继续。打印一行回执:
   `[draw-pcb] A check_chirality verdict=? cw_serpentine=?`
1c. `check_footprint_sanity <pcb>` — **库数据体检,与 1b 并列跑**。
   `PADSTACK_BROKEN`(钻孔>铜箔 / 零环宽 PTH)→ 停,修库再摆,别带病布局;
   `NO_COURTYARD` → 可以继续,但记住重叠防线降级:闸门走图元 bbox 回退,
   **KiCad DRC 的 courtyard 检查也一并失效**(见 known_issues.md),终板前必须渲染目检。
   打印一行回执:`[draw-pcb] A check_footprint_sanity verdict=? no_courtyard=?`
2. `placement_brief <pcb>` — 拿电路事实 JSON。
3. Read 项目 `CLAUDE.md` 的 `placement` 段(人工声明的意图:板宽 / 隔离槽 / anchors /
   decoupling_pairs / chains / **connector_access**)。字段表 → `claude_md_constraints.md`。
   `connector_access` 有声明就**当场落成 `decl.json`**(`{ref: {mating_dir, note}}`),
   Phase C 的 `check_connector_access --declare` 要用;不落下来就等于每轮重查 datasheet。
4. **合并成布局意图**,并**打印**出来:
   - 域划分:每件属 HV / ISO / LV
   - barrier 器件:哪些件横跨隔离屏障 + 各自 pad→net(屏障 x 在 Phase B `init_layout` 画槽后才定)
   - 去耦对 / cap-IC 链接:哪个 cap 该贴哪个 IC
   - chains:按序的串联链(分压链等)
   - `geometry_uncertain` 件(若有):courtyard 退化的件,extent 不可靠,后面 DRC 重点看

**必须额外打印一行:项目 CLAUDE.md 的 `placement` 块 found / not found + 解析出几条 `isolation_slots`
+ 几件 `connector_access` 声明。**
解析条件很严(标题正则 + **紧随其后**的 yaml 围栏 + 精确键名),不匹配就静默返回 `{}` 走默认——
板上一条隔离槽都不会画,而 Phase A 其余四类信息看起来全都正常。声明了槽却解析出 0 条 = 配置被吞了,
去查 `references/claude_md_constraints.md` 的三个易错点,别继续往下摆。

`placement_brief` 给的是**机械事实**。哪条回路 EMC 敏感、间距具体多少 mm,是你在事实上做的**判断**——
项目 CLAUDE.md 的声明优先于 brief 的推断。

## Phase B — 按电路布局

1. `init_layout <pcb> --claude-md <project>/CLAUDE.md` — 出确定性区域种子(别从原点零开始)。
   ⚠️ **只在空板/首轮跑**。它是全量重算(partition→floorplan→layout),不读现状,
   **会静默抹掉板上已有的全部人工布局且不可逆**。"回 B 修"是从第 2 步进,不重跑 init_layout;
   板上已有摆位(改布局 / 只审不改 / 局部微调)一律跳过这步。
2. `get_geometry <pcb>` — 读当前每件 center / courtyard / pad。
3. **按电路判断摆**,不是铺格子。优先级:

   **① 隔离屏障 + barrier 器件定向(最高)**
   - 屏障是竖线,x = `init_layout` 输出 JSON 的 `slots[].x_mm`(画槽时算的,对应项目
     CLAUDE.md 的 `isolation_slots` 中心)。后续 `check_placement` / `render` 的 `--barrier-x`
     都用这个值。(`placement_brief` 的 `barrier_x` 是摆好后的复核值,空板上为 null。)
   - barrier 器件(如隔离放大器、隔离 DC-DC)必须**横跨**屏障。旋转推理见下节。
   - 非 barrier 件**不许**穿屏障——HV 件全在屏障左,LV 件全在右。

   **② 连接器 / 开关贴板边 + 开口朝外**
   - `placement_brief` 的 `edge_devices[]` 列出全部接口件(按**封装库名**认,不靠位号前缀——
     CN* / DC* / TF* 这类非 KiCad 习惯的位号一样能认出来)。必须摆到板**周边**——
     courtyard 离 Edge.Cuts ≤3mm。线缆 / 螺丝端子从板边引线,开关要能拨。
   - **贴边不等于能插**。同一个连接器贴在同一条边上,转 180° 就变成开口朝板内——
     DRC 干净、courtyard 不重叠、"贴边"也过,板子做出来插不进去。
     每件的 `mating_side_local` + `angle_for_edge` 直接给出「要朝哪条边就转多少度」,
     **落子时按这张表定 rot,别默认 0 也别沿用导入值**(从别的 EDA 导入的旋转可能整体差 180°)。
   - `mating_unknown_reason` 非空的件 = 封装没有本体轮廓,几何判不出朝向。
     **查 datasheet 后写进 `--declare` JSON**,别猜(猜错的代价是整批板报废)。
   - 连接器摆板内 = 线缆横跨整块板。项目 CLAUDE.md 常指定哪个连接器靠哪条边
     (HV 端子左、daisy-chain 上等),按它来;没指定就按域就近的板边。
   - 指示 LED 尽量也靠边,便于观察。

   **③ 回路收紧 / 去耦贴 IC**
   - 去耦 / 旁路电容:**最近 pad 到 IC pad ≤2mm**。配对按项目 CLAUDE.md 的
     `decoupling_pairs` 声明,**不是**按 net 猜(net 推断会配错 IC)。
   - 但 pad-to-pad **别 < 0.5mm**——那是阻焊 web 下限,太近 KiCad DRC 会报
     `solder_mask_bridge` / `shorting_items`。贴紧 ≈ 0.5–1.5mm,不是越近越好。
   - 电流回路(iso DC-DC 输入回路、整流回路等)的成员聚成一簇,回路面积小 = EMI 小。
   - 高 di/dt 的别拉长。

   **④ chain 按序**
   - chain 成员沿一条直线、按 `members` 顺序排,相邻 courtyard 间距 ~1–2mm。

   **⑤ 区域内其余件**:同域的尽量靠近其连接的件(看 `net_pads`),别乱撒。

4. `move <pcb> --moves-json <file>` 落子。一次可移多件。
5. **打印**:这一轮移了哪些件,每个一句话为什么(如"C6 贴 U1 东侧——iso 供电去耦")。

### barrier 器件旋转推理(信条 3 的展开)

`placement_brief` 的 `barrier_devices[]` 给每个 pad 的 net。步骤:

1. 把 pad 按 net 的域分两组:接 HV 侧网(HV_GND / 高压 sense)的 = HV 组;接 LV 侧网(LV_GND / 低压供电 / 输出)的 = LV 组。
2. 屏障是竖线。目标:HV 组 pad 落在屏障**左**(x 小),LV 组 pad 落在屏障**右**。
3. **`get_geometry` 读该件每个 pad 的真实 x/y——别凭封装名假设引脚布局。** 实测同一个 SOIC-8 footprint 引脚可能是上下两排(不是常识里的左右两排);凭"SOIC = 左右排"假设 rot=0 会让 HV·LV 两排都横跨竖直屏障。对 ∈ {0,90,180,270} 逐个 `move` 试,每次 `get_geometry` 复核,选能让 HV 组平均 x < LV 组平均 x 的那个。
4. `move` 时带上这个 rot,落子后再 `get_geometry` 确认 HV 组 pad 全在屏障左、LV 组全在右。

**禁止默认 rot=0,也禁止凭封装名推 rot。** 引脚物理位置只有 `get_geometry` 说了算。
⚠️ **别指望兜底**:`check_slot_clearance` / `resolve_pad_conflicts` / `ensure_pads_in_board`
这三个"确定性兜底"**只在 `pipeline.py` 一键路径上被调用**;工具箱 A→D 回路(`init_layout` →
`apply_layout`)**根本不经过它们**。在回路里旋转错了没有任何东西会自动纠正 —— 自己推理 + `get_geometry` 复核。

## Phase C — 验证迭代

1. `check_placement <pcb> --barrier-x <X> --decoupling-pairs <CLAUDE.md 声明的 cap:IC>` —
   合法性闸门。看 `hard_fail` + `violations[]` + `warnings[]`。`--decoupling-pairs`
   传 Phase A 从项目 CLAUDE.md 提的 `decoupling_pairs`(如 `C6:U1,C10:U1,C5:U3`);
   不传则 `cap_far_from_ic` 退回 net 推断配对,可能配错 IC。
   ⚠️ **两个 flag 都是可选参数,漏传不会报错,闸门直接降级**:`--barrier-x` 不传时
   `check_placement.py` 的穿屏障检测**整段不执行**(`if barrier_x is not None:`),
   仍然返回 `hard_fail=false / score=100`。绿灯 ≠ 检查过。
   ⚠️ **barrier 器件被闸门无条件豁免**——它只判"是不是隔离器件"就 `continue`,
   **不判 HV pad 是否真在槽左、LV pad 是否真在槽右**,连"隔离件整个躺在单侧根本没跨槽"
   也不报。route-ready 第 4 项(隔离旋转)因此 100% 靠你自己:
   `get_geometry` 读该件每个 pad 的真实 x,逐个跟 barrier x 比,没有脚本兜底。
1b. `check_connector_access <pcb> [--declare <decl.json>]` — 物理可用性闸门,与 1 并列必跑。
   `hard_fail=true` 要修:`faces_inward` 按 `fix_hint` 转 180°(`move` 带 rot),
   `not_at_edge` / `blocked` 挪位。**`undetermined` 同样算不过**——封装判不出朝向,
   查 datasheet 后写 decl.json 再跑;留着不管 = 交一块可能插不进的板。
   decl.json 来源:Phase A 从项目 CLAUDE.md `placement.connector_access` 落下来的那份。
2. `render <pcb> --barrier-x <X> --ratsnest -o <png>` — 标注图。**必须 Read 这张 PNG**。
3. **判断**(打印结论):
   - 闸门:`hard_fail` 真 → 有重叠 / 越界 / 间距 / 非隔离件穿屏障,必修。
   - `warnings`:`connector_not_on_edge` → 连接器没贴边,必修;`geometry_uncertain` → 该件 extent 不可靠,Phase D DRC 重点看。
   - 回路:对照 brief 看飞线(灰线)——去耦对的飞线该极短;某域的件飞线大量射到别的域 = 没按回路摆。
   - 视觉:看 PNG——挤成团 / 大片空 / 大件压小件 / 连接器是否都贴板边。
   - HPWL(`metrics.hpwl_mm`):跨轮记录,**下降 = 在收紧**;但 HPWL 是参考不是目标,别为压它牺牲③④。
4. 把判断出的**具体问题**变成下一批 move,回 Phase B。

**不要**把 `check_placement` 的 score 当成要最小化的数。score=100 只代表合法。回路紧不紧、好不好看,是你看 brief + PNG 得出的判断。

## 改完要重跑什么(回路外单独 move / rotate 之后)

单独动了元件就**不能直接报完成**,按动了什么补跑:

| 动了什么 | 必须重跑 |
|---|---|
| 任何元件位置 / 角度 | `check_placement`(带全 flag)+ `render` |
| 连接器 / 开关 | 上面两项 **+ `check_connector_access`** |
| 板上**已有铜 / 桥**时动了任何件 | 上面全部 **+ `refit_board → bridge_slot → add_zones → check_zones → run_drc`** ——不重跑桥会停在旧位置、铜绕的是旧 courtyard |

## 什么算"验过了"(贯穿全回路的铁律)

**渲染图只能用来发现问题,不能用来宣布通过。** 凡是"贴上去看着没问题 / 图上看布通了 /
参数我传给布线器了"这类判断,都必须换成**可测量的量对账**——坐标 vs 坐标、数值 vs 阈值:

| 想断言的事 | 量它的工具 |
|---|---|
| 元件在该在的位置 / 没撞 | `get_geometry` + `check_placement`(硬闸门) |
| 几何合法 | `run_drc`(最终裁判,用真 footprint 几何) |
| 网长 / 过孔数 / 线宽 / 差分对等长 | `net_metrics`(**唯一量具**) |
| 铜没跨隔离屏障 | `check_zones` 退出码 |
| 连接器插得进 | `check_connector_access` 的 `hard_fail` |

**还要选看得见待验对象的视角**:引脚在板底,只渲顶视等于挑了一个刚好看不到待验物的角度,
再把"看着没问题"当通过。要验底面的东西就出底视图。

**别把断言写进注释当证据**:"pad pitch 对得上"这种话如果没量过,它就是猜测冒充结论。

## Phase D — 收尾

> **先看这块板有没有隔离屏障,再决定跑哪几步。** 下面是**有屏障板**的全序列。
> **单一 GND、无隔离屏障的板**(绝大多数纯 LV 板)只跑:
> `set_design_rules → refit_board → add_zones → run_drc`。第 2 步 `bridge_slot` 与第 3b 步 `check_zones`
> **按定义不适用**——没有槽就没有桥可留,`check_zones` 也没有屏障可验(它会直接 `skipped`)。
> 那不是漏了步骤。**板框由外部给定**(外壳 / 机箱 / 客户规格)时第 1 步换成
> `refit_board --keep-outline`(只量 `fill_ratio`,一个字节都不写)。
> **布完线**(Phase E)还要补一步 `stitch_zones` 把两面地缝起来(调用形式见 `tools.md`,
> 在 Phase E 链上重铺之后跑)。

0. `set_design_rules <pcb> ...` — fab 规则写进 `.kicad_pro`,**一次就够**(kicad-cli DRC
   只认这里;`init_pcb` 建的板有保守基线,fab 规格与基线不同、或板来自外部,必须先跑)。
1. `refit_board <pcb>` — 把 Edge.Cuts + 隔离槽缩到元件实际范围 + margin。板框尺寸是
   `init_layout` 按 CLAUDE.md `pack_density` 一次性定的;回路把元件收紧后板框就偏大,
   refit 让它贴合。输出 `board` 尺寸 + `fill_ratio`(紧凑度)。**必须在 bridge_slot /
   add_zones 之前**——这两步都从 Edge.Cuts 读尺寸。`fill_ratio` 偏低 / 终图有大空洞 →
   回 B 把松散的区收紧,再 refit。
   ⚠️ **margin 会把板边推到连接器外面**:默认 2.5mm > `check_connector_access` 的
   `--max-gap` 2.0mm,连接器多半是那一侧最外沿的件,refit 完必然变 `not_at_edge`;
   再把连接器往外挪又触发下一次 refit → 震荡。**有接口件的板用 `--margin 1.5`**
   (小于 max-gap),refit 后**立刻重跑 `check_connector_access`** 复核,别等到验收才发现。
2. `bridge_slot <pcb>` — 隔离槽在每个跨槽 barrier 器件下方留实体桥。THT 隔离 DC-DC 的 pad pitch < 槽宽,不桥接槽会切穿 pad → DRC `copper_edge_clearance`;器件自身即屏障,桥接是正解。**必须在元件摆定 + refit 后跑**(桥按 barrier 器件最终位置画)。
3. `add_zones <pcb>` — GND 铺铜。铺哪面 / 哪个网按 `references/copper_pour.md` 逐面逐区判断。
   **记下这一轮用过的每个 `(net, layer)` 组合并打印**——Phase E 重铺时必须逐条复现。
   漏一面**不会报错**:那面没铜,DRC 无铜可查,`unconnected` 也不涨。只能靠这份清单对账。
3b. `check_zones <pcb>` — **验铜没跨隔离屏障**(有 crossings 即退出码 1)。铜跨槽是同网,
   **DRC 不会报 clearance**,这是唯一的专门防线;`add_zones` 铺完必跑,不跑等于没验过安规架构。
3c. `set_silk_spec <pcb>`(可选,项目 / fab 有丝印规格才跑)— 字高线宽统一到规格,
   **放在 run_drc 之前**让 DRC 报告含最终丝印状态;字变大会推高 `silk_overlap` warning,
   是预期副作用,不是回归。
3d. `place_silk_refs <pcb>`(跑了 3c 就跟着跑)— 贪心摆全部位号,把 3c 推高的
   `silk_overlap` / `silk_over_copper` 压回去(还避开板边与内部隔离槽)。验收看
   run_drc 前后这几类计数差,不看"顺不顺眼"。
4. `run_drc <pcb>` — kicad-cli DRC。**跑之前确认规则来源**:fab 规格与 `init_pcb` 写的基线
   不同时,必须先跑 `set_design_rules`(kicad-cli 的规则来自 `.kicad_pro`,没写进去 = 拿错
   标尺量板,报告每个数都不对;外部板 / 审图形状尤其要查)。
   **`run_drc` 是几何最终裁判——但 courtyard 检查除外**:
   KiCad 的 courtyard DRC 同样依赖库里的 CrtYd 层,**整库缺 CrtYd 时 DRC 对器件重叠同样是瞎的**
   (实测两颗器件实体互插 2mm+,DRC 0 error)。此时唯一防线是 `check_placement` 的图元 bbox
   回退(`extent_source` 里 `graphics`/`pad_bbox` 的件就是走回退的);`pad_bbox > 0` 的板要
   加渲染目检。其余违例类(clearance / hole / annular)DRC 用真几何,仍是最终裁判。
   - `unconnected_items` 大量 = 预期(还没走线),忽略。**这条只在 Phase D(未布线)成立**——
     Phase E 布线 + 重铺之后 `unconnected` 是**真信号**(铜被走线切成孤岛),必须查,见 `SKILL.md` Phase E。
   - `courtyard` / `clearance` / `hole` 类违例 = **真问题**,不是预期——回 Phase B 修。
5. `render <pcb> -o <final>.png` — 终图。
6. **打印**:板框尺寸 + fill_ratio + DRC 各类违例数 + 终图路径 + 一句话布局总结。
7. 走线三选一,**问用户要哪条,别默认交回 GUI**:①跑 Phase E 用 KRT 自动布线(`SKILL.md` Phase E)
   ②用户在 KiCad GUI 手布 ③先不布,直接进 `check-pcb` 审布局。draw-pcb **有**自动布线能力,
   默认甩给 GUI 是旧流程的残留。

## route-ready 验收(回路退出条件,逐条全过才算交付)

布局做完到能直接布线的硬清单。SKILL.md 只点名,判断标准在这里:

1. `check_placement` `hard_fail=false`——0 重叠 / 0 间距违例 / 0 越界 / 非隔离件 0 穿屏障。
2. `run_drc` 无 `courtyard` / `clearance` / `hole` 类违例(此处 `unconnected` 是预期——**仅限未布线的
   Phase D**;Phase E 布完重铺后必须为 0)。规则来源已确认(`set_design_rules` / 基线,见 Phase D 第 0 步与第 4 步)。
3. `check_zones` 退出码 0——**没有任何 GND 铜跨隔离屏障**。铜跨槽同网不产生 clearance,
   DRC 兜不住,只有这一道防线。铺了铜就必须过这条。
4. 隔离器件跨屏障且旋转正确——`get_geometry` 复核各域 pad 落在屏障对应侧。
5. 连接器 / 开关贴板边——`check_placement` 无 `connector_not_on_edge`。
6. **连接器插得进去**——`check_connector_access` `hard_fail=false`。贴边只证明位置对,
   这条证明**开口朝板外、插合面到板边够近、插入通道没被挡**。
   `undetermined` 也算不过:封装判不出朝向时必须查 datasheet 写 `--declare`,
   **不允许留空当通过**——猜错方向的板子 DRC 全绿但插不进,是整批报废级错误。
7. 去耦 HF 电容 pad-to-pad ≤2mm 贴 IC(`cap_far_from_ic`);bulk 大容量可适当远。
8. **退化 courtyard 件**(电解电容等)按 footprint 名查真实体积复核——别信 pad-bbox 估计。
   这些件 `placement_brief` 会标 `geometry_uncertain`,courtyard 退化时 pad-bbox 远小于实体,
   靠它判间距会假阴性(看着没撞、实际撞);Phase D 的 `run_drc` 用真 footprint 几何兜底。
9. **紧凑度**:Phase D 跑 `refit_board` 让板框缩到元件实际范围;看 `fill_ratio`
   (footprint bbox 总面积 / 板面积,不含文字)+ 终图。
   **板框外部给定时**(外壳 / 机箱 / 客户规格锁死尺寸):跑 `refit_board --keep-outline`,
   它不动 Edge.Cuts、只回报 `fill_ratio`。**别跑默认档**——那会把给定板框删掉重画。
   **达标线(缺项目声明时用这个,别凭感觉)**:混合板(有 THT 连接器)`fill_ratio ≥ 0.18`,
   纯 SMD 板 `≥ 0.30`。低于线 → 回 B 收紧再 refit,**不是**直接判 pass。
   两轮 refit 后仍不达标但终图看不出大空洞 → 记为"已知不达标 + 理由"交用户,别硬刷。
10. Read 终图:三域分明、信号链按序、无明显挤团 / 大空洞。

任一条不过 → 回 B 修。逐条判完打终账回执:`[draw-pcb] route-ready: N/10 项通过,未过:<编号>`——
没有这一行 = 没对过账,打到第 6 项停下报完成是不可见的。

## 停止条件 + keep-best

- **停**:上面「route-ready 验收」**10 项逐条全过**(清单与阈值以上节为唯一权威,不在此复述),
  任一条不过就不算完工。
- ⚠️ `check_placement` 闸门干净 **≠** DRC 干净:闸门对 `geometry_uncertain` 件可能误判,Phase D 的 `run_drc` 才是几何裁判。
- **硬上限**:Phase B-C 最多 **6 轮**。到顶仍有验收项不过 → 停,在报告里逐条点名剩余问题交用户,**布局不甩给 GUI 补摆**(走线才可以走 GUI,而且还有 Phase E 自动布线可选)。
- **keep-best**:每轮 C 之后存一份 `<pcb>.round<N>.kicad_pcb` + 记 (hard_fail, hpwl)。
  收敛失败时取:先 hard_fail=false 的,再其中 hpwl 最低的——不取最后一轮。
  **每轮必须打印 `round N / 快照路径 / (hard_fail, hpwl)`**——没有脚本做这件 bookkeeping,
  不打印就等于没存,最后只剩最后一轮那块(可能最差的)板,keep-best 是句空话。
- 回路会原地打转。同一个问题修 2 轮没改善 → 停,在报告里点名,别死磕。

## 完成报告(给用户)

跑了几轮 / 最终 score + hard_fail / DRC 违例数 / 终图路径 / route-ready 验收逐条结果 / 仍需用户处理的点。
合法 ≠ route-ready——如实说哪些验收项你没做到、为什么。
