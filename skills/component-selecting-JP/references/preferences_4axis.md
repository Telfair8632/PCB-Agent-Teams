# Phase 0 用户偏好（4 轴 · session sticky · 用于推荐不用于过滤）

session 第一颗 role 跑前用 `AskUserQuestion` 一次问完 4 轴；后续 role 不再重问
（除非用户主动改）。**单颗或多颗都要问**——单颗也需要这把尺子来推荐。

## 偏好的唯一用途

**偏好只在 Final LLM review 阶段起作用，不参与 Discovery 抽 MPN、不参与脚本 verdict**。

```
        Discovery（按技术 spec 中立抽 MPN，不看偏好）
                       ▼
        脚本（3 lane × N MPN 完整跑，不看偏好）
                       ▼
        shortlist 落盘（中立、字段完整）
                       ▼
   ★ Final LLM review ★（**这一步**用 4 轴）
        ├─ 套偏好对 pass 候选排序
        ├─ 推 top pick + 候补 1–2 颗
        └─ AskUser 时一句话引用偏好理由
```

中立流程的好处：user 想临时换偏好（比如"再帮我看下严控本视角下哪个最便宜"），
LLM 拿同一份 shortlist 重新排序就行，不用重跑脚本 / 重 quota。

## 4 轴 + 影响

| 轴 | 选项 | 默认 fallback | 在 Final review 怎么用 |
|---|---|---|---|
| 渠道偏好 | (a) 日本仓快速 DK JP / Mouser JP（3-5 天）<br>(b) LCSC 拼 JLCPCB 便宜单（10-14 天，省 5-10×）<br>(c) 自动选最便宜 lane | (c) 自动最便宜 | 把命中 lane 的 active 候选往前排；解释 top pick 时引用 |
| 品牌偏好 | (a) 日系优先（Murata/Panasonic/KOA/Rohm/Toshiba）<br>(b) 欧美（Vishay/TI/Yageo/Bourns）<br>(c) 都行 | (c) 都行 | 命中品牌的优先；非命中作候补 |
| 价格 vs 库存 | (a) 严控本<br>(b) 平衡<br>(c) 稳定优先（库存深 + 品牌大） | (b) 平衡 | 严控本看单价，稳定优先看库存深度 × 品牌大小 |
| 黑名单 | 用户已知不能用的 MPN（实测焊不上 / 不兼容） | 读 USER.md / 项目 CLAUDE.md | 直接从推荐里剔除（不是从 shortlist 剔除） |

## 问完立刻持久化（AskUserQuestion 拿到 4 个答案后）

```bash
.venv/bin/python skills/component-selecting-JP/scripts/record_preferences.py \
  --project <name> --channel <code> --brand <code> --price-vs-stock <code> \
  [--blacklist MPN1,MPN2]
```

code key：
- 渠道 channel：`jp_domestic_fast` / `lcsc_jlcpcb` / `auto_cheapest`
- 品牌 brand：`japan_first` / `western` / `any`
- 价格 vs 库存 price-vs-stock：`tight` / `balanced` / `stable_first`
- 黑名单 blacklist：`--blacklist MPN1,MPN2,...`（可空）

写到 `Projects/<name>/_artifacts/component_selecting/user_preferences.json`。
**Phase 5 release 直接读这份文件驱动 ORDER_GUIDE 推荐**——不写盘 release 阶段会
fail-fast 让你回来重问，浪费 round-trip。

## ❌ 禁止

- 不要用偏好 filter Discovery 抽 MPN——抽样按技术 spec（电压/电流/封装/隔离档）中立
- 不要用偏好 override 脚本 verdict（pass 的不能因为偏好不命中变 fail，反之亦然）
- 不要"用户已明示渠道就跳过 4 轴问询"——剩下 3 轴还得问

## Why this gate exists

用户原话："我使用四轴去问他这个偏好，我觉得是个 OK 的。"
——用户认可 4 轴问询本身，关键是要把偏好用在**推荐时**而不是**过滤时**。
之前我把偏好当 Discovery filter（"日系优先就只抽日系"），是错的设计；
正确做法是脚本永远中立、偏好只在 LLM 给 user 推 top pick 时上场。
