# DigiKey & Mouser API 速率限制（官方 + 第三方交叉验证，2026-05 核对）

## DigiKey API v4

来源：[Shared Concepts | API Developer Portal](https://developer.digikey.com/tutorials-and-resources/shared-concepts) ·
[FAQ - API Response and Error Codes](https://developer.digikey.com/faq/api-response-unexpected-response-and-error-codes) ·
[TechForum: API limit increase request](https://forum.digikey.com/t/required-api-limit-to-be-increased/67924)

| 维度 | Standard tier 限制 |
|---|---|
| 每分钟（burst） | **120 / minute** |
| 每天（daily quota） | **1000 / day** |
| Product Information API（KeywordSearch 走这个）| 同上 |
| 其他产品（Create BOM / Ordering / Quoting）| 10/分钟 |

### 错误响应区分（关键）

**429 + `"BurstLimit exceeded"`** — 每分钟限制：
- Header：`Retry-After`, `X-BurstLimit-Limit`, `X-BurstLimit-Remaining`, `X-BurstLimit-Reset`
- 处理：等 `Retry-After` 秒后重试

**429 + `"Daily Ratelimit exceeded"`** — 每天配额耗尽：
- Header：`Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`
- 处理：**不要重试**——等到 `Retry-After` 或日级 reset（GMT 午夜附近）

### 每个响应都带的状态 header

| Header | 含义 |
|---|---|
| `X-RateLimit-Limit` | 每天最大请求数 |
| `X-RateLimit-Remaining` | 当前剩余 |
| `X-BurstLimit-Limit` | 每分钟最大 |
| `X-BurstLimit-Remaining` | 当前分钟剩余 |

### 提高配额

填 [DigiKey API ticket](https://developer.digikey.com/) 申请 commercial / Marketplace tier。

## Mouser Search API

来源：[mouser.com/api-search](https://www.mouser.com/api-search/)（直接访问被反爬挡，用第三方文档交叉验证）·
[go-mouser library docs](https://pkg.go.dev/github.com/PatrickWalther/go-mouser) — 第三方但准确归纳了官方 spec

| 维度 | 限制 |
|---|---|
| 每分钟 | **30 / minute** |
| 每天 | **1000 / day** |
| 错误码 | 429（也观察到 503 用于限流） |
| Header | `Retry-After` |

## LCSC（jlcsearch.tscircuit.com + wmsc.lcsc.com）

无公布的 rate limit。jlcsearch 是社区维护的免费服务，wmsc 是 LCSC CDN。
我们防御性地按 1.0s 间距（≈60/min）throttle，避免被任一端 ban。

| 维度 | 限制 |
|---|---|
| 每分钟 | 未公布；自我约束 60/min |
| 每天 | 未公布 |
| 错误码 | 通常 503 / timeout（社区 endpoint 抖动） |
| Header | 一般无 `Retry-After` |

LCSC lane 的 `fetch_error` 不影响 DK/Mouser 的 verdict，shortlist 会照常出，
只是该候选少一个买源信号。

## 我们的 throttle 实现（_dk_throttle.py）

| 维度 | 配置 | 安全余量 |
|---|---|---|
| DigiKey 间距 | 600ms（≈ 100/min） | 17% |
| Mouser 间距 | 2200ms（≈ 27/min） | 9% |
| jlcsearch.tscircuit.com 间距 | 1000ms（≈ 60/min） | 防御性，无官方 quota |
| wmsc.lcsc.com 间距 | 1000ms（≈ 60/min） | 防御性，无官方 quota |
| 重试时延 | 3s / 6s / 12s 指数退避 | — |
| 跨进程协调 | fcntl flock + `/tmp/api_throttle.json` | 多 sub-agent 并行安全 |
| Daily quota 检测 | 命中 `Daily Ratelimit exceeded` body 立即 fail-fast，写 dead-until 时间 | 不浪费配额重试（仅 DK 用）|
| Burst limit 检测 | 命中 `BurstLimit exceeded` 按 Retry-After 退避 | — |

## 测试方法

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'skills/component-selecting-JP/scripts')
from _dk_throttle import reset_state
reset_state()
"
# 起 N 个并行进程，看 /tmp/api_throttle.json 能否被共同遵守
```

## 历史教训（2026-05-02）

3 个 sub-agent 并行 dispatch + 各进程独立 throttle = 进程间无协调，combined 速率 ~250/min，撞穿 DK 120/min 上限，2 小时累积 ~300+ HTTP 请求耗尽 daily 1000 配额。修复方向：跨进程文件锁 throttle（已实现）+ 日级 quota 命中后立即 fail-fast（已实现）。
