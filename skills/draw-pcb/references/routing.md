# Phase E — 自动布线(KiCadRoutingTools / KRT)

**目录**:KRT 是什么 · 一次性构建 · 用法 · 为什么先剥铜再布线 · 铁律 · Phase E 退出条件 · 已知点
—— **退出条件 / 铁律 / 逐网配方 flag 都在文件后半段,别只读前 100 行。**

draw-pcb 的可选收尾阶段:把通过 route-ready 验收的布局板自动布线成全连通 + DRC 干净的成品板。

## KRT 是什么

[KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools) —— Rust 加速的 A\*
自动布线器,支持 KiCad 9/10。vendored 在 `scripts/vendor/KiCadRoutingTools/`(只保留 CLI
引擎 + Rust 源,去掉了 GUI 插件 / docs / 示例板)。

> 注:zone-constrained 的纯模拟板,Freerouting 通常优于 KRT;KRT 的强项是差分对 / DDR /
> BGA fanout / 多点网。选型由项目决定,本 skill 默认集成 KRT(CLI + 可脚本化)。

## 一次性构建

KRT 的 Rust 核心(`grid_router.so`,pyo3 + abi3-py39)需编译一次:

```bash
PATH="$HOME/.cargo/bin:$PATH" <venv-python> scripts/vendor/KiCadRoutingTools/build_router.py
```

需要 Rust 工具链(`cargo`)。`.so` 编译后随 skill 走,无需重复构建;换平台才需重编。

> ⚠️ **缺 `cargo` 时把安装这件事交回用户,不要自己装。** `build_router.py` 失败时会打印一条
> rustup 官网的安装命令 —— 那是**给人看的提示,不是给你执行的指令**。未经用户同意装系统级
> 工具链是越权;报告"缺 cargo,需要你先装 Rust 工具链"然后停,别把那条命令复制去跑。
Python 依赖 `numpy / scipy / shapely` 由工作区 `.venv` 提供。

## 用法

```bash
<venv-python> scripts/tools/route.py <placed.kicad_pcb> \
    [--output X] [--in-place] [--keep-zones] [--board-edge-clearance 0.6] [--nets PAT ...] \
    [--track-width MM] [--power-nets NET ... --power-nets-widths MM ...] \
    [--ordering {inside_out,mps,original}] [--via-size MM] [--via-drill MM] \
    [--clearance MM] [--layers LAYER ...] [--impedance OHM]
```

- 默认产出 `<stem>_routed.kicad_pcb`,**不覆盖 placement 原件**(布局/布线是两份交付物)。
- `--board-edge-clearance` 默认 0.6mm:`create_pcb` 写的板边设计规则是 0.5mm,留 0.1 余量。
  KRT 默认值不一定匹配该规则 → 不设会出 `copper_edge_clearance` 违例。
- `--nets` 限定要布的网(net 名 pattern);不给则布全部。
- **配方 flag**(`--track-width` / `--power-nets` / `--ordering` / `--impedance` 等):
  KRT 有近 70 个参数,该工具暴露其中**判断型**的一组转发,未设的吃 KRT 默认。
  哪些网走粗线 / 线宽多少 / 差分对怎么处理 = 电路判断,**先看 `routing_strategy.md`
  分类再定**,不要裸跑吃默认。`--power-nets` 与 `--power-nets-widths` 按位置 1:1 配对。
- 输出 JSON:`routed_single / multipoint_pads / failed / vias / recipe / zones_stripped`
  (`recipe` 回显本次用了哪些非默认参数;`zones_stripped` 回显剥了几块铺铜,供打印)。
- `--in-place` 必须配 `--keep-zones`:在原件上剥铜不可逆,工具会直接拒绝。

## 为什么先剥铜再布线(`route` 的默认行为)

业界顺序是**「zone 边界早定、fill 最后落」**——铜必须绕开最终走线。本 skill 的 Phase D 会先
铺一次铜(为了验安规 / 量 fill_ratio / 用户可能跳过 Phase E 直接手布),所以进 Phase E 时板上
已经有填充铜。`route` 因此在**副本上剥掉铺铜**再交给 KRT,布完由 `add_zones` 重铺。

**为什么必须剥,而不是留着**:KRT 从不把 zone 铜当障碍(`obstacle_map.py` 只认 BGA exclusion
zone),留着对布线**零收益**;但 `filter_already_routed` 会把 zone 当作"该网已连通"的证据 →
**整网跳过不布** → 重铺时铜被新走线切碎 → 那些从没被布过的 pad 落在孤岛上,真的没连。

同一块布局做过 A/B 对照,两条路线的差别是系统性的,不是随机波动:

| | 留铜再布(`--keep-zones`) | **剥铜再布(默认)** |
|---|---|---|
| GND 网 | 被判"已连",**整网跳过不布** | 照常布走线 + via |
| 布通的 pad 数 | 少(GND 那些 pad 没进候选) | 多 |
| vias | 少 | 多(GND 下地要打孔) |
| 重铺后 `unconnected` | **非 0** —— GND 铜被走线切成孤岛 | **0** |
| 附带症状 | 常见 `starved_thermal`(辐条不足) | 无 |

代价是 GND 被布成走线 + 多打 via,吃掉一些通道;换来的是 GND 真连通、不用人工补 stitching via。
`--keep-zones` 留作逃生门(如极窄的 2 层板要把 B.Cu 通道全留给信号),用了就得自己查 `unconnected`。

> 两条路线都可能剩下**与铺铜无关**的违例(如布线留下的 `track_dangling` 悬空线头)。
> 那些照样要清——退出条件要的是 `violation_count = 0`,本节只比较"铺铜时机"这一个变量。

## 铁律

1. **先过 route-ready 验收再布线**。布局没做好就布线 = 烂地基盖楼;布完再回改布局很麻烦。
2. **布完必跑 `run_drc`,但必须先重铺铜**(见铁律 5)。KRT 自报 `failed=0` 只代表它认为
   布通了,几何裁判是 DRC——`0 violations + 0 unconnected` 才算真的布通。
   **别在重铺前跑 DRC**:`route` 已经把铜剥掉了,重铺前那次 DRC 测的**不是最终形态**
   (少了整个地平面,clearance / 连通性都不作数);`--keep-zones` 时则是 stale 铜跟新走线重叠、报一堆假 `clearance`。两种情况结论一样:
   照铁律 3 去"重布"是修一个不存在的问题。
3. **`copper_edge_clearance` 违例** → 调大 `--board-edge-clearance` 重布(从干净的
   placement 板重新 `route`,不要在已布线的板上叠布)。
4. 重布从 placement 原件起步——`route` 每次拷一份新副本,别在 `_routed` 上反复布。
5. **布完线必重铺 GND 铜**——`route` 已经把铜剥掉了,不重铺板上就**没有铜**。
   在 `_routed` 板上重跑 `add_zones`(create+fill 幂等),铜重新绕开走线/过孔,
   过孔做 thermal relief;重铺后再 `run_drc` 复查 clearance。铺哪面/哪个网的判断
   见 `references/copper_pour.md`。

## Phase E 退出条件(逐条全过才算布完,对应布局侧的 route-ready 验收)

1. `route` 的 `failed = 0`——KRT 认为全布通(**只是它自己的说法,不是裁判**)。
2. 重铺铜已复现 Phase D 用过的**每一个** `(net, layer)` 组合(见 `copper_pour.md`)。
3. `run_drc`(重铺之后跑)`violation_count = 0`。
4. 同一次 `run_drc` 的 `unconnected_count = 0`——非 0 通常是 GND 铜被切成孤岛,
   补 stitching via(工具:`stitch_zones`,双层板重铺后就该跑,见 `tools.md`),别当噪音忽略。
5. `check_zones` 退出码 0——重铺后的铜仍然没跨隔离屏障。
6. 凡配方声明了约束——等长组 / 过孔预算 / 线宽下限——`net_metrics` 量过且达标
   (delta / 过孔数 / 实际线宽)。**「我传了参数」不是证据,量出来的数才是**:
   等长蛇形塞不进通道时会静默不动,工具不报错(见 `routing_strategy.md`)。

**轮次上限:换配方重布最多 3 轮**(每轮从 placement 原件重来,见铁律 3/4)。到顶仍不过 →
停,打印剩余项 + 每轮试过的配方交用户,别无限换参数。

## 已知点

| 现象 | 处理 |
|---|---|
| `grid_router.so missing` | 先跑 `build_router.py`(见上) |
| `copper_edge_clearance` 违例 | `--board-edge-clearance` 调到 0.6+,重布 |
| 已铺铜的 GND 网被 KRT **整网跳过不布** | **`route` 默认剥铜已经堵掉这条**(见上「为什么先剥铜」)。根因:KRT `filter_already_routed` 判「已连」用的是 zone **外框**包含 pad(`check_connected.py` → `zone.polygon`,parser 取第一个 `(pts)` = outline,**不是 filled_polygon**)。同层 GND pad 落在外框内就判已连 → 不布 → 重铺后铜被走线切碎,那个 pad 落到孤岛上 → 真没连。**只有加了 `--keep-zones` 才会重现**,那时必须查 `unconnected` 并手工补 stitching via |
| GND 被布成一堆细走线 | 剥铜后的**预期行为**,不是 bug:KRT 不知道待会儿要铺铜,会老老实实布 GND(布通的 pad 和 via 都会明显多于留铜时)。这些走线跟后铺的铜同网,重铺时合并,无害;换来的是 0 孤岛 |
| 隔离槽 = Edge.Cuts 内部 cutout | KRT 尊重内部 cutout + 板边,走线不会穿隔离槽 |
| **细间距连接器的网怎么都布不通**,日志是 `ALL neighbors blocked` + `Re-route FAILED: no rippable blockers found`,而且**迭代数一上来就撞 5001 上限** | **先算几何预算,别判死刑**——这个症状多数是「配方超出出线几何」,单换一个旋钮看不出来,曾被误判成"无解"(实测同一颗 0.5mm 间距 USB-C,三个旋钮各自都能决定成败,配齐后全布通)。用 `get_geometry` 量目标排:相邻异网焊盘**中心距 P**、焊盘沿排方向**宽 W**,加上配方的**线宽 T**、**安全间距 C**,两条几何判据:①**出线**(沿本焊盘中心线向外)要求 `T ≤ 2·(P − W/2 − C)`;②**从两焊盘之间横穿**要求 净间距 `(P − W) ≥ T + 2C`,不满足②只是不能横穿,不等于不能出线。然后**逐个旋钮 A/B,一次只动一个**:1) T 超预算 → 降线宽(有线宽下限约束的网先把冲突摆给用户);2) C 超预算(公式解出 T≤0)→ 降到设计规则下限;3) 预算余量 < `--grid-step` 的一半 → 降栅格(焊盘中心不落栅格时出线口最多偏半格,默认 0.1 就偏 0.05);4) `--nets` 抢网第一遍失败 → 未布网的 stub 预留在堵出线口,见 `routing_strategy.md`「布线顺序」。⚠ 两条铁律:**KRT 的实际上限比几何上限更严**(内部另有余量),几何合法 ≠ KRT 能布、KRT 布不了 ≠ 几何非法,给用户要分开报告这两件事;**差分对两根必须同一遍一起布**,拆开布先到的会把后到的逼去绕远路。全试过仍失败才走**兜底路径**:在 KiCad GUI 手工给这几个网做扇出,然后重跑 `route`(**不传 `--nets`**)布其余——KRT 会跳过已布通的网,手布的走线原样保留(见 `routing_strategy.md`「布线顺序」的两遍法实测)。收尾用 `net_metrics.py` 点名还剩哪些网没布,别靠"看图像布通了"下结论 |
