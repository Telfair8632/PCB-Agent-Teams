# Hard rules（脚本与 LLM 边界）

- **No subagents** — 即使 longlist 大也不要 spawn subagent；脚本 worker=2 已并行
- **No HTML scraping in buyable_gate** — 脚本只调 DigiKey REST API v4 / Mouser Search API / LCSC jlcsearch。无公开 API 的 vendor（akizuki / marutsu / chip1stop / rs_jp）在 URL build 时已过滤
- **No LLM classification** — availability / NRND / price / stock / package / ranking 全部读 API 字段，禁止文本匹配
- **Fail fast on schema drift** — API 返回结构变了就 `fetch_error: <reason>`，不要文字 paper-over
- **LLM cannot override hard gates** — Final LLM review 只能做 ok / blocked，不能把脚本判 fail 的拉回 pass

## spec 满足不了时：分类退回 circuit-design（回环的「另一半」）

某 role 在 envelope 内**全 fail / 无解**时，**不要自己硬改 spec 往下走**。按改动性质分三类（与 circuit-design 落盘的回退判据同源）：

- 🟢 **纯换等效件**（envelope 内换个 MPN，结构没变、spec 没松）→ 选品自己定，不回头
- 🟡 **要松安规 spec**（隔离 / 耐压 / CTI / 温度额定，哪怕结构没变）→ **停，AskUser + 回 circuit-design 让它 bless**，不在选品阶段擅自松
- 🔴 **要改结构**（加减级 / 换隔离方案 / 动电源域）→ **停，回 circuit-design 重开**（受影响 role 一起回退）

纯**性能** spec（Rds(on) 等）松动归 🟢，选品自己定。🟡/🔴 一律交回 circuit-design，不擅自决定。
