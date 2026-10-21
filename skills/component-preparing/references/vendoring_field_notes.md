# Phase 2.5 vendoring 实战笔记

> 这里写**遇到过什么 / 怎么解决的**。SKILL.md 是指南针，不写细节；细节落这里。
> 触发条件：Phase 2.5 verify_vendoring.py reject 任何 MPN 时，先来翻这个文件找已知 case。

---

## DK / SnapEDA / UL 抓 zip 的三层墙（按拒绝从轻到重）

```
┌──────────────────────────────────────────────────────────────┐
│ 想要：lib_external/incoming/<MPN>.zip （含真 .kicad_sym/.mod）│
└────────────────────────────┬─────────────────────────────────┘
                             ↓
       ┌─────────────────────────────────────────────┐
       │ Cloudflare 反爬（DK product detail 页）      │
       │ 表现：title="Just a moment..."               │
       │ 处理：换 /en/models/<internal_id> 端点       │
       │       （CF 在这条路径上松一些）              │
       │ 状态：✅ 可绕                                │
       └─────────────────────────────────────────────┘
                             ↓
       ┌─────────────────────────────────────────────┐
       │ DK "Choose Download Format" 模态异步加载    │
       │ 表现：模态打开 header 有 body innerHTML=""   │
       │ 根因：JS XHR 被 bot detection 静默 drop      │
       │ 处理：放弃 DK 直接下载路径，去 UL/SnapEDA 找 │
       │ 状态：❌ 拦不住                              │
       └─────────────────────────────────────────────┘
                             ↓
       ┌─────────────────────────────────────────────┐
       │ UL/SnapEDA login wall                        │
       │ 表现：UL "Download Now" 点击跳 /Account/Login│
       │       SnapEDA HTTP 403 直接 Cloudflare 挡    │
       │ 处理：                                       │
       │   A. user 一次性 UL/SnapEDA 注册 + 登录       │
       │      → main session 写入 cookie              │
       │      → headless 之后能下载                   │
       │   B. 不下 zip，**只读 UL 网页里的 pin map**  │
       │      → 同 pinout 的现有 lib 改名复用         │
       │ 状态：⚠ A 一次性手工，B 是当下务实办法      │
       └─────────────────────────────────────────────┘
```

**关键 takeaway**：Cloudflare + bot detection 让纯 headless 下载 DK 系 zip 接近不可能（2026 起）。**不要跟它硬碰硬**，直接走"读 UL DOM 拿 pin map"+"复用 lib_external 已有件"路径。

---

## 已验证可读的"DK EDA Models 真数据"端点

`https://www.digikey.jp/en/models/<DK_INTERNAL_ID>`
- DK_INTERNAL_ID = product URL 末尾的数字（不是 DK part number "<XXXX-ND>"）
- 例：TMA 0505S 的 DK_INTERNAL_ID = 9324924（来自 `.../detail/traco-power/TMA-0505S/9324924`）
- main session（不需要 dkjp）能正常渲染
- 页面 4 个 model 提供方：Manufacturer / Ultra Librarian / TraceParts / SnapMagic
- 每个提供方有 `<a class="btn-download-model">Select Download Format</a>` 按钮

`https://app.ultralibrarian.com/details/<GUID>/<MFG-with-dash>/<MPN-with-dash>?ref=digikey`
- 怎么拿 GUID：从 DK API `/products/v4/search/<DK_PART>/media` 返回的 EDA Models URL 里抓
- main session 加载页面后**整页 DOM 渲染完**
- **关键节点**（agent-browser eval 抓得到）：
  - `Symbol`/`Footprint`/`3D Model` 三段
  - 每个 pin 的 (number, name) 对——例如 TMA 0505S：
    ```
    Symbol
    1 +VIN
    2 -VIN
    4 -VOUT
    6 +VOUT
    ```
  - "Download Now" 按钮（无 UL 账号点了跳 login）

---

## 已知 case 索引

### Case 1: TMA 0505S（TRACO Power 1W iso DC-DC）

- 实测时间：2026-05
- 问题：Phase 2.5 想 vendor 真件 zip，三层墙全踩
- 解法：UL DOM 读到 pin map 1/2/4/6 与 XP IH0503SH 完全一致 → lib_external 复制
  IH0503SH 派生一个 `(symbol "TMA_0505S" (extends "IH0503SH") ...)` 块，footprint
  共用 `Converter_DCDC_XP_POWER-IHxxxxSH_THT`（同 SIP-7 pad-compatible）
- evidence library.status 升级到 `vendored_complete`，加 `vendoring_provenance`
  字段写明 UL 验证来源
- 不是 100% 真 vendoring（缺厂方原版 footprint 微观尺寸），但安全关键的 pin map
  是 UL 官方确认的——**焊接级别正确**

### Case 2: 100SP1T1B1M2QEH (E-Switch SPDT toggle)

- 同项目同时间
- 问题：选品时它有 stock + 价格 OK，但 KiCad 没有它的 1:1 lib，只有
  EG1271 的 footprint（不同子系列）
- 解法：直接换 MPN 到 **EG1271**（E-Switch 同厂另款）
  - JP buyable: stock=4242 active=2 ¥148（甚至比原选的便宜）
  - KiCad std 1:1：symbol `Switch:SW_SPDT` + footprint
    `Button_Switch_THT:SW_E-Switch_EG1271_SPDT`
- evidence library.status: `kicad_std_compatible` → `kicad_std`
- **lessons**：换件比硬要原 lib 简单时，先看候选有没有 KiCad std 真匹配的同档件

---

## 决策树（下次遇到 verify_vendoring reject 时按此走）

```
verify_vendoring reject MPN
   │
   ├─ Step 1：能不能找一个同档替代件，KiCad std 已有 1:1 lib？
   │   │
   │   ├─ 是 → 跑 component-selecting --mpn 验 active+stock+price
   │   │       OK → 改 .py value 字段 + 改 evidence JSON → 完
   │   │       不 OK → Step 2
   │   │
   │   └─ 否 → Step 2
   │
   ├─ Step 2：lib_external 已有"同 pinout 同封装"的 vendored 件？
   │   │（UL DOM / 厂家 datasheet 比对 pin map）
   │   │
   │   ├─ 是 → 在 components.kicad_sym 派生
   │   │       (symbol "<MPN>" (extends "<existing>") ...)，
   │   │       footprint 共用，evidence 加 vendoring_provenance
   │   │       记录 pin map 来源 → 完
   │   │
   │   └─ 否 → Step 3
   │
   ├─ Step 3：能不能让 user 一次性 UL 注册 + 浏览器登录建 session cookie？
   │   │（一次成本，之后所有项目受益）
   │   │
   │   ├─ 是 → user 登录后跑 vendor_mpn.py --auto-download → 完
   │   │
   │   └─ 否 → Step 4
   │
   └─ Step 4：fail-fast，告诉 user 这一步必须他出手（手工下载 zip
              到 lib_external/incoming/，再跑 vendor_mpn.py 就 OK）
```

---

## agent-browser 用法速记（main session）

```bash
# 打开 DK EDA Models 页（不需 dkjp session，main 即可）
agent-browser --session-name main open "https://www.digikey.jp/en/models/<DK_INTERNAL_ID>"

# 读 UL/SnapEDA 详情页 DOM 拿 pin map
agent-browser --session-name main open "<UL_URL>"
agent-browser --session-name main eval "document.body.innerText.substring(0, 2000)"
# Pin map 通常在 "Symbol" 节后面，格式 "1\n+VIN\n2\n-VIN\n..."

# 真 CDP 点击（用 eval setAttribute id 后再 click，比合成 click 稳）
agent-browser --session-name main eval "document.querySelectorAll('a.btn-download-model').forEach((a,i)=>a.id='dl-'+i)"
agent-browser --session-name main click "#dl-0"
```

**已知不工作**：
- `agent-browser download "<selector>"` 在 DK 页和 UL 页都返回 success 但文件不落盘——下载链路被 bot detection 默默 drop
- DK product detail 页（不是 /en/models/）用 dkjp-test session 会撞 Cloudflare "Just a moment..."
- SnapEDA 页 (snapeda.com/parts/...) 直接 HTTP 403

---

## 这个文件什么时候更新

- 每次 Phase 2.5 vendoring 遇到新坑且找到 workaround → 在"已知 case 索引"加一节
- 决策树有反例（某 step 走了发现行不通的 case）→ 在树下面加"反例"小节
- agent-browser 速记里某个命令突然失效 → 在"已知不工作"加一行 + 日期
