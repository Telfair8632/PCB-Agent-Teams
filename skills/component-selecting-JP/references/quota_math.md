# 配额预算（quota math）

DK 1000/天，Mouser 1000/天，LCSC（jlcsearch / wmsc）无公布 quota（按 1.0s 间距防御性 throttle）。每候选打 1 DK + 1 Mouser + 1 LCSC = 3 calls/件，但只有 DK + Mouser 算 daily 配额。

| longlist N | DK/Mouser calls/会话 | × 10 会话/天 | 状态 |
|---|---|---|---|
| 10 | 10 | ≈ 100/天 | ✅ |
| 25 | 25 | ≈ 250/天 | ✅ |
| > 25 | 硬 fail | — | ❌ longlist 质量已退化 |

> N>25 限制原因不再是 daily quota，而是 longlist 质量。回 LLM 重新 filter。
