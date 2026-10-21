# Discovery 工作流（LLM 跑 WebSearch + Tavily，不在脚本内）

"广撒网搜 MPN"这一步留给 LLM，因为：
- WebSearch + Tavily（带 country=日本 boost）能拉到 DigiKey 关键词搜索覆盖不到的本土厂（TDK-Lambda / Cosel / Murata Power 等）
- LLM 抽 MPN 比 vendor 关键词组合更灵活
- 把搜索时间和 token 花在脚本之外，不影响 buyable_gate 本身的速度

## LLM 该做的

1. 读用户需求 → spec（input/output voltage、isolation、package、stock 阈值等）
2. 并发跑 `Tavily(country=日本)` + `WebSearch`，从 catalog/datasheet/选型 PDF 抽 MPN
3. **去重 + 按相关性 filter，输出 JSON longlist**（**软上限 10 / 硬上限 25**）
4. 喂给脚本走 `--longlist <path>` verify

⚠️ **不要超过 25 个候选**——脚本会硬 fail 让你回头 filter（见 quota_math.md）。

## Discovery 单 query 模板

一句话覆盖所有候选 family，不要拆多次：

```
"<MPN_family_A> <MPN_family_B> <function> <key_spec>"
例: AMC1311 ACPL-C87 isolated voltage amplifier 5kVrms 2V input
例: TMA0505S B0505S NME0505S isolated DC-DC 5V 1W SIP through-hole
```

目标 10 颗 MPN，按技术 spec **中立采样**——日系 / 欧美 / 国产都拉一些。
偏好不参与 Discovery（中立采样 → user 临时换偏好不用重跑脚本）。

## 内置 `--discover`：关键件弃用，被动件可用

- **关键件（IC / 模块）**：`--discover` 的 DK keyword search 覆盖窄，DK quota 一死就只剩 2-3 个 LCSC 候选。**优先用 LLM discovery + `--longlist`**，`--discover` 仅作离线兜底。
- **被动件（R/C/L/磁珠）**：免 key 参数化车道可用——`--discover --role resistor --param resistance=1000 --param package=0603`（jlcsearch 类型化端点）；电感/磁珠自动落 jlcparts 分片。LCSC 料经 JLCPCB 拼单顺路到日本，产物是 longlist，仍需回 `--longlist` 过三车道 buyable 验证。
