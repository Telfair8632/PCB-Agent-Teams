# 输出契约（output contract）

本 skill 有**两个**输出通道，职责严格分开。

## 1. stdout `--summary` —— 给真人看的硬代码渲染

`summary_lines()` / `_render_evaluation_summary()` 在 `scripts/component_select.py` 里把候选信息**硬代码**渲染成 user-ready 文本。LLM 调完脚本后**直接把 stdout 复制给用户即可**，不需要再读 JSON 拼字符串。

格式（已在脚本里 hardcoded，下面只是参照说明）：

```
=== Component Selecting / locale=日本 currency=JPY ===
fx: 22.935 JPY/CNY (cache)

| # | MPN | 厂家 · spec · 封装 | 🇯🇵 DK_JP (¥/在库) | 🇯🇵 Mouser_JP | 🇨🇳 LCSC (CNY ≈ JPY / 在库) | lib |
|---|---|---|---|---|---|:-:|
| 1 | AMS1117-3.3 | EVVO · vout=3.3V current=1A · TO-261 | ¥50 (9.6k) | — | ¥0.04 CNY (≈¥0.8 JPY, 2.1M) | ✅ |
| 2 | NCP1117ST33T3G | onsemi · vout=3.3V current=1A · TO-261 | ¥100 (53k) | ¥94 (55k) | ¥0.21 CNY (≈¥4.9 JPY, 14k) | ✅ |
| 3 | MIC5219-3.3YM5 | Microchip · vout=3.3V current=500mA · SOT-753 | ¥198 (63k) | ¥194 (19k) | ¥0.54 CNY (≈¥12 JPY, 14k) | ✅ |

注：购买 URL / datasheet / fail 候选完整数据 → JSON。LCSC lane 走 JLCPCB 拼单（PCB+元件同 DHL）。
json: /tmp/<...>.json
llm_review: only block / rerun if top pick contradicts project constraints
```

约定（**展示哲学：只显示搜到、且能用的**）：
- **fail 候选隐藏**。`verdict=fail`（library 缺 / 全 lane no stock 等）的整条候选不出现在 stdout——user 只想看 actionable 选项。fail 数据完整保留在 JSON 供 audit。
- **per-lane 单元格**：只有 `status ∈ {active, nrnd}` 才显示价格+库存；其他状态（no_match / fetch_error / skipped / pending）渲染为 `—`。
- **URL 不进 stdout**：购买 URL 和 datasheet URL 都在 JSON 里，stdout 表格不要 URL（视觉混乱、用户问的时候再给）。
- **库存表示**：`(9.6k)` / `(2.1M)` 缩写，零库存写 `(缺货)`。
- **LCSC 单元格**：`¥CNY (≈¥JPY, 库存)` 三段式；`fx_source=fallback` 时加 ⚠fx 标记。
- **`nrnd` 例外**：库存存在但 EOL 是真实可用警告，单元格末尾加 ⚠NRND。
- **warn_single_source**：MPN 后加 ⚠（如"AP2114H-3.3TRG1 ⚠"），让用户多看一眼但不藏候选。
- **`lcsc_only_active`** 候选额外加一行「⚠ <MPN>：本土仓未收录，仅 LCSC 有现货」，明确该 MPN 走 JLCPCB 拼单 lane。
- 全部 fail 时显示「✗ 没有任何候选通过 verdict —— 请回 longlist 重 spec 或考虑替代型号」并提示 fail 详情见 JSON。
- Phase 1 库存短路已移除：library 缺失不再阻止 vendor API 调用，library 状态独立进入 verdict（library 缺 → verdict=fail，但 vendor 数据完整出现在 JSON）。
- **`lib` 列只显示 ✅ / ❌（2 态）**：✅ = 任一来源能稳定抓到（已在 lib_external / KiCad std / LCSC 有 C-number / DigiKey models 页 / cache 近似可 vendor）；❌ = 所有来源都没有（真正无法 sch 用）。**🔧 中间态从 stdout 去掉** —— 用户视角只关心"能不能用"，"现在还需不需要抓"是 Phase 2.5 内部状态，留在 JSON `library.status` 字段供 component-preparing 决定从哪个源抓。`_LIBRARY_GLYPH` 内 7-class → 2 态映射在 `scripts/component_select.py`。

LLM **不需要**也**不应该**改这套格式——直接 cat 给 user。LLM 唯一可以加的：
1. ⚠ 性价比标记（在某行末尾加一句 LLM 判断"该让 user 多看一眼"的理由）
2. `llm_review` 阻断：top pick 跟项目约束矛盾时让 user 复核

## 2. JSON `--output <path>` —— 给下游 skill 的稳定字段契约

下游 skill（`bom-readiness` / `component-preparing` / 用户自己的脚本）**必须读 JSON，不要 grep stdout**。stdout 格式允许演化，JSON keys 不会随便改。

每个候选（`results[i]`）保证带的字段：

```
mpn                    str          MPN
verdict                str          pass | warn_single_source | fail | ...
expected_role          str
locale, currency       str
local_jp_active        bool         有任意 jp_domestic vendor active
lcsc_only_active       bool         只有 LCSC active，本土全无
local_price            float|null   三条 lane 中最低价（按各自 currency）
local_stock            int|null     三条 lane 中最大库存
key_parameters         dict         按 role profile 抽出的电气参数
buyable_gate           dict         {status, ...}
library                dict         {status, ...}
solderability_gate     dict         {status, ...}
product_urls           dict         vendor_id → product_url
datasheet_urls         dict         vendor_id → datasheet_url
vendor_results         list         详细 lane 数据（见下）
```

`vendor_results[i]` 字段（每个 lane 一条）：

```
vendor_id              str          digikey_jp / mouser_jp / lcsc / ...
lane                   str          jp_domestic | jlcpcb_consolidated |
                                    intl_direct | unknown
status                 str          active | nrnd | no_match | fetch_error |
                                    skipped_after_pass | pending
stock                  int|null
price                  float|null   原始货币（DK/Mouser=JPY, LCSC=CNY）
currency               str
final_url              str          vendor 产品详情页 URL
datasheet_url          str|null
manufacturer           str|null
matched_mpn            str|null
package                str|null

# LCSC 专属字段
price_jpy_estimated    float|null   CNY 价 × fx_rate 后的 JPY 估算
fx_rate                float|null   JPY per CNY
fx_source              str|null     frankfurter | cache | fallback
```

下游 skill 推荐用 `lane` 字段而不是 `vendor_id` 列表来过滤买源。例如：

```python
# 拿 jp_domestic 的所有 active 买源
[v for v in c["vendor_results"]
 if v.get("lane") == "jp_domestic" and v.get("status") == "active"]
```

## 不要包含

stdout 不要包含：phase 计数 / verdict_reasons 列表 / rejected 列表 / throttle 状态 / 泛话提醒。

用户主动问"为什么"时去 JSON 翻 reason 字段拿具体证据。
