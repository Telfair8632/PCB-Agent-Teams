# PCB-Agent-Teams — KiCad PCB 工作流

**中文** · [English](README.md)
<!-- SYNC: 本文件与 README.md 是同一份文档的两个语言版本，改一个必须同步改另一个。 -->

[![License: PolyForm NC 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-informational)](LICENSE.md)
![KiCad 10](https://img.shields.io/badge/KiCad-10-blue)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
![Runtime: Claude Code only](https://img.shields.io/badge/运行环境-仅%20Claude%20Code-orange)
![Platform: macOS tested, Linux untested](https://img.shields.io/badge/平台-macOS%20已测%20%7C%20Linux%20未测-lightgrey)

**说出你想要的板子，拿回一个 KiCad 工程和一套可直接下厂的 Gerber** —— 每一个元件都对着分销商实时库存核过，
每一个阶段都由脚本、SPICE 和 DRC 判过，而不是听模型自己说「没问题」。

底层是这样：一个建在 KiCad 10 上的多项目 PCB 工作区，十个 skill 驱动 Phase 0–5 流水线，覆盖从拓扑讨论到
Gerber 出货的完整链路。

<!-- TODO(visual): 这里放一张实拍板照或流程产出的 3D 渲染图——整页目前只差这个。
     文件放 assets/（被 git 跟踪；docs/ 是本地目录，已 gitignore），引用写 assets/<name>.png。
     只用这套流程真正做出来的板子。 -->

### 跑一次长什么样

```text
你：我要做个隔离型 400V 母线电压采集板，输出 0–3.3V 给 MCU 的 ADC。

/circuit-design        定下拓扑：隔离放大器 + 分级高压分压，锚点件点名
/component-selecting-* 每个元件 3 个实时候选——全部有货，全部有现成 KiCad 库
/component-preparing   datasheet 抓齐，symbol + footprint + 3D 落库，BOM 锁定
/draw-schematic        由 Python 源码生成 .kicad_sch，ERC 清零
/check-schematic       分压比重跑 SPICE 仿真，每个引脚对着 datasheet 逐一核对
/draw-pcb              高低压分区、隔离屏障守住、GND 铺铜、DRC 清零
/check-pcb             44 条 EMC 规则 + 热 + 寄生 + 原理图↔PCB 网表逐项比对
/release               Gerber + CPL + 生产 BOM + ORDER_GUIDE.md，打包好交厂
```

你不会被这个顺序绑死——每一步都是可以单独跑、重跑、手改或跳过的工具盒。

### 往下读之前：两件事先说清楚

1. **硬性依赖 KiCad 10；平台只在 macOS 上测过。** skill 直接调 `kicad-cli` 和 KiCad 自带的 `pcbnew`
   Python API。Linux 的路径代码里铺好了但没验证过 —— 解释器探测表含 `/usr/lib/kicad/python3`，
   `kicad-cli` 定位覆盖 Flatpak、Snap、Nix。真正的缺口是 Windows：`kicad-cli` 找得到，但没有
   `pcbnew` 解释器路径，`draw-pcb` 里改写板文件那几步没有解释器可调。细节见[前置条件](#前置条件)。
2. **好消息：国内开箱可用，一把 API key 都不用申请。** 选品阶段（Phase 2）的中国大陆通道走
   LCSC jlcsearch + jlcparts 公开数据分片，**零 key、零注册**。日本通道走 DigiKey + Mouser + LCSC
   三路并行（需要一个免费 DigiKey key）。**选品是唯一跟地区绑定的阶段**，其余所有 phase 与地区无关。
   其它地区要么照着已有的两个选品 skill 改一个自己的（它们都是共享引擎之上的薄壳），要么手工选品把
   清单交给 `component-preparing`；流水线绝不会静默 fallback 到别的地区。细节见
   [首次配置](#首次配置)。

> **运行环境 — 多 harness（fork 改造）。** 所有 skill 都在 harness 无关的 `skills/` 目录；
> [`bootstrap.py`](bootstrap.py) 把它们以链接（Windows junction / 其它系统软链）装进各 agent 的
> 发现目录，并从 canonical 的 [`AGENTS.md`](AGENTS.md) 生成各 harness 的指令文件。已在 ZCode 和
> Claude Code 上验证；`SKILL.md` 是开放格式，接新的 harness 通常只需在 `bootstrap.py` 里加一条。
> 见 [换用其它 agent](#换用其它-agent)。
>
> **刚 clone 下来？** 用你的 agent 打开这个文件夹，跑 `/setup`。它先问一个短问题，然后用你的语言引导你
> 配完所属地、工具链和个人档案。`USER.md` 一旦存在，它就不再触发。

---

## 架构

```text
PCB-Agent-Teams/
├── README.md             ← 英文版
├── README.zh-CN.md       ← 本文件（中文版）
├── AGENTS.md             ← canonical 路由表（ZCode 等自动加载）
├── CLAUDE.md             ← 由 bootstrap.py 从 AGENTS.md 生成，供 Claude Code（gitignored）
├── bootstrap.py          ← 多 harness 安装器：把 skills/ 链接进各 agent 的发现目录
├── pcbenv.py             ← 共享环境发现（kicad-cli / KiCad Python / venv / 规格文件 walk）
├── LICENSE.md            ← PolyForm Noncommercial 1.0.0
├── THIRD_PARTY_NOTICES.md ← 引入/派生的第三方代码 MIT 声明
├── USER.md.example       ← 用户档案模板（复制成 USER.md）
├── USER.md               ← 你的档案：在手硬件 / 所属地 / 能力 / 偏好（gitignored）
├── requirements.txt      ← Python 依赖
├── assets/               ← README 用到的图片（进 git；`docs/` 是本地目录，已 gitignore）
├── skills/               ← 10 个 skill + `setup`（首次配置引导）—— harness 无关
├── tests/                ← 仓库级测试（pcbenv 等）
├── .claude/references/   ← 工作区元协议（protocols.md）
├── lib_external/         ← 共享元件库（初始为空；component-preparing 往这里落 symbol/footprint/3D）
├── lib_cache/sources/    ← 外部库只读 cache（pre-filter 池）
├── Projects/<name>/      ← 项目目录（由 project-init 生成）
│   ├── CLAUDE.md         ← 项目设计基准（定下就不动）
│   ├── STATUS.md         ← live 进度 / artifact 索引
│   ├── .gitignore        ← 项目级 ignore 规则
│   ├── datasheets/       ← datasheet PDF + 选品证据 JSON + BOM 就绪标记文件 + 采购 BOM
│   ├── kicad/            ← circuit-synth .py + 生成的 .kicad_sch / .kicad_pcb
│   ├── reference_designs/← 厂商参考资料
│   ├── layout/           ← 摆件 / 布局工作文件
│   ├── docs/             ← BOM / 文档
│   ├── analysis/         ← analyzer JSON / 仿真产物（由检查 gate 生成）
│   ├── _artifacts/       ← shortlist JSON + 4 轴采购偏好（selecting 写，release 读）
│   └── release/<ts>/     ← Gerber + 生产 BOM + CPL（由 release 生成）
├── logs/                 ← 运行日志
├── .venv/                ← Python 3.12 venv（gitignored）
├── .env.example          ← API key 模板（复制成 .env）
└── .env                  ← 你的 API key（gitignored）
```

---

## 亮点

三道 gate，针对的都是「AI 画 PCB 时人们最不信任的环节」。

### 1. 零幻觉选品

每一个元件型号都来自分销商实时 API。模型没有机会自己编一个出来。

- **强制走分销商 API** — 没有任何 MPN 来自记忆。日本地区（代码里叫 locale=JP）**三路并行**查 DigiKey / Mouser / LCSC，一个元件拿到 N 个候选并排比较；中国大陆地区走单路免 key 的 LCSC 通道
- **硬性过滤**：库存 active/nrnd + 有现成元件库 + 规格匹配；零库存或没有可用库的直接淘汰
- **快速检索**：基于硬指标（footprint / 容差 / 温度等级 / 耐压等级等）秒级返回候选清单
- **整份 BOM 一趟跑完**；只有当某个规格在市面上无解时才停下来问用户

### 2. 资产没落到硬盘上，一笔都画不了

担心 AI 跳步直接开画？入口 gate 会拦住。

- 候选清单定稿前，**先抓 datasheet**，同时把元件库落到本地（原理图符号 / 封装 / 3D 模型）
- **四项一致性核对**：MPN 对锁定 BOM / 封装类别（插件 vs 贴片）对 datasheet / 通用符号冒充真实 IC / 引脚数对元件实际引脚数 —— 任一项不符，gate 直接判 fail
- **元件型号自动注入**原理图属性，避免画图时手输出错
- **资产没齐就锁死画图入口** —— 绕不过去

### 3. 每个数都查两遍，而且用两种不同的方法

没有哪个数字只看一眼就算数。每个关键量都由两层独立手段验证，任一环节不过就整体回退 —— 绝不绕行。

**原理图阶段**（`check-schematic`）：

- ERC 分级 + analyzer 规则 ID，以及 datasheet 引脚级交叉核对（防「零错误」假通过的 `total_errors == 0` 裸输出 gate 在上一阶段 `draw-schematic` 里跑）
- SPICE 子电路仿真（不只查连通性 —— 查真实动态行为）
- Design review 报告 —— AI 生成的分析，交人签字确认，不自动通过

**PCB 阶段**（`check-pcb`）：

- DRC 设计规则检查
- EMC 预合规分析 —— 18 个类别共 44 条规则（地平面完整性 / 去耦 / I-O 接口滤波 / 开关电源 EMC / 时钟走线……）
- 热分析（功耗 / 温升）
- 寄生仿真（从 PCB 反提 R/L/C，重跑 SPICE）
- **原理图 ↔ PCB 网表交叉核对**：把原理图里「哪个引脚连哪个引脚」的清单，逐条对上 PCB 上的实际走线，查漏连、错连、短路

**出货阶段**（`release`）：

- **四轴采购偏好 gate**：渠道 / 品牌 / 价格 vs 库存 / 黑名单，必须先记录在案才能打包 —— `ORDER_GUIDE.md` 里的推荐下单路径完全由它们决定，所以打包脚本会停下来问，而不是瞎猜
- **采购 BOM 与生产 BOM 分轨**：买料清单和贴片清单独立生成，避免买错或漏件

---

## Skill 流水线

每个 skill 都是独立工具盒，**不是必经流水线**。任一阶段都可以：用 skill / 手工做 / 跳过。multi-step 的 skill 也支持中途人工审核（render / DRC / 仿真）；不满意就回退。

| Phase | Skill | 职责 |
| --- | --- | --- |
| 0 骨架 | `project-init` | 搭项目骨架：5 个子目录（datasheets / kicad / reference_designs / layout / docs）+ CLAUDE.md（9 章节设计基准）+ STATUS.md + .gitignore |
| 1 拓扑 | `circuit-design` | 锁定电路拓扑，定下锚点件，写项目 9 章节设计基准 |
| 2 选品 gate | `component-selecting-*` | 按地区路由（locale → vendor），产出 shortlist JSON |
| 2.5 备料 + BOM gate | `component-preparing` | 先对每个首选件做适用性审查（对照它在电路里的角色），然后抓 datasheet、把元件库落到本地、写选品证据，最后放下 BOM 就绪标记文件 + 采购 BOM CSV |
| 3 原理图生成 | `draw-schematic` | 源码驱动生成 `.kicad_sch` + ERC clean + 视觉验证 |
| 3.5 原理图检查 gate | `check-schematic` | ERC 分级 + analyzer + SPICE 子电路仿真 + datasheet 引脚级交叉核对 + design review |
| 4 PCB 生成 | `draw-pcb` | 区域分区布局 + GND zone + DRC + 视觉 PDF，可选自动布线 |
| 4.5 PCB 检查 gate | `check-pcb` | DRC + EMC + 热 + 寄生 SPICE + sch↔pcb 交叉核对 + gerber 审计 |
| 5 出货 | `release` | Gerber / CPL / 生产 BOM + 文档 PDF + 打样厂决策 + 打包 |

> **地区路由**：`component-selecting` 是 phase 名，具体 skill 由 `USER.md §0` 的 locale 决定。已实现两个地区：**`component-selecting-JP`**（DigiKey JP + Mouser JP + LCSC，需要一个免费 DigiKey key）和 **`component-selecting-CN`**（LCSC jlcsearch + jlcparts 数据分片 —— **零 API key**）。美国版还没做。其它地区不要静默 fallback —— 告诉用户对应的 skill 还不存在。

任何 gate 不过：**回退**，不要绕过。详细 Phase 表见 [CLAUDE.md](CLAUDE.md)。

> 另有一个非流水线 skill：**`setup`** —— 首次配置引导。它以 `USER.md` 不存在为触发条件，所以新 clone 时跑一次，之后再不触发。

---

## 典型流程

```text
[空目录]
   │
   ▼  /project-init
[项目骨架]
   │
   ▼  /circuit-design          ← 拓扑 + 锚点件
   ▼  /component-selecting-*   ← shortlist
   ▼  /component-preparing     ← datasheet + 元件库 + BOM
   ▼  /draw-schematic          ← .kicad_sch + ERC
   ▼  /check-schematic         ← ERC + analyzer + SPICE + datasheet 交叉核对
   ▼  /draw-pcb                ← .kicad_pcb + DRC
   ▼  /check-pcb               ← DRC + EMC + 热 + 寄生 + 交叉核对
   ▼  /release                 ← Gerber + 生产 BOM
[release/<ts>/ + release_<ts>.zip]
```

---

## 三层文档职责划分

| 文件 | 角色 |
| --- | --- |
| `CLAUDE.md`（工作区根） | skill 路由表；不含 domain 知识 |
| `Projects/<name>/CLAUDE.md` | 项目设计基准（定下就不动）：设计意图、参数、BOM 锚点 |
| `Projects/<name>/STATUS.md` | 项目 live dashboard：当前 phase、artifact 索引、change log |
| `skills/<skill>/SKILL.md` | skill 手册：跨项目约束、命令、失败模式 |

---

## 两类 BOM — 别混

| BOM 类型 | 谁写 | 用途 |
| --- | --- | --- |
| **采购 BOM** | `component-preparing` | 从分销商下单买料 |
| **生产 BOM / CPL** | `release` | fab 厂装配 / 贴片 |

这两者上游还有第三个文件：选品证据 CSV（`bom_v01.csv`，由 `component-selecting` 写）—— 它按 MPN 记录 vendor 证据供后续 gate 用，从不用来下单或装配。

细节见 [`component-preparing/references/bom_lifecycle.md`](skills/component-preparing/references/bom_lifecycle.md)。

---

## 首次配置

> **引导式（推荐）。** 用 Claude Code 打开这个文件夹，跑 **`/setup`**。
> 它先问一个短问题，然后用你的语言把下面这些全办了 —— 地区路由、工具链检查、`USER.md`，以及只在你所在地区需要时才配的 key。它以 `USER.md` 不存在为触发条件，所以新 clone 跑一次，之后再不触发。下面的手动步骤是同一件事的手工版。

> **地区支持。** 如开头所说，Phase 2（选品）是唯一跟地区绑定的阶段 —— 其它所有 phase 都与地区无关，原样运行。在 `USER.md §0` 设你的所属地：
> - **中国大陆** —— 开箱支持，走 `component-selecting-CN`：LCSC jlcsearch + jlcparts 数据分片，**完全不需要 API key**（生命周期状态如实标为 `unverified`，因为 LCSC 不提供 NRND 数据）。
> - **日本** —— 原生的 `component-selecting-JP`（DigiKey JP + Mouser JP + LCSC；需要一个免费 DigiKey key）。
> - **其它地区** —— 要么 **(a)** 把已有两个 skill 之一改成你所在区域的变体（两者都是共享的地区驱动引擎之上的薄壳 —— 往 `locale_mapping.yaml` 加一个 locale 块，再照 CN skill 的结构复制），要么 **(b)** 手工选品，把清单交给 `component-preparing`。流水线刻意不会静默 fallback 到别的地区的 skill。

### 前置条件

- **[KiCad 10](https://www.kicad.org/download/)**，跑在 **macOS** 上 —— 唯一测过的平台。skill 调用 `kicad-cli` 和 KiCad 自带的 `pcbnew` Python API（不走 MCP），两者的定位方式不一样：
  - `kicad-cli` —— 先查 `PATH`，再按平台探标准安装位置：macOS app bundle + Homebrew，Linux Flatpak/Snap/Nix，Windows Program Files/Chocolatey/MSYS2。把 `kicad-cli` 放进 `PATH` 在哪个平台都够用。
  - **`pcbnew` 解释器** —— 只在 KiCad 自带的 Python 上探：`/Applications/KiCad/KiCad.app/…/Python.framework/…`（macOS）和 `/usr/lib/kicad/python3`（Linux）。**没有 Windows 路径**，所以 `draw-pcb` 改写板文件那几步在 Windows 上找不到解释器。Linux 有路径，但从没跑过。
  - （`KICAD_ROOT` 跟这个无关 —— 它覆盖的是*项目工作区根目录*，不是 KiCad 安装位置。）
- **Python 3.12**（不支持 3.13 / 3.14）。
- **[ngspice](https://ngspice.sourceforge.io/)** 在 `PATH` 上 —— `check-schematic` 的子电路仿真只认 ngspice（`brew install ngspice`）；`check-pcb` 的寄生 / PDN SPICE 也接受 LTspice 或 Xyce，自动探测。没有仿真器两者都不会失败 —— SPICE 步骤会跳过，并在报告里如实标注。
- API key —— **取决于地区**：**日本**流水线最少需要一个免费 [DigiKey developer](https://developer.digikey.com/) 账号；**中国大陆**流水线**完全不需要 key**。完整服务清单和各自作用见 [外部 API](#外部-api)。

> **两样东西只以源码形式发布 —— 按需自己编译或拉取：**
> - Rust 栅格布线器（`draw-pcb/.../rust_router/`）只发 Rust 源码，编译产物被 gitignore。在 `draw-pcb/scripts/vendor/KiCadRoutingTools/` 下跑 `python3 build_router.py` 编译（需要 [Rust 工具链](https://rustup.rs/)）—— 只有可选的自动布线阶段用得上。
> - `lib_cache/sources/`（当 pre-filter 用的外部 KiCad 库池）被 gitignore，发布时为空。需要时自行把上游库重新 clone 进去；在那之前 `component-selecting` 会在没有本地池的情况下运行。

### 配置

```bash
# 1. 建 Python 环境并装依赖
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # 一次性下载浏览器 —— DigiKey 页面探测 / datasheet 抓取需要

# 2. 配 API key
cp .env.example .env
#    然后编辑 .env：填 DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET
#    （只有日本需要 —— 中国大陆不需要任何 key，跳过这步）
#    其它 key（Mouser / element14 / Firecrawl）是可选的 —— 见下面「外部 API」

# 3. 建你的用户档案
cp USER.md.example USER.md
#    然后编辑 USER.md：§0 的 locale 决定跑哪个 component-selecting skill
#    （§0 出厂是 [待填] —— 没有默认值，必须自己填；日本和中国大陆
#    都已实现，locale 为空绝不会静默 fallback）。其余 [待填] 字段
#    遇到再补；skill 在推荐任何元件之前都会读它，所以不会推荐
#    你没法测的硬件、或你焊不上的封装。
```

### 运行

- **Claude Code**：先跑一次 `python bootstrap.py`，然后打开这个文件夹 —— 根 `CLAUDE.md`（生成的）自动加载，skill 用 `/<skill-name>` 调用。新 clone 从 `/setup` 开始。
- **ZCode**：先跑一次 `python bootstrap.py` —— skill 落进 `~/.zcode/skills`，根 `AGENTS.md` 自动加载。
- **纯 shell**：先加载 key `set -a && source .env && set +a`，然后按 `.venv/Scripts/python.exe skills/<skill>/scripts/<script>.py`（Windows）或 `.venv/bin/python skills/<skill>/scripts/<script>.py`（POSIX）跑任意 skill 脚本。

### 注意

- `.env`（你的真实 key）和 `.venv/` 已 gitignore，永远不会被提交 —— 别把 key 弄进 git。
- `Projects/` 发布时为空；`project-init` 首次运行时会在那里搭出新板子目录。
- 共享库命名规范：[`lib_external/CONVENTIONS.md`](lib_external/CONVENTIONS.md)。

---

## 换用其它 agent

**这个 fork 天生多 harness。** `bootstrap.py` 一条命令把工作区接进你在用的所有 agent —— 把每个
`skills/<name>` 链接（Windows junction / POSIX 软链）到各 harness 的发现目录，并从 canonical 的
`AGENTS.md` 生成对应 harness 的指令文件：

```bash
python bootstrap.py            # 安装/刷新所有已知 harness（ZCode、Claude Code）
python bootstrap.py --list     # 干跑：只看计划，不改任何东西
```

它幂等，clone 或布局变更后重跑即可：已正确的链接不动，失效链接（比如还指着 2026-10 之前的
`.claude/skills` 旧布局）会被刷新，恰好同名的真实目录绝不碰，`CLAUDE.md` 只在 `AGENTS.md` 变了时才重写。

harness 覆盖：

| Harness | 指令文件 | skill 目录 | 状态 |
| --- | --- | --- | --- |
| ZCode | `AGENTS.md`（canonical，进 git） | `~/.zcode/skills/` | ✅ 已验证 |
| Claude Code | `CLAUDE.md`（生成） | `~/.claude/skills/` | ✅ 已验证 |
| Codex / Copilot / Gemini | — | — | 在 `bootstrap.py` 的 `HARNESSES` 里加一条 |

**能期待什么。** skill *发现*这样移植是干净的。skill *执行*取决于 harness —— skill 会 shell out 调
`kicad-cli`、项目 venv 和分销商 REST API，各 agent 沙箱命令与网络访问的方式不同。环境发现本身已收拢在
[`pcbenv.py`](pcbenv.py)（认 `KICAD_CLI` / `KICAD_PYTHON` / `KICAD_ROOT` 环境变量覆盖），所以默认安装
路径够不着的 harness 也可以显式指过去。

---

## 外部 API

> **下面这些 key 只适用于日本地区**（`component-selecting-JP` 三路并行查 DigiKey JP + Mouser JP + LCSC，DigiKey 是主可购性判定源；Mouser / element14 的 key 同时也被 `check-pcb` 的生命周期审计和 `component-preparing` 的分销商查询读取）。**中国大陆流水线（`component-selecting-CN`）完全免 key** —— LCSC jlcsearch + jlcparts 公开数据，什么都不用注册。美国地区变体还没做。

### 分销商 / 采购 API

| 服务 | 干什么 | 是否必需 | 环境变量 | 在哪申请 |
| --- | --- | --- | --- | --- |
| **DigiKey** | 主可购性判定源 —— JP 仓的实时库存 / 价格 / 生命周期（NRND/EOL）；驱动元件 shortlist | ✅ 必需 | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` | [developer.digikey.com](https://developer.digikey.com/) —— 免费，OAuth |
| **Mouser** | 第二等价源 —— 交叉核对库存/价格 + 拉 datasheet 链接 | ⭐ 推荐 | `MOUSER_SEARCH_API_KEY`, `MOUSER_PART_API_KEY` | [mouser.com/api-hub](https://www.mouser.com/api-hub/) |
| **element14 / Farnell** | 额外分销商源，补库存 + 规格 + 生命周期审计 | ○ 可选 | `ELEMENT14_API_KEY` | [partner.element14.com](https://partner.element14.com/) |
| **LCSC / JLCPCB** | 中国仓，**发货到日本**（元件可以跟 JLCPCB 打样单合并），所以对 JP 流水线仍是有效第三源 + 通过 `easyeda2kicad` 抓 KiCad 库 | — 免 key（公开） | — | — |
| **Firecrawl** | 分销商不提供直链 PDF 时的 datasheet 网页抓取兜底 | ○ 可选 | `FIRECRAWL_API_KEY` | [firecrawl.dev](https://firecrawl.dev/) |

所有 key 放 `.env`（从 `.env.example` 复制）。日本地区下 DigiKey 是主源；其余都是扩大覆盖或加功能。注意脚本缺 key 不会中止 —— 只警告，该 vendor 的查询返回 `fetch_error`，所以没 key 的 JP 运行会悄悄丢掉主可购性判定源。中国大陆整节跳过 —— 不需要任何 key。想要非默认区域/币种，设可选的 DigiKey locale 变量（`DIGIKEY_LOCALE_SITE=JP` 等）。

---

## 文档入口

| 我想…… | 去哪 |
| --- | --- |
| 配置一个新 clone | 在 Claude Code 里跑 `/setup`，或看 [`skills/setup/SKILL.md`](skills/setup/SKILL.md) |
| 了解整体路由 | [CLAUDE.md](CLAUDE.md) |
| 学某个具体 skill | `skills/<skill>/SKILL.md` |
| 看跨项目电气铁律 | `skills/circuit-design/references/electrical_invariants.md` |
| 看共享库命名规范 | [lib_external/CONVENTIONS.md](lib_external/CONVENTIONS.md) |
| 看工作区元协议（计划先行 / sub-agent 分工 / 监控） | `.claude/references/protocols.md` |

---

## 致谢

本工作区站在优秀开源工作的肩膀上。诚挚感谢以下项目及其维护者：

- **[KiCad](https://www.kicad.org/)** —— 整条流水线的 EDA 基座。每一份原理图、板子、ERC/DRC 检查、渲染和 Gerber 都经过 `kicad-cli` 和 `pcbnew` Python API，建立在官方 KiCad symbol / footprint / 3D 库之上。
- **[ngspice](https://ngspice.sourceforge.io/)** —— 驱动全部 SPICE 检查：`check-schematic` 的子电路仿真和 `check-pcb` 的寄生重仿真。
- **[kicad-happy](https://github.com/aklofas/kicad-happy)**（作者 aklofas）—— 面向 AI agent 的 KiCad skill；`check-pcb` 的分析套件（EMC / 热 / 生命周期审计 / 项目配置 / issue 导出）基于它构建。
- **[circuit-synth](https://github.com/circuit-synth/circuit-synth)** —— Python 源码驱动的 `.kicad_sch` 生成，`draw-schematic` 的引擎。
- **[kicad-sch-api](https://github.com/circuit-synth/kicad-sch-api)** —— 程序化原理图后处理（label 修正、PWR_FLAG 插入、BOM 校验）。
- **[easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py)** —— 把 LCSC / EasyEDA 元件转成 KiCad symbol / footprint / 3D 模型，供 `component-preparing` 用。
- **[KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools)**（作者 drandyhaas）—— 可选的自动布线阶段，含其 Rust 栅格布线器（MIT，源码已随仓库收进 `skills/draw-pcb/scripts/vendor/`）。
- **[jlcsearch](https://github.com/tscircuit/jlcsearch)**（作者 tscircuit）—— 公开的 LCSC 检索 API：JP 流水线的第三采购通道，CN 流水线的主通道（且免 key）。
- **[jlcparts](https://github.com/yaqwsx/jlcparts)**（作者 yaqwsx）—— 发布的 JLC 目录参数化数据分片；为 CN 流水线提供 jlcsearch 缺失类别（电感、磁珠）的离线参数化检索。
- **[Playwright](https://playwright.dev/)** —— 无头浏览器探测分销商页面和抓 datasheet。
- 以及本项目赖以运行的 Python 生态：NumPy、SciPy、Shapely、Matplotlib、Pillow、ReportLab、odfpy、python-docx、Jinja2、Requests、PyYAML、psutil、pytest。

如果本项目对你有帮助，也请考虑给这些上游项目点 star / 提供支持。

---

## 许可证

源码可见，采用 **[PolyForm Noncommercial License 1.0.0](LICENSE.md)**。
**非商业**用途（个人、研究、教育）可自由使用、修改、分享。**商业用途需另行授权** —— 请联系作者。© 2026 zhang zheng.

第三方组件（kicad-happy 派生的分析器、随仓库收录的 KiCadRoutingTools）仍遵循其原始 MIT 许可 —— 见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
