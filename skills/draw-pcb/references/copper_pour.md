# 铺铜策略 — 逐面逐区判断框架

`add_zones` 把"铺哪一块铜"做成机械执行(zone 几何 / 切槽 / 填充);**铺不铺、铺哪面、铺哪个网**是电路判断,由 AI 按本文逐面逐区决定。跟 draw-pcb 的总分工一致:判断走回路,机械走脚本。

> 反模式:把 `add_zones` 当无脑收尾步骤,两面所有 GND 一锅铺。铺铜在错误的面 / 区会吃掉安规余量、把铜碎成孤岛,反而有害。

## 默认与否决

**默认**:每个铜层、每个区域都铺其所属 GND 网——大块地平面收紧回流、降阻抗、做 EMI 屏蔽,是好习惯。

**但默认是可被否决的**。对每一个 (层 × 区域) 组合,按下面五轴逐条过;命中任一否决条件就该面该区不铺(或换网铺):

| 轴 | 铺 | 不铺 |
|---|---|---|
| **安规** | 该区无高压净距约束 | 高压区铺 GND 会吃掉爬电 / 电气间隙余量 —— **硬否决,不可优化** |
| **回流** | 该区有较大 / 较快 / 高频电流需要就近回流 | 高阻、小电流、低频区(如 µA 级高阻分压链)铺了无回流收益,纯增复杂度 |
| **空间 / 走线密度** | 铜能连成大片连通面 | 走线过密,铜被切成一堆孤岛 —— 无屏蔽 / 回流价值,跳过 |
| **EMI** | 有快沿信号 / 时钟 / 开关节点需要屏蔽参考面 | 纯慢速模拟、无干扰源 |
| **散热** | 有发热件需要铜面散热(配 thermal relief) | 无功率器件 |

判断顺序:**安规 veto 最先**——它是架构红线,其余四轴是收益权衡。安规否决一旦成立,后面四轴的收益再大也不铺。

## 多 GND 网与隔离屏障

板上有多个独立 GND 网(如 `HV_GND` / `LV_GND` / `/GND`)时:

- **每个 GND 网只铺它自己的域**:`HV_GND` 铺高压区,`LV_GND` 铺低压区,不互相越界。
- **禁止任何 GND 铜跨隔离屏障**——铜皮带电位,跨槽 = 把隔离架构废掉。`add_ground_zones` 已按隔离槽把 `HV*` clip 到槽左、`LV*` clip 到槽右;但前提是你**按网分开调用**,不要用一次全 GND 调用让脚本替你猜。
- **铺完跑 `check_zones` 验证**(`tools.md`):它取每个 zone 的填充铜 X 跨度,任一 zone 同时出现在槽两侧就报 fail。比只靠最后 DRC clearance 兜底更直接,net/电压无关。
- 用 `--nets` 精确指定:`add_zones <pcb> --layers B.Cu` 铺全部 GND;`add_zones <pcb> --layers F.Cu --nets LV_GND` 只在正面铺低压地。helper 幂等粒度是 `(net, layer)`,多次调用各管一块,不冲突。

## Phase E:布线后必须重铺

**`route` 会先把副本上的铺铜剥掉再布线**(原因见 `routing.md`「为什么先剥铜」——留着会让 KRT
判该网已连、整网跳过,重铺后碎成孤岛)。所以 routed 板上**根本没有铜**,不重铺就交付 = 交一块光板。
`add_zones` 是 create+fill 幂等一步:在 routed 板上跑,铜绕开走线 / 过孔,过孔自动做 thermal relief。

⚠️ **"重跑一次"不等于裸跑一次。** `add_zones` 默认只铺 `--layers B.Cu`,且不给 `--nets` 时会
一次铺掉所有 GND-like 网。Phase D 若按上面分域调过多次(如 `--layers F.Cu --nets LV_GND` 再
`--layers B.Cu --nets HV_GND`),重铺**必须把那几次逐个复现**。
⚠️ **漏一面不会有任何报错**:那一面根本没有铜,DRC 无铜可查、一声不吭,`unconnected` 也不会涨
(没铜就没有孤岛)。**沉默 ≠ 铺对了**——唯一的核验手段是拿 Phase D 打印的 `(net, layer)` 清单
逐条点名对账,漏掉半个地平面的板会安安静静通过所有闸门。
规则:Phase D 铺铜时**记下每个 `(net, layer)` 组合并打印**,Phase E 照单重放并复述对账结果。

```
Phase E:  route → add_zones(重铺,复现 D 的每个 net·layer) → run_drc → check_zones → render
```

**顺序不可换**:重铺前板上**没有铜**(被 route 剥掉了),那次 DRC 测的不是最终形态;
用了 `--keep-zones` 则是旧铜跟新走线重叠 → 一堆假 `clearance`。两种情况都一样:
会误导成"布线出问题了"。重铺后那一次 DRC 才是唯一有效的几何裁判(refill 本身也可能
新冒真 `clearance` 违例,同一次一起查)。

## 为什么不用 KRT 的 route_planes 自动铺铜

KRT 自带 `route_planes.py`(建铜 + via stitching)功能更强,但它用 Voronoi 按过孔位置分铜,**不知道隔离槽在哪**——会让一个域的 GND 铜斜穿隔离槽渗进另一个域。对有隔离屏障的板这是安规架构破坏,不可接受。所以 draw-pcb 用自家 `add_zones`(按隔离槽 clip),via stitching 需要时在 KiCad GUI 手动补几个 stitching via。
**什么时候"需要"有明确判据**:Phase E 重铺后 DRC 报 `unconnected: 填充区[GND] ↔ 填充区[GND]`
(铜被走线切成孤岛,走线密的那面常见)或 `starved_thermal` → 就是该补 stitching via 的信号,
把碎岛缝到对面完整的地平面。细节见 `known_issues.md`。无隔离屏障的简单板可另行评估 KRT route_planes。
