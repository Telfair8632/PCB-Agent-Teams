# 落盘格式（**简洁版**）

讨论完成后，**只更新真正变化的章节**到 `Projects/<name>/CLAUDE.md`：

## §3 — ASCII 拓扑（必填，简洁版）

```
[输入] → [保护] → [分压] → [锚点 IC] ─┤隔离边界├→ [滤波] → [MCU ADC]
   HV 域                                LV 域 (3.3V)
```

画到能看清"输入 / 处理 / 输出 / 隔离边界 / 电源域"即可，不必精雕。

## §5 — BOM + spec 冻结表（必填，三态）

> ⚠️ **本项目特例**：`Projects/<name>/CLAUDE.md`（某个**具体项目**的文件）是该项目「回路真相」唯一源，**任何阶段都要读它** → spec 直接进它的 §5（**覆盖**「compass 不放数据」惯例）。**仅限 `Projects/<name>/CLAUDE.md`**；工作区根 `PCB-Agent-Teams/CLAUDE.md` 照旧纯路由、不放数据。

| role | 元件角色 | 目标 spec（硬值 / 范围 / TBD:原因） | 已有件 MPN | 状态 |
|---|---|---|---|---|
| iso_amp | 隔离放大器 | Viso≥5kVrms; Vin=±2V; pkg≥SOIC; CMTI≥50kV/µs | <用户已有则填，否则 PENDING> | locked / spec_frozen |
| iso_dcdc | 隔离 DC-DC | Viso≥3kV(范围,选品落点); Vout=5V; P≥1W; SIP 通孔 | PENDING | spec_frozen |
| hv_div_top | HV 分压上臂 | 400V; CTI≥Group II; ±1%; P≥0.25W | PENDING | spec_frozen |
| <role_snake_case> | <角色名> | TBD: <为什么填不了 + 谁来解> | — | TBD |

**状态三态**：
- `locked` — 已有件，MPN 钉死（用户手头有 / 已选定）
- `spec_frozen` — spec 冻好，MPN 待选（→ Phase 2 component-selecting）
- `TBD` — spec 还缺，**必须带原因 + 归属**；该 role 在 TBD 解掉前不进 Phase 2（不挡其他 role）

**完整性判据（落盘放行条件）**：表里**没有裸空格**——每个 spec cell 是 {硬值 │ 范围/约束 │ TBD:原因} 三选一。能填的填，填不了的标 TBD 写清为什么，不许留白不解释。**范围/约束**别硬填精确点值（会鸡生蛋，留给市场落点）。

> role ID 用 snake_case 功能名（**禁** R1/U2/C1）。详细 MPN / 封装 / vendor URL / verdict 由 component-selecting + component-preparing 后续填，本 skill 只冻 spec + 锁已有件。

## §1 / §2 / §6 / §7 / §8

如 legacy 已有就**继承不动**。本 skill 仅在用户**明确改变方向**时才更新。
