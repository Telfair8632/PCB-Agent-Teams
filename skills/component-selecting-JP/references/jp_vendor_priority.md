# 日本所属地选品铁律（迁自工作区 CLAUDE.md）

适用 component-selecting-JP / component-preparing。所属地见 `USER.md §0`。

## 仓优先级 source of truth

`skills/component-selecting-JP/scripts/locale_mapping.yaml` 是唯一权威。所有 skill 按 USER.md §0 → 该 locale 的 `vendors_priority` 顺序读。改优先级 / 加 vendor 只改 yaml。

## 铁律

1. **所属地优先**：选元件先看日本本土能不能买到，再看技术参数。DigiKey JP 库存极少 / NRND / 只有 LCSC 中国库 → 主动提替代型号（保持 pin/footprint/电气参数兼容）。
2. **仓集中**：单板 BOM ≤ 3 个仓。能在秋月+Marutsu 凑齐就不开 DigiKey 单。
3. **替代提示责任**：component-preparing 给元件建议时必须注明日本本土仓的可买性 + 替代型号，不能只给 LCSC 库存。
4. **PCB 打样 + 元件拼单**：JLCPCB（5 枚 + DHL 4–5 天到日本，~$30）。
   因为 JLCPCB 在中国仓发货，**LCSC 元件可以和 PCB 一起拼单**——
   这是日本场景下一条**常规** lane，不是次选。
   `component-selecting-JP` 把 DK_JP / Mouser_JP / LCSC 当成三条**平等并列**
   的买源 lane：
   - 三条永远都查（不会因为前两条 active 就跳过 LCSC）
   - 三条永远都展示给 user
   - user 根据当前订单偏好（"和 PCB 一起做" vs "急着拿到件"）自己挑 lane
   `local_jp_active` / `lcsc_only_active` 是诊断标志（让 user 一眼看出本土
   是否有现货），不是 gate 条件，不影响 verdict。
5. **Library 抓取按 locale 路由**：组件 vendoring（symbol/footprint/3D → `lib_external/`）由 locale-routed dispatcher 决定。配置写在 `locale_mapping.yaml.locales.<L>.library_fetch_strategy`（中国大陆 → `lcsc_easyeda`；日本 → `digikey_jp_browser`，需一次性 headed dkjp session 见 `lib_external/CONVENTIONS.md §11`；其它 → `fallback_chain`）。**禁止**在任何 skill / 脚本里硬编码 LCSC 或 Ultra Librarian 作为唯一抓取路径。

## 常见日本本土替代库（持续补充）

| 海外 | 日本本土替代 | 仓 |
|---|---|---|
| Mornsun 隔离 DC-DC | Murata MGJ 系列 / Recom REC 系列 | Marutsu / DigiKey JP |
| E-Switch EG 拨杆 | 秋月 SS-12D00 系列（pin 兼容） | 秋月 |
| 高耐压 NP0 电容 | Murata GRM 系列 | Marutsu |
| AEC-Q200 高阻值高耐压电阻 | Vishay CRMA 系列 | Marutsu / DigiKey JP |
