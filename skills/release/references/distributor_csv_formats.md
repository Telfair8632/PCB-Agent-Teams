# Distributor BOM 上传格式 — 列名规范

各 distributor 自家网站接受的 BOM 上传 CSV 列名清单。`distributor_csv.py` 按这份规范产出。

## DigiKey BOM Manager
**上传入口**：https://www.digikey.com/en/mylists/list/bulk-add（JP 镜像 .jp 同样接受）
**格式来源**：DigiKey 帮助 — "BOM Manager Upload"（manually verified 2026-05）
**列名（必填）**：
- `Manufacturer Part Number`
- `Quantity`
- `Customer Reference`（可空但保留列头）

DK 接受多余列；只匹配 MPN 列。

## Mouser BOM Tool
**上传入口**：https://www.mouser.jp/Bom/
**格式来源**：Mouser BOM Tool 文档（manually verified 2026-05）
**列名（必填）**：
- `Mfr Part Number`
- `Quantity`
- `Description`（推荐填，便于人工校对）

## LCSC BOM
**上传入口**：https://www.lcsc.com/bom（中文主站 / 国际站同接口）
**格式来源**：LCSC BOM 模板 v3（manually verified 2026-05）
**列名**：
- `Comment`
- `Designator`
- `Footprint`
- `LCSC Part #`（可空 — 用户填则 LCSC 优先匹配 LCSC#，否则按 MPN）
- `Manufacture Part Number`
- `Quantity`

## 维护

格式变了就改 `distributor_csv.py` 里对应的 transform 函数 + 同步本文件。
列名拼写错误是 distributor 拒收的最常见原因，改之前一定先在 vendor 站点重新上传一份样例验证。
