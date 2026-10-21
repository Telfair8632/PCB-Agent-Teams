# component-preparing 反模式全集

> SKILL.md 正文只留最高频的 2-3 条；其余下沉到这里。
> 触发条件：动手前对照本表自检；review 别人产出时逐条比对。

## 职责越界（重评 / 跳步）

- ❌ **重评 shortlist verdict / 跳过 user 确认**：verdict 来自 component-selecting，top pick 必须 user 拍板（正文已留）
- ❌ **跳过 ⓪ 适用性审查直接抓全 BOM**：spec 错位的料 vendoring 完才发现 → 浪费 quota + 误导 user 拍板；⓪ 报告必须先到 user 桌上（正文已留）
- ❌ **⓪ 审查只写「OK / pass」不带支撑句**：每条 verdict 必须引 datasheet 页码或项目 §5 行号，否则等于没审

## 资产获取 / vendoring

- ❌ **手工拷 .kicad_sym / .kicad_mod 进 lib_external**：必须走抓取脚本，保证 audit trail
- ❌ **用 KiCad std 凑近似 footprint 当 vendoring**：物理 footprint 名相同 ≠ pinout 相同（典型反例：isolated DC-DC 借用相似 SIP-N 封装，pin 顺序不同 → 焊不上）
- ❌ **跳过 verify_vendoring.py / 跳过 check_readiness.py / MPN swap 后不删旧 PDF**
- ❌ **抓完不更新 docs/bom.md / 不写 evidence JSON / 不写 sentinel**

## 命名 / 编号

- ❌ **BOM role 引用 R1 / U2 / C1 电气编号**：docs/bom.md / sentinel JSON `role` 字段 / change log 一律 snake_case 功能名（`iso_amp` / `iso_dcdc` / `hv_terminal`），与项目 CLAUDE.md §5 ID 列同源。电气编号是 Phase 3 sch 阶段才有，BOM 阶段不允许出现。

## 同族换值（family-swap）

- ❌ **同族换值禁用 bare sed / Edit cross-file**：必须走 `swap_family_mpn.py`（正文已留）。
  - 理由：手改容易漏 .net / shortlist 内部 raw_parameters 等连带字段，evidence 跟 .py value 不一致 → sentinel 静默放行 → 物理板按错值贴片。
  - 该脚本会做 family check + atomic 跨文件替换 + shortlist 加 `_v5_revision`（不动 results[]）+ 自动 clean orphan PDF + 重跑 sentinel。
