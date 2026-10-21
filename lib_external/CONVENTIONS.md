# lib_external/ 使用规范（single source of truth）

工作区共享元件库的所有规则。SKILL.md / workspace CLAUDE.md 不重复，只指针。

## 目录结构

```
lib_external/
├── README.md             ← 元件清单（每次 vendor 必更新）
├── CONVENTIONS.md        ← 本文件（规则）
├── components.kicad_sym  ← 所有 vendored symbol 都进单一文件
├── components.pretty/    ← 所有 vendored footprint（.kicad_mod）
├── components.3dshapes/  ← 3D 模型（.step + .wrl）
└── datasheets/           ← 跨项目共享 datasheet（fallback 池）
```

> 除本文件外，`lib_external/` 全是本地工作数据，**不入 git**（见根 `.gitignore`）。
> 新 clone 下来这里只有 CONVENTIONS.md —— `README.md` 和各 components.* 由首次 vendor 时自动生成。

## 1. 命名空间：单库约定

**所有 vendored 元件统一进 `components` 库**，不分多个子库（不做 `mcu/`、`power/`、`sensor/`）。

- 理由：单库扫描快、跟 KiCad 标准库（`Device:` `Connector_Generic:` `Package_SO:` …）天然 namespace 隔离
- `.py` 引用形如 `symbol="components:AMC1311DWV"` / `footprint="components:SOIC-8_..."`
- KiCad 标准库已有的元件（R/C/L/D、standard connectors、common opamps）**不要 vendor**，直接用标准库
- 例外：用户明确要替换标准 symbol（pin 顺序不对、缺 datasheet 字段）才 vendor 同名

## 2. 写入：component-preparing 是唯一入口（按 locale 路由）

写入 `lib_external/` 由 **`component-preparing`** 拥有。component-selecting 只出 shortlist，
不动磁盘。Phase 2.5 在 user 拍板 top pick 后，**按 USER.md §0 locale 路由**到对应抓取路径：

| 路径 | 触发（USER.md §0 locale） | 抓取脚本 | 写入方式 |
|---|---|---|---|
| **A. DigiKey EDA Models browser ZIP** | 日本（当前唯一已实现） | `agent-browser` (session=`dkjp`) → `lib_external/incoming/<MPN>.zip`；component-preparing 调用 import 解析合并 | 解 ZIP → 写 `components.kicad_sym` + `components.pretty/` + `components.3dshapes/` |
| **B. UL/SnapEDA fallback** | 任何 locale，DigiKey EDA Models index 引用时 | 同上路径，URL 来自 DigiKey `/media` | 同上 |
| **C. LCSC + EasyEDA** | 中国大陆（待建：`component-selecting-CN` + component-preparing 对应抓取分支） | `draw-schematic/scripts/download_lcsc_lib.py`（须带 `COMPONENT_SELECTING_LCSC_WRITE=1` 或 component-preparing 显式启用 write） | 直接写同三处 |

所有路径最后都流入同一份 canonical lib（symbol idempotency guard 在 `_append_symbol_block`）。

**唯一 writer**：`component-preparing`（在 user 确认 shortlist top pick 之后）。

**只读 caller**：
- `component-selecting-<locale>/scripts/library_probe.py`：扫 lib_external + lib_cache 给 7 档 library_status（不写）
- `bom-readiness/check_readiness.py`：复检 symbol/footprint 索引 + Phase 2.5 写入的 evidence JSON
- `draw-schematic`：消费 bom-readiness sentinel，再用 `verify_footprints.py` 做命名校验

**禁止**：
- 手工拷 `.kicad_sym` / `.kicad_mod` 进来 — symbol pin 顺序必须可追溯回 vendoring + datasheet 一一对照
- 手编辑 `components.kicad_sym` 文件内部块 — 下次 vendor 同 MPN 会被覆盖
- 在任何 skill / 脚本里硬编码 LCSC 或 Ultra Librarian 作为唯一抓取路径 — locale routing 决定，配置写在 `skills/component-selecting-<locale>/scripts/locale_mapping.yaml`

## 3. 冲突解决

| 冲突类型 | 处理 |
|---------|------|
| `components:Foo` vs `Connector_Generic:Foo` | 不冲突（不同库前缀），KiCad 按完整 `Lib:Name` 唯一定位 |
| 同一 MPN 二次 vendor | easyeda2kicad **无声覆盖**原文件。vendor 后必须 `git diff components.kicad_sym` 确认改动符合预期 |
| 上游 vendor/EDA 改了 symbol pin 顺序 | git diff 抓出来；旧项目 .py 引用同名但 pin 含义可能换了 → 必须人工对照 datasheet 重新校验 |

## 4. 读取：索引建立

| 工具 | 索引 | 调用方 |
|------|------|-------|
| `verify_footprints.build_footprint_index()` | KiCad 标准 + lib_external 所有 `*.pretty/*.kicad_mod` | bom-readiness、draw-schematic |
| `check_readiness.build_symbol_index()` | KiCad 标准 + lib_external 所有 `*.kicad_sym` 顶层 `(symbol "...")` | bom-readiness |

**索引坑**：
- KiCad 标准 `.kicad_sym` 用 tab 缩进；easyeda2kicad 写 2-space 缩进 → regex 必须吃两种 (`^[ \t]+\(symbol`)
- 子符号名带 `_<digit>_<digit>` 后缀（unit-style）→ 按名字 filter 掉，不当顶层

## 5. README.md 元件清单（必维护）

`lib_external/README.md` 的"元件清单"表是 **ground truth**。每次 vendor 完必须更新，列：
`MPN | LCSC ID | Pin 数 | 下载日期 | 备注`

vendor 改 MPN（如 BOM 错把 1714984 当 MKDS 5/2-7.5，实测下来是 1868076）→ "备注"列写明原 BOM 错在哪。

## 6. 清理（GC）

**不会自动 GC** vendored 元件 —— 一个项目不用了，可能其他项目用。

确定全工作区不再用某 MPN：
1. 从 `README.md` 元件清单删那行
2. 手编 `components.kicad_sym` 删对应顶层 `(symbol "MPN" ... )` 块（含子单元 `_0_1`）
3. 删 `components.pretty/<MPN>*.kicad_mod`
4. 删 `components.3dshapes/<MPN>*.{step,wrl}`
5. `git diff` 验证只动了对的元件

**评测/试错期间 vendor 的孤儿**：等评测结束一次性清。中间不要清，会打断 audit trail。

## 7. datasheets/ fallback

`lib_external/datasheets/` 是**跨项目共享 datasheet** 的 fallback 池：

```
查找顺序：
  Projects/<name>/datasheets/<MPN>*.pdf      ← 优先（项目专属）
  ↓ 找不到
  lib_external/datasheets/<MPN>*.pdf         ← fallback（跨项目共享）
  ↓ 还没有
  component-preparing 抓到 Projects/<name>/datasheets/
```

命名约定：文件名必须包含 MPN；LCSC 路径通常是 `<MPN>_<LCSC_ID>_datasheet.pdf`，
manufacturer/UL 路径可用 `<MPN>_manufacturer_datasheet.pdf`。

实现入口是 `component-preparing`（调用 `sourcing/scripts/fetch_datasheet_*.py`）。
bom-readiness/draw-schematic 只复检文件和 evidence。

## 8. 3D model 路径（已知坑）

easyeda2kicad 写**绝对路径**进 `.kicad_mod` 的 `(model ...)` 字段，路径形如 `lib_external/components.3dshapes/<MPN>.step`。

**项目搬家就断链**（3D 视图空白；不影响 PCB 走线/Gerber）。

修法（不改 vendoring 工具）：
1. KiCad GUI → Preferences → Configure Paths → 加环境变量 `KICAD_USER_3DMODEL_DIR = lib_external/components.3dshapes`
2. 搬家后只改这个变量，不动 .kicad_mod 文件

或者：以后改 vendoring 工具，下载完 sed `.kicad_mod` 把绝对路径换成 `${KICAD_USER_3DMODEL_DIR}/<MPN>.step`（待办，不紧急）。

## 9. Git 跟踪策略

**当前**：lib_external 整体随 git 走（无 .gitignore）。

**风险**：
- `components.3dshapes/*.step` 单文件 ~MB 级，多个项目积累后仓库变大
- `components.kicad_sym` 同一文件多人改 → merge conflict 难手解

**建议**（待用户决策）：
- 短期：保持现状；评测期 vendoring 出来的元件随项目入仓便于回溯
- 长期：如果跨多机器协作 → `*.step` / `*.wrl` 进 Git LFS；`components.kicad_sym` 按字母分文件减少 conflict 域

## 10. 一次性 dkjp session 配置（locale=日本 才需要）

如果 USER.md §0 = 日本，`component-selecting-JP/scripts/library_probe.py` 探查 EDA Models 索引
和 `component-preparing` 抓 ZIP 都会通过 agent-browser 的持久 session `dkjp` 访问 DigiKey JP
`https://www.digikey.jp/en/models/<dk_part_id>` —— 该页面有反爬 + DigiKey 登录墙。
**首次必须在 headed 模式手动登录一次**，session cookie 落盘后 headless 才能复用。

```bash
# 1) headed 打开登录页（人脸 / MFA 都自己点完）
agent-browser --session-name dkjp --headed open https://www.digikey.jp/MyDigiKey/Login
agent-browser --session-name dkjp wait --load networkidle
agent-browser --session-name dkjp close   # session 自动写入 ~/.agent-browser/sessions/dkjp*

# 2) 验证 session 落盘
ls -la ~/.agent-browser/sessions/dkjp*

# 3) 跑 self-check（应看到 "agent-browser dkjp session exists ✅" + 全部 PASS）
python3 skills/component-selecting-JP/scripts/component_select.py --self-check

# 4) 可选：加密 session（cookie 含登录态，相当于一份长效凭证）
export AGENT_BROWSER_ENCRYPTION_KEY=$(openssl rand -hex 32)   # 加进 ~/.zshrc
```

**session 失效检测**：probe 页面 body 含 `Just a moment` / `cloudflare` 字样时，
`library_probe` 标 `library_status=unknown`，error=`cloudflare_or_session_expired`，
不 fail-hard；提示用户重跑上面 4 步。

**安全提示**：dkjp session 文件**不入 git**（`.gitignore` 已含 `~/` 路径，但脚本不读 cookie 内容，只检测存在）。

## 11. External library cache strategy

`lib_cache/sources/` 是工作区级**只读** pre-filter 检索池，**不是项目依赖**：

- 路径：`<workspace>/lib_cache/sources/{kicad-symbols, kicad-footprints, kicad-packages3D, jlcpcb-kicad-library, digikey-kicad-library}`（按需新增）
- `component-selecting-<locale>/scripts/library_probe.py` 扫这里给候选打 7 档 library status；`component-preparing` 才会决定要不要 vendor 进 `lib_external/`
- 项目 `.kicad_sch` / `.kicad_pcb` / `.py` **不允许**引用 `lib_cache/...` 路径或 nickname。需要时 vendor 进 `lib_external/components`，或引用官方 KiCad 库 nickname
- 允许 stale、允许 partial、允许浅 / sparse clone；缺失时 library_probe 自动降级，不 fail

更新（不影响 lib_external）：

```bash
for repo in lib_cache/sources/*; do
  [ -d "$repo/.git" ] || continue
  git -C "$repo" pull --ff-only
done
```

7 档 library 状态跟外部 cache 的对应关系详见对应 locale skill 的 `references/`（如 `component-selecting-JP/references/`）。

**禁止**：
- 把整个上游库 vendor 进 `lib_external/`（只 vendor 用户拍板的最终 MPN）
- 在 `lib_cache/` 里手编 / 自动写文件（git pull --ff-only 之外的写都禁）
- 选品阶段对 `lib_external` 做 GC（只在确认 NRND/淘汰后置 `gc_recommended=true` 标记）

## 12. 给 LLM 的速查

| 问题 | 答案 |
|------|------|
| 我要新加一个元件 | 流程：`circuit-design` Phase 1 锁定 → `component-selecting-<locale>` 出 shortlist → `component-preparing` 抓 datasheet/library/evidence → `bom-readiness` 复检 |
| .py 怎么引用 | `symbol="components:<MPN>"` / `footprint="components:<vendored_name>"` |
| symbol 名我不记得 | `grep '(symbol "' lib_external/components.kicad_sym` 拿顶层名字 |
| vendor 完没生效 | 重建索引（脚本会自动），别忘了改 .py 字段对齐 |
| 旧项目 3D 视图空白 | KiCad 设 KICAD_USER_3DMODEL_DIR 环境变量 |
| 同 MPN 别人也 vendor 过 | git log lib_external/components.kicad_sym 看历史 |
