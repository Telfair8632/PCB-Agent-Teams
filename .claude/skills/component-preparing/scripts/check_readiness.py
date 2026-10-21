#!/usr/bin/env python3
"""bom-readiness 主入口：选元件阶段三联检 + 写 sentinel。

复用 draw-schematic 的工具：
  - verify_footprints  → library 检查
  - project_datasheets_dir → datasheets/ 路径解析

不再直连 LCSC vendoring：
  - 选品决策由 component-selecting skill 做并写 evidence JSON
    （Projects/<name>/datasheets/component_selecting/<safe_mpn>.json）
  - 本 skill 缺 library/datasheet/locale vendor evidence 时仅检查 evidence；
    缺 evidence → fail-fast，要求先跑 component-preparing

新增：
  - component-selecting evidence 复检（不再独立 query distributor）
  - sentinel 文件写入：<project>/datasheets/.bom_readiness.json

用法:
    python check_readiness.py <project.py>
    python check_readiness.py <project.py> --skip-stock  # 选元件阶段刚开始可跳

退出码:
    0  全过 → sentinel 写入，可进 draw-schematic
    1  有 fail → 修了再来
"""

# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import argparse
import glob
import json
import re
import subprocess
import sys
import time
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# 把 draw-schematic 的脚本目录放到 sys.path（复用现有逻辑）
DRAW_SCHEMATIC_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "draw-schematic" / "scripts"
)
sys.path.insert(0, str(DRAW_SCHEMATIC_SCRIPTS))

# 这些是 draw-schematic 已有的工具（lazy import 避免依赖缺失时崩）
# Note: 我们 *不再* 直连 LCSC vendoring / distributor query。
# 选品决策由 component-selecting skill 做并写 evidence JSON；本 skill 仅复检。
def _lazy_imports():
    from verify_footprints import (build_footprint_index, find_alternate,
                                    extract_components_from_py)
    from download_datasheet import project_datasheets_dir
    return {
        "build_footprint_index": build_footprint_index,
        "find_alternate": find_alternate,
        "extract_components_from_py": extract_components_from_py,
        "project_datasheets_dir": project_datasheets_dir,
    }


# ---------- component-selecting evidence gate ----------

def _safe_mpn_for_evidence(mpn: str) -> str:
    """Mirror component-selecting-JP/scripts/component_select.py::_safe_mpn.
    Both implementations must stay in sync (single source of truth would be
    nicer but cross-skill imports add coupling we don't want)."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", mpn or "")


def _evidence_path_for(ds_dir: Path, mpn: str) -> Path:
    """Where component-selecting commit_part writes per-MPN evidence."""
    return ds_dir / "component_selecting" / f"{_safe_mpn_for_evidence(mpn)}.json"


def _read_component_selecting_evidence(ds_dir: Path, mpn: str) -> Optional[Dict]:
    """Return the evidence dict if the JSON file exists and parses, else None."""
    p = _evidence_path_for(ds_dir, mpn)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


_COMPONENT_SELECTING_OK_VERDICTS = {"pass", "warn_single_source"}
_COMPONENT_SELECTING_OK_VENDOR_STATUSES = {"active"}


def _component_selecting_verdict_ok(ev: Optional[Dict]) -> bool:
    if not ev or ev.get("rollback_incomplete"):
        return False
    verdict = (ev.get("verdict") or "").strip().lower()
    return verdict in _COMPONENT_SELECTING_OK_VERDICTS


def _component_selecting_evidence_is_buyable(ev: Dict) -> tuple[bool, str]:
    """Whether component-selecting already accepted locale-specific buyability.

    component-preparing's BOM gate deliberately does not query distributors. The only acceptable
    source for stock/vendor pass/fail is component-selecting evidence, because
    that skill owns USER.md locale routing and verified vendor gates.
    """
    if ev.get("rollback_incomplete"):
        return False, "rollback_incomplete=true"

    verdict = (ev.get("verdict") or "").strip().lower()
    if verdict not in _COMPONENT_SELECTING_OK_VERDICTS:
        return False, f"verdict={verdict or '<empty>'}"

    vendor = ev.get("vendor") or {}
    status = (vendor.get("status") or "").strip().lower()
    if status not in _COMPONENT_SELECTING_OK_VENDOR_STATUSES:
        return False, f"vendor.status={status or '<empty>'}"
    if not vendor.get("url"):
        return False, "vendor.url missing"

    return True, "ok"


# 通用占位元件 — stock 检查跳过（5 元一袋的）
GENERIC_PLACEHOLDERS = {
    "Device:R", "Device:C", "Device:CP", "Device:C_Polarized", "Device:L",
    "Device:D", "Device:LED",
    "Device:Q_NPN", "Device:Q_PNP", "Device:Q_NMOS", "Device:Q_PMOS",
    "power:GND", "power:VCC", "power:VDD", "power:+5V", "power:+3V3",
    "Connector_Generic:Conn_01x02", "Connector_Generic:Conn_01x03",
    "Connector_Generic:Conn_01x04",
    # KiCad standard Connector library — Phoenix-style screw terminals + sockets
    "Connector:Conn_01x02_Socket", "Connector:Conn_01x03_Socket",
    "Connector:Conn_01x04_Socket",
    # KiCad standard Switch library — common slide / pushbutton switches
    "Switch:SW_SPDT", "Switch:SW_DPDT", "Switch:SW_Push",
}


def _looks_like_real_mpn(value: Optional[str]) -> bool:
    """value 看起来是真 MPN 还是规格描述。

    真 MPN：
      - 纯数字 ≥6 位（厂家 order code，如 1729128）
      - 含数字 + 大写字母混合（典型 IC MPN，如 AMC1311DWV）
    规格（不是 MPN）：
      - 100nF / 10uF / 470pF / 1uH / 500k / 10K / J_PWR_OUT / LED_RED
    """
    if not value or len(value) < 4:
        return False
    # 排除单位结尾（电容电感电阻规格）
    if value.endswith(("uF", "nF", "pF", "uH", "mH", "Ω", "Ohm",
                       "k", "K", "M", "F", "H")):
        return False
    # 排除全大写下划线分隔的描述（J_PWR_OUT / LED_RED 等）
    if "_" in value and value.replace("_", "").isalpha():
        return False
    if value.isdigit() and len(value) >= 6:
        return True  # 厂家 order code
    # 数字+单位字母结尾的阻值/容值写法（220R / 0R / 4k7 之外的单字母后缀）
    if re.fullmatch(r"\d+[RrKkMm]", value):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_upper = any(c.isupper() for c in value)
    return has_digit and has_upper


def _classify(py_symbol: Optional[str], mpn: Optional[str],
              ref: Optional[str] = None) -> str:
    """元件分类（决定要做哪些检查）：

    - generic：R/C/L/D/LED 等通用占位 + value 是规格描述 → 只查 library
    - connector：物理连接器/开关 → 查 library + datasheet（如有 MPN），跳 stock
    - ic：真元件（U1/U2 等） → 全部三联检

    分类优先级：
      1. ref 以 U 开头（IC / 模块）→ 强制 ic（即使 symbol 是 Conn 占位）
      2. ref 以 J/SW 开头 → connector
      3. symbol 是 GENERIC_PLACEHOLDERS：
         - 有真 MPN → connector（如 Conn_01x02 + 1729128）
         - 否则 → generic
      4. symbol 含 "Connector" / "Switch" → connector
      5. 兜底 → ic
    """
    has_real_mpn = _looks_like_real_mpn(mpn)

    # ref 前缀强判（最权威）
    if ref:
        if ref.startswith("U"):
            return "ic"
        if ref.startswith(("J", "SW")):
            return "connector"

    if py_symbol in GENERIC_PLACEHOLDERS:
        if has_real_mpn:
            return "connector"
        return "generic"
    if py_symbol and ("Connector" in py_symbol or "Switch" in py_symbol):
        return "connector"
    return "ic"


def check_one_component(comp: Dict, tools: Dict, ds_dir: Path,
                        skip_stock: bool = False,
                        sym_index: Optional[Dict[tuple, Path]] = None) -> Dict:
    """对一个元件做三联检（按类别决定查什么）。"""
    ref = comp.get("ref")
    mpn = comp.get("value")
    fp = comp.get("footprint")
    py_symbol = comp.get("symbol")

    category = _classify(py_symbol, mpn, ref)

    result = {
        "ref": ref,
        "mpn": mpn,
        "py_symbol": py_symbol,
        "footprint": fp,
        "category": category,
        "symbol_ok": False,      # symbol 在某个 .kicad_sym 库里能找到
        "footprint_ok": False,   # footprint 在某个 .pretty 库里能找到
        "library_ok": False,     # = symbol_ok AND footprint_ok
        "stock_ok": None,        # None = 跳过；True/False = 实查结果
        "datasheet_ok": False,
        # fidelity 检查（None = 没问题；dict = 有问题）
        "fidelity_masquerade": None,    # C: U* 用通用占位
        "fidelity_package": None,        # B: footprint TH/SMD vs datasheet
        "fidelity_mpn_mismatch": None,  # A: 由 main() 跨元件填回
        "fidelity_pin_count": None,     # D: symbol pin 数 vs evidence MPN pin 数
        "pinout_verification_required": None,  # 借用/兼容 footprint：pin 顺序需人工核对（advisory）
        "issues": [],
        "actions": [],
    }

    # fidelity-C：占位符伪装（不依赖网络/PDF，最便宜，先跑）
    masq = check_placeholder_masquerade(comp)
    if masq:
        result["fidelity_masquerade"] = masq
        result["issues"].append(
            f"[fidelity-C 占位符伪装] symbol={masq['symbol']} 是通用占位，"
            f"但 value={masq['value']} 是真 MPN — 必须换真符号"
        )
        result["actions"].append(
            f"先跑 component-preparing --mpn '{masq['value']}' 拉真 symbol+footprint，"
            "然后改 .py 的 symbol 字段为 vendoring 后的实际名"
        )

    # ============ Footprint 库检查 ============
    name_to_libs, exact = tools["build_footprint_index"]()
    if fp and ":" in fp:
        lib, name = fp.split(":", 1)
        if (lib, name) in exact:
            result["footprint_ok"] = True
        else:
            cands = tools["find_alternate"](name, mpn, name_to_libs)
            if cands:
                result["issues"].append(f"footprint 库引用错（候选: {cands[0]}）")
                result["actions"].append(f"改 .py L{comp.get('line', '?')} footprint 为 {cands[0]}")
            else:
                # 直连 LCSC 已废弃。component-selecting 是唯一选品 gate；
                # 这里只检查它写的 evidence JSON。缺 evidence 或 verdict 不通过 →
                # fail-fast，要求先跑 component-selecting commit_part。
                ev = _read_component_selecting_evidence(ds_dir, mpn) if mpn else None
                if _component_selecting_verdict_ok(ev):
                    # evidence 说 library 是齐的，但 footprint index 没找到 →
                    # 索引和 lib_external 不同步（commit 后没刷新）。提示重建即可。
                    result["issues"].append(
                        f"footprint 0 候选，但 component-selecting evidence "
                        f"(verdict={ev.get('verdict')}) "
                        f"标 library 已齐 — 索引可能 stale；重跑 component-preparing 或检查 "
                        f"lib_external/components.pretty/")
                    result["actions"].append(
                        "重跑 component-preparing（footprint 索引会重建）")
                elif mpn:
                    result["issues"].append(
                        f"footprint 0 候选 + 无通过的 component-selecting evidence (期望 "
                        f"{_evidence_path_for(ds_dir, mpn).relative_to(ds_dir.parent.parent) if ds_dir.parent.parent in _evidence_path_for(ds_dir, mpn).parents else _evidence_path_for(ds_dir, mpn)})")
                    result["actions"].append(
                        f"先跑 component-preparing --mpn '{mpn}' "
                        f"--ref {ref} --project-path <proj> --verified-url ... "
                        f"（让 component-selecting 决定 library/datasheet/vendor）")
                else:
                    result["issues"].append("footprint 0 候选 + 元件无 MPN（手工画或填 MPN）")

    # ============ Symbol 库检查（新加） ============
    # 画原理图必须 .py 里写的 symbol 在某个 .kicad_sym 里真存在，
    # 否则 circuit-synth 跑不动 / 跑出来 sch 是空 lib_id 的破图
    if sym_index is None:
        sym_index = build_symbol_index()
    if py_symbol and ":" in py_symbol:
        if verify_symbol_exists(py_symbol, sym_index):
            result["symbol_ok"] = True
        else:
            # 不再直连 LCSC；要么有 component-selecting evidence，要么 fail-fast。
            ev = _read_component_selecting_evidence(ds_dir, mpn) if mpn else None
            if _component_selecting_verdict_ok(ev):
                result["issues"].append(
                    f"symbol 不存在（{py_symbol}）— 但 component-selecting evidence "
                    f"(verdict={ev.get('verdict')}) 标 library 已齐；symbol 索引可能 stale，"
                    f"或 .py 里 symbol 名跟 lib_external 实际名不一致")
                result["actions"].append(
                    "查 lib_external/components.kicad_sym 找实际 symbol 名，"
                    "改 .py symbol=components:<actual_name>")
            elif mpn and len(mpn) >= 4 and any(c.isdigit() for c in mpn):
                result["issues"].append(
                    f"symbol 不存在（{py_symbol}）— 无通过的 component-selecting evidence")
                result["actions"].append(
                    f"先跑 component-preparing --mpn '{mpn}' "
                    f"--ref {ref} --project-path <proj> --verified-url ...")
            else:
                result["issues"].append(
                    f"symbol 不存在（{py_symbol}）— MPN 缺失或太短，不能走 component-selecting")
                result["actions"].append("手工画 symbol 或填合规 MPN 后走 component-selecting")
    elif py_symbol:
        # symbol 字符串没冒号（畸形）
        result["issues"].append(f"symbol 字符串畸形（{py_symbol}）— 必须是 Lib:Name")
    else:
        # 没 symbol 字段（不可能，circuit-synth Component() 必填）
        result["issues"].append("symbol 字段缺失")

    # library_ok = footprint AND symbol 双双 OK
    result["library_ok"] = result["footprint_ok"] and result["symbol_ok"]

    # ============ 可买性检查（component-selecting evidence）============
    if skip_stock or category in ("generic", "connector"):
        result["stock_ok"] = None  # 跳过
    elif not mpn or len(mpn) < 4:
        result["stock_ok"] = None
    else:
        ev_path = _evidence_path_for(ds_dir, mpn)
        ev = _read_component_selecting_evidence(ds_dir, mpn)
        result["component_selecting_evidence"] = str(ev_path)
        if not ev:
            result["stock_ok"] = False
            result["issues"].append(
                "无 component-selecting evidence，不能复检 locale vendor gate"
            )
            result["actions"].append(
                f"先跑 component-preparing --mpn '{mpn}' "
                f"--ref {ref} --project-path <proj> --verified-url ..."
            )
        else:
            vendor = ev.get("vendor") or {}
            result["component_selecting_verdict"] = ev.get("verdict")
            result["vendor_status"] = vendor.get("status")
            result["vendor_stock"] = vendor.get("stock")
            result["vendor_price"] = vendor.get("price")
            result["vendor_currency"] = vendor.get("currency")
            result["vendor_url"] = vendor.get("url")

            ok, reason = _component_selecting_evidence_is_buyable(ev)
            result["stock_ok"] = ok
            if not ok:
                result["issues"].append(
                    f"component-selecting evidence 未通过可买性 gate（{reason}）"
                )
                result["actions"].append(
                    f"重跑 component-preparing --mpn '{mpn}'，"
                    "或更换已被当前 locale verified vendor 接受的替代型号"
                )

    # ============ Datasheet 检查 ============
    if category == "generic":
        # 通用占位（R/C/L/D/LED/通用 connector）— 不需要 datasheet
        result["datasheet_ok"] = True
        result["datasheet"] = None
    else:
        safe_mpn = (mpn or "").replace("/", "_").replace(" ", "_")
        matching_pdfs = list(ds_dir.glob(f"*{safe_mpn}*.pdf")) if safe_mpn else []
        found_pdf = [p for p in matching_pdfs if _is_real_datasheet_pdf(p)]
        fake_pdf = [p for p in matching_pdfs if not _is_real_datasheet_pdf(p)]
        if found_pdf:
            result["datasheet_ok"] = True
            result["datasheet"] = str(found_pdf[0])
        elif fake_pdf:
            result["issues"].append(
                "datasheet PDF 是占位/无效文件（0-page / 过小 / 非真实 PDF）："
                + ", ".join(p.name for p in fake_pdf)
            )
            result["actions"].append(
                "删除占位 PDF，重跑 component-preparing 抓真实 datasheet；"
                "通用件没有真 MPN 时应留 datasheet=None"
            )
        elif mpn and len(mpn) >= 4 and any(c.isdigit() for c in mpn):
            # 不再直接 LCSC 下载 datasheet。component-selecting commit_part 已经
            # 在选品阶段把 datasheet 拉到 ds_dir 并写到 evidence。
            ev = _read_component_selecting_evidence(ds_dir, mpn)
            ds_from_ev = (ev or {}).get("datasheet", {}).get("path") if ev else None
            if ds_from_ev and _is_real_datasheet_pdf(ds_dir.parent / ds_from_ev):
                result["datasheet_ok"] = True
                result["datasheet"] = str(ds_dir.parent / ds_from_ev)
            elif ds_from_ev and (ds_dir.parent / ds_from_ev).exists():
                result["issues"].append(
                    f"component-selecting evidence 引用的 datasheet 不是有效 PDF：{ds_from_ev}"
                )
                result["actions"].append("重跑 component-preparing 抓真实 datasheet")
            elif _component_selecting_verdict_ok(ev):
                result["issues"].append(
                    f"datasheet 文件不在磁盘但 component-selecting evidence 标通过 — "
                    f"evidence 引用 {ds_from_ev or '<空>'} 不存在")
                result["actions"].append("重跑 component-preparing 让它重抓 datasheet")
            else:
                result["issues"].append(
                    f"datasheet 缺失 + 无通过的 component-selecting evidence")
                result["actions"].append(
                    f"先跑 component-preparing --mpn '{mpn}'（它会同时抓 library + datasheet）")
        else:
            # 物理连接器但 mpn 是描述（如 J_PWR_OUT）→ 不强制要 datasheet
            result["datasheet_ok"] = True
            result["datasheet"] = None

    # fidelity-B：footprint 封装类 vs datasheet 文字
    ds_path = result.get("datasheet")
    if ds_path:
        pkg_issue = check_package_class(comp, Path(ds_path))
        if pkg_issue:
            result["fidelity_package"] = pkg_issue
            result["issues"].append(
                f"[fidelity-B 封装不匹配] footprint={pkg_issue['footprint']} 是 "
                f"{pkg_issue['footprint_class']}，但 datasheet 是 {pkg_issue['datasheet_class']}"
                f"（证据：{pkg_issue['datasheet_evidence']}）"
            )
            result["actions"].append(
                f"换成 {pkg_issue['datasheet_class']} 类 footprint（重跑 component-preparing）"
            )

    # fidelity-D：symbol pin 数 vs evidence MPN pin 数
    pin_issue = check_pin_count_mismatch(comp, ds_dir)
    if pin_issue:
        result["fidelity_pin_count"] = pin_issue
        result["issues"].append(
            f"[fidelity-D pin 数不匹配] symbol {pin_issue['symbol_pins']} pin，"
            f"但 MPN {pin_issue['mpn']} 是 {pin_issue['evidence_pins']} pin"
        )
        result["actions"].append(
            f"换 {pin_issue['evidence_pins']} pin symbol/footprint，或换 {pin_issue['symbol_pins']} pin MPN"
        )

    # 借用/兼容 footprint：pin 顺序需人工核对（advisory，不 fail gate）
    pinout_issue = check_pinout_verification(comp, ds_dir)
    if pinout_issue:
        result["pinout_verification_required"] = pinout_issue

    return result


def generate_bom_csv(components: List[Dict], py_file: Path,
                     ds_dir: Path) -> Path:
    """从 components 列表生成统一 BOM CSV（写到 datasheets/）。

    格式（采购导入 + 给人看）：
        Qty,Refs,MPN,Category,Footprint,Vendor_Status,Vendor_Stock,Vendor_Url,Datasheet
    """
    # 按 MPN 聚合数量
    by_mpn: Dict[str, Dict] = {}
    for c in components:
        mpn = c.get("mpn") or "<placeholder>"
        if mpn not in by_mpn:
            by_mpn[mpn] = {
                "refs": [], "py_symbol": c.get("py_symbol"),
                "footprint": c.get("footprint"),
                "category": c.get("category"),
                "vendor_status": c.get("vendor_status"),
                "vendor_stock": c.get("vendor_stock"),
                "vendor_url": c.get("vendor_url"),
                "datasheet": c.get("datasheet"),
            }
        by_mpn[mpn]["refs"].append(c.get("ref"))

    # 写 CSV
    project_name = py_file.stem
    out_path = ds_dir / f"bom_{project_name}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Qty", "Refs", "MPN", "Category", "Footprint",
            "Vendor_Status", "Vendor_Stock", "Vendor_Url", "Datasheet",
        ])
        for mpn, info in by_mpn.items():
            refs = " ".join(sorted(info["refs"]))
            ds_name = Path(info["datasheet"]).name if info.get("datasheet") else ""
            writer.writerow([
                len(info["refs"]),
                refs,
                mpn,
                info.get("category") or "",
                info.get("footprint") or "",
                info.get("vendor_status") or "",
                info.get("vendor_stock") or "",
                info.get("vendor_url") or "",
                ds_name,
            ])
    return out_path


def _mpn_pdf_match(mpn: str, pdf_stem: str) -> bool:
    """MPN 跟 PDF 文件名（不含扩展名）是否表示同一元件。

    匹配规则（任一即算 match）：
      - MPN 是 PDF 名的子串（如 1729128 → 1729128_C7509570_datasheet）
      - PDF 第一个 token 是 MPN 的前缀且 ≥ 6 字符
        （如 AMC1311 → AMC1311DWV; TLV70033 → TLV70033DDCR）
    """
    if not mpn:
        return False
    # Use the same normalizer as evidence file naming, otherwise PDFs whose
    # filename was generated via _safe_mpn_for_evidence(mpn) (e.g. commas, dots,
    # parens) won't match the raw mpn value and get wrongly archived as orphans.
    safe = _safe_mpn_for_evidence(mpn).lower()
    name = pdf_stem.lower()
    if safe in name:
        return True
    # PDF 名第一个 token（按 _-. 切）
    first_token = re.split(r'[_\-\s.]', name, maxsplit=1)[0]
    if len(first_token) >= 6 and safe.startswith(first_token):
        return True
    if len(safe) >= 6 and first_token.startswith(safe[:6]):
        return True
    return False


def _pdf_page_count(pdf_path: Path) -> Optional[int]:
    """Return PDF page count when pdfinfo is available; otherwise None."""
    try:
        r = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=5, encoding="utf-8", errors="replace"
        )
        if r.returncode != 0:
            return None
        m = re.search(r"^Pages:\s*(\d+)\s*$", r.stdout, re.M)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _is_real_datasheet_pdf(pdf_path: Path) -> bool:
    """Reject placeholder PDFs used only to satisfy a gate.

    A real datasheet must at least be a non-trivial PDF. If pdfinfo is present,
    it must also report one or more pages.
    """
    try:
        if not pdf_path.exists() or pdf_path.stat().st_size < 1024:
            return False
        with pdf_path.open("rb") as f:
            if f.read(4) != b"%PDF":
                return False
        pages = _pdf_page_count(pdf_path)
        if pages is not None and pages <= 0:
            return False
        return True
    except Exception:
        return False


# ===================================================================
# Symbol library 索引 — verify_footprints 只扫 footprint，这里补 symbol
# ===================================================================

_KICAD_SYM_DIRS = [
    Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
    Path("/usr/share/kicad/symbols"),
    Path("/usr/local/share/kicad/symbols"),
]
# Windows install roots (standard Program Files + custom drive layouts)
for _pat in (r"C:\Program Files\KiCad\*\share\kicad\symbols",
             r"F:\KiCad\*\share\kicad\symbols",
             r"F:\KiCad\share\kicad\symbols"):
    for _p in glob.glob(_pat):
        _KICAD_SYM_DIRS.append(Path(_p))
_LIB_EXTERNAL_DIR = Path(__file__).resolve().parents[4] / "lib_external"

# 顶层 symbol 声明：行首任意空白（tab 或空格），然后 (symbol "Name"
# KiCad 标准库用 1 tab，easyeda2kicad 用 2 空格 — 两种都得吃。
# 子 symbol 名字带 _N_M 后缀（unit-style），后面用名字 filter 掉。
_TOPLEVEL_SYMBOL_PAT = re.compile(r'^[ \t]+\(symbol\s+"([^"]+)"', re.MULTILINE)


def _find_kicad_sym_dir() -> Optional[Path]:
    for p in _KICAD_SYM_DIRS:
        if p.exists():
            return p
    return None


def build_symbol_index() -> Dict[tuple, Path]:
    """扫所有 .kicad_sym 文件，返回 {(lib_name, sym_name): file_path}。

    覆盖：
    - KiCad 标准库（_KICAD_SYM_DIRS 里能找到的目录里所有 .kicad_sym）
    - lib_external/*.kicad_sym（vendoring 来的）
    """
    exact: Dict[tuple, Path] = {}

    search_paths = []
    kc = _find_kicad_sym_dir()
    if kc:
        search_paths.append(kc)
    if _LIB_EXTERNAL_DIR.exists():
        search_paths.append(_LIB_EXTERNAL_DIR)

    for base in search_paths:
        for sym_file in base.glob("*.kicad_sym"):
            lib_name = sym_file.stem
            try:
                text = sym_file.read_text(errors="replace", encoding="utf-8")
            except Exception:
                continue
            for m in _TOPLEVEL_SYMBOL_PAT.finditer(text):
                sym_name = m.group(1)
                # 跳过子符号（含 _0_1 / _1_0 等后缀的内部子单元）
                # 顶层符号也可能含下划线；用更严格判断：
                # 子符号一定带 _N_M 末尾数字模式
                if re.search(r"_\d+_\d+$", sym_name):
                    continue
                exact[(lib_name, sym_name)] = sym_file
    return exact


def verify_symbol_exists(symbol_str: Optional[str],
                          sym_index: Dict[tuple, Path]) -> bool:
    """symbol_str 形如 "Library:SymbolName"，查索引里在不在。"""
    if not symbol_str or ":" not in symbol_str:
        return False
    lib, name = symbol_str.split(":", 1)
    return (lib, name) in sym_index


# ===================================================================
# Fidelity check 区 — sch/PCB 物理一致性硬门槛
# 触发血泪案：U2 .py value="B0505S-1WR2" 但 BOM 写 IB0505XT-1WR3，
#   footprint=Package_DIP:DIP-4_W7.62mm（TH）但 datasheet 是 SMD，
#   symbol=Connector_Generic:Conn_01x04（占位）当真器件用，全部静默通过。
# 这一区的 3 个 check 必须在画图前 fail-fast。
# ===================================================================

# footprint 库前缀 → 封装类
PACKAGE_CLASS_BY_FP_PREFIX = {
    # TH
    "Resistor_THT": "TH", "Capacitor_THT": "TH", "Diode_THT": "TH",
    "Inductor_THT": "TH", "LED_THT": "TH", "Crystal_THT": "TH",
    "Package_DIP": "TH", "Package_TO_SOT_THT": "TH",
    "Package_TO_THT": "TH",
    "Button_Switch_THT": "TH",
    # SMD
    "Resistor_SMD": "SMD", "Capacitor_SMD": "SMD", "Diode_SMD": "SMD",
    "Inductor_SMD": "SMD", "LED_SMD": "SMD", "Crystal_SMD": "SMD",
    "Package_SO": "SMD", "Package_QFN": "SMD", "Package_DFN": "SMD",
    "Package_BGA": "SMD", "Package_LCC": "SMD", "Package_LGA": "SMD",
    "Package_TO_SOT_SMD": "SMD", "Package_CSP": "SMD",
    "Package_SON": "SMD", "Package_SO_SMD": "SMD",
    "Button_Switch_SMD": "SMD",
}


def _footprint_class(fp: Optional[str]) -> str:
    """从 footprint 库前缀推断 TH / SMD / unknown。

    优先级：库前缀直查 → 库名/footprint 名含 SMD/THT/DIP 关键词 → unknown
    """
    if not fp or ":" not in fp:
        return "unknown"
    lib, name = fp.split(":", 1)
    if lib in PACKAGE_CLASS_BY_FP_PREFIX:
        return PACKAGE_CLASS_BY_FP_PREFIX[lib]
    upper_lib = lib.upper()
    if "SMD" in upper_lib:
        return "SMD"
    if "THT" in upper_lib or "DIP" in upper_lib:
        return "TH"
    upper_name = name.upper()
    if "SMD" in upper_name or "SMT" in upper_name:
        return "SMD"
    if "_TH_" in "_" + upper_name + "_" or "DIP" in upper_name or "THT" in upper_name:
        return "TH"
    return "unknown"


def _datasheet_package_class(pdf_path: Optional[Path]) -> Dict:
    """从 datasheet PDF 文字推断封装类。

    返回 {"class": "SMD"/"TH"/"unknown", "evidence": [...]}。
    用 pdftotext（poppler）抽文字，失败则 unknown 不阻塞。
    """
    if not pdf_path or not Path(pdf_path).exists():
        return {"class": "unknown", "evidence": []}
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", "-q", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace"
        )
        text = out.stdout.lower() if out.returncode == 0 else ""
    except Exception as e:
        return {"class": "unknown", "evidence": [f"pdftotext failed: {e}"]}
    if not text:
        return {"class": "unknown", "evidence": []}

    smd_keywords = ["smd 封装", "smd package", "smd-",
                    "surface mount", "贴片", "smt package",
                    "soic", "qfn", "tsop", "msop", "sot-23", "lga", "bga"]
    th_keywords = ["through hole", "通孔", "插件", "thru-hole",
                   "dip 封装", "dip package", "dip-4", "dip-8", "dip-14",
                   "dip-16", "to-220", "to-247"]
    sip_keywords = ["sip-4", "sip package", "sip 4", "sip 封装"]

    smd_hit = [k for k in smd_keywords if k in text]
    th_hit = [k for k in th_keywords if k in text]
    sip_hit = [k for k in sip_keywords if k in text]

    # SIP 通常是 TH，但有些 mini DC-DC 是 SMT-SIP，要看 dimension
    if sip_hit and not smd_hit:
        return {"class": "TH", "evidence": sip_hit[:3] + ["SIP→TH"]}
    if smd_hit and not th_hit:
        return {"class": "SMD", "evidence": smd_hit[:3]}
    if th_hit and not smd_hit:
        return {"class": "TH", "evidence": th_hit[:3]}
    if smd_hit and th_hit:
        if len(smd_hit) >= len(th_hit):
            return {"class": "SMD", "evidence": smd_hit[:3]}
        return {"class": "TH", "evidence": th_hit[:3]}
    return {"class": "unknown", "evidence": []}


def check_package_class(comp: Dict, datasheet_path: Optional[Path]) -> Optional[Dict]:
    """fidelity-B：footprint 推断的 class vs datasheet 文字的 class。

    返回 None = 一致或不可判断；dict = mismatch（必须 fail）。
    """
    fp = comp.get("footprint")
    if not fp or not datasheet_path:
        return None
    fp_class = _footprint_class(fp)
    if fp_class == "unknown":
        return None
    ds_info = _datasheet_package_class(datasheet_path)
    if ds_info["class"] == "unknown":
        return None
    if ds_info["class"] != fp_class:
        return {
            "ref": comp.get("ref"),
            "footprint": fp,
            "footprint_class": fp_class,
            "datasheet_class": ds_info["class"],
            "datasheet_evidence": ds_info["evidence"],
        }
    return None


def check_pin_count_mismatch(comp: Dict, ds_dir: Path) -> Optional[Dict]:
    """fidelity-D：连接器 symbol pin 数 vs evidence MPN pin 数。

    只对 Connector symbol 触发。两边都能解析出 pin 数才比对，
    任一侧缺失就跳过（不误报）。
    """
    symbol = comp.get("symbol") or ""
    mpn = comp.get("value")

    # 只检查 Connector 类 symbol（KiCad 命名：Conn_01x04, Conn_02x04_Odd_Even 等）
    conn_match = re.search(r'(?:^|:)Conn_(\d+)x(\d+)(?:_|$)', symbol)
    if not conn_match:
        return None
    symbol_pins = int(conn_match.group(1)) * int(conn_match.group(2))

    # 从 evidence JSON 读 pin 数
    if not mpn:
        return None
    ev = _read_component_selecting_evidence(ds_dir, mpn)
    if not ev:
        return None
    positions = (ev.get("key_parameters") or {}).get("positions")
    if not positions:
        return None
    try:
        evidence_pins = int(positions)
    except (ValueError, TypeError):
        return None

    if symbol_pins == evidence_pins:
        return None

    return {
        "ref": comp.get("ref"),
        "mpn": mpn,
        "symbol": symbol,
        "symbol_pins": symbol_pins,
        "evidence_pins": evidence_pins,
    }


# Borrowed / package-compatible library statuses: footprint may fit but the
# pin order is NOT guaranteed for this MPN. Must surface for human pinout check.
# Kept in sync with verify_vendoring.PINOUT_VERIFY_STATUSES.
_PINOUT_VERIFY_STATUSES = {
    "kicad_std_compatible",
    "compatible_existing",
    "external_cache_compatible",
}


def check_pinout_verification(comp: Dict, ds_dir: Path) -> Optional[Dict]:
    """Surface a borrowed/compatible footprint that needs human pinout check.

    Returns None when the library status is an exact/standard one (no concern),
    or a dict flagging required-but-unverified pin order otherwise. Advisory —
    does NOT fail the gate (pad fit may be fine; only pin order is unconfirmed),
    but it is lifted out of the evidence into the readiness summary so it can't
    be missed.
    """
    mpn = comp.get("value")
    if not mpn:
        return None
    ev = _read_component_selecting_evidence(ds_dir, mpn)
    if not ev:
        return None
    status = (ev.get("library") or {}).get("status")
    if status not in _PINOUT_VERIFY_STATUSES:
        return None
    return {
        "ref": comp.get("ref"),
        "mpn": mpn,
        "library_status": status,
        "pin_order_verified": False,
        "reason": (
            f"library.status={status} 是借用/封装兼容 footprint；焊盘可能对得上，"
            f"但 pin 顺序不保证属于 {mpn} —— 必须对 datasheet 人工核对 pinout"
        ),
    }


def check_placeholder_masquerade(comp: Dict) -> Optional[Dict]:
    """fidelity-C：U* 用通用占位 symbol + value 是真 MPN → 伪装真器件。

    最危险的一类：ERC 通过、视觉装得像、实际焊不上。
    """
    ref = comp.get("ref") or ""
    symbol = comp.get("symbol") or ""
    value = comp.get("value")
    if not ref.startswith("U"):
        return None
    if symbol not in GENERIC_PLACEHOLDERS:
        return None
    if not _looks_like_real_mpn(value):
        return None
    return {
        "ref": ref,
        "symbol": symbol,
        "value": value,
        "msg": "U* 用通用占位 symbol 但 value 是真 MPN，必须换真符号",
    }


def find_project_claude_md(py_file: Path) -> Optional[Path]:
    """沿 .py 父目录上溯找项目 CLAUDE.md（最多 5 层）。"""
    cur = py_file.parent
    for _ in range(5):
        candidate = cur / "CLAUDE.md"
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def check_mpn_consistency(components: List[Dict],
                           claude_md_path: Optional[Path]) -> Dict:
    """fidelity-A：.py value 跟 CLAUDE.md BOM 表 MPN 列必须一致。

    BOM 表期望格式（markdown）：
        | **U2** | 1 | 隔离 DC-DC | Mornsun **IB0505XT-1WR3** ... | SIP package |

    匹配逻辑：
      - 找含 `| ref |` 的行
      - 提该行所有看起来像真 MPN 的 token
      - .py value 必须是其中之一的子串/全等（≥6 字符重叠）
      - 否则 → mismatch（fail）
      - ref 在 BOM 表里完全找不到 → missing_in_bom（warn，不一定 fail）
    """
    result: Dict = {"checked": False, "mismatches": [], "missing_in_bom": []}
    if not claude_md_path or not Path(claude_md_path).exists():
        result["reason"] = "no CLAUDE.md"
        return result

    md = Path(claude_md_path).read_text(encoding="utf-8")
    lines = md.splitlines()
    result["checked"] = True
    result["claude_md"] = str(claude_md_path)

    for c in components:
        ref = c.get("ref")
        # 接受两种 schema：raw .py 元件用 "value"；results dict 用 "mpn"
        py_value = c.get("value") or c.get("mpn")
        if not ref:
            continue
        # category 来自 results dict；raw 元件没有，需要现场分类
        cat = c.get("category") or _classify(
            c.get("py_symbol") or c.get("symbol"), py_value, ref
        )
        if cat == "generic":
            continue
        if not _looks_like_real_mpn(py_value):
            continue

        ref_re = re.compile(
            rf"\|\s*\*{{0,2}}\s*{re.escape(ref)}\s*\*{{0,2}}\s*\|"
        )
        matched_row = None
        for line in lines:
            if ref_re.search(line):
                matched_row = line
                break
        if not matched_row:
            result["missing_in_bom"].append({
                "ref": ref, "py_value": py_value,
                "msg": f"CLAUDE.md BOM 表里找不到 {ref} 这行",
            })
            continue

        clean = re.sub(r"\*+", "", matched_row)
        candidates: List[str] = []
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-/]{3,}", clean):
            if _looks_like_real_mpn(token):
                candidates.append(token)
        if not candidates:
            continue

        # Use the same MPN normalizer as evidence-path / orphan-PDF matching
        # so "LM1117T-3.3/NOPB" (CSV/CLAUDE.md) and "LM1117T-3.3_NOPB" (lib symbol)
        # don't trip a false fidelity-A mismatch over `/` ↔ `_`.
        py_norm = _safe_mpn_for_evidence(py_value).upper()
        matched = False
        for cand in candidates:
            cand_norm = _safe_mpn_for_evidence(cand).upper()
            if py_norm == cand_norm:
                matched = True
                break
            if py_norm in cand_norm or cand_norm in py_norm:
                if min(len(py_norm), len(cand_norm)) >= 6:
                    matched = True
                    break
        if not matched:
            result["mismatches"].append({
                "ref": ref,
                "py_value": py_value,
                "claude_md_mpns": candidates,
                "row": matched_row.strip()[:160],
            })
    return result


def manage_datasheets_dir(ds_dir: Path, components: List[Dict],
                          clean: bool = False) -> Dict:
    """扫 datasheets/ 目录：找孤儿 PDF + 缺失的 datasheet。

    clean=True 时把孤儿移到 datasheets/_archive/ 子目录（不是直接删，可恢复）。
    """
    if not ds_dir.exists():
        return {"orphans": [], "missing": []}

    expected_mpns: set[str] = set()
    for c in components:
        mpn = c.get("mpn")
        # 只把"真 MPN"加进 expected — 描述（J_PWR_OUT 等）不算
        if (mpn and c.get("category") != "generic"
                and _looks_like_real_mpn(mpn)):
            expected_mpns.add(mpn)

    pdfs = list(ds_dir.glob("*.pdf"))
    invalid_pdfs = [pdf.name for pdf in pdfs if not _is_real_datasheet_pdf(pdf)]

    orphans: List[str] = []
    matched: set[str] = set()
    for pdf in pdfs:
        if not _is_real_datasheet_pdf(pdf):
            orphans.append(pdf.name)
            continue
        for mpn in expected_mpns:
            if _mpn_pdf_match(mpn, pdf.stem):
                matched.add(mpn)
                break
        else:
            orphans.append(pdf.name)

    archived: List[str] = []
    if clean and orphans:
        archive_dir = ds_dir / "_archive"
        archive_dir.mkdir(exist_ok=True)
        for orphan_name in orphans:
            src = ds_dir / orphan_name
            dst = archive_dir / orphan_name
            # 同名已存在就加时间戳
            if dst.exists():
                stem = dst.stem
                ts = int(time.time())
                dst = archive_dir / f"{stem}_{ts}.pdf"
            try:
                src.rename(dst)
                archived.append(orphan_name)
            except Exception as e:
                print(f"    ⚠ 归档失败 {orphan_name}: {e}", file=sys.stderr)

    missing = sorted(expected_mpns - matched)
    return {
        "orphans": orphans,
        "missing": missing,
        "archived": archived,
        "total_pdfs": len(pdfs),
        "invalid_pdfs": invalid_pdfs,
        "expected_mpns": sorted(expected_mpns),
    }


def audit_component_selecting_dir(ds_dir: Path) -> Dict:
    """Detect process artifacts that should not live in final evidence root."""
    ev_dir = ds_dir / "component_selecting"
    if not ev_dir.exists():
        return {"exists": False, "pollution": [], "total_json": 0}

    pollution: List[Dict] = []
    jsons = sorted(ev_dir.glob("*.json"))
    for p in jsons:
        reason = ""
        if p.name.startswith("_pending_"):
            reason = "pending_longlist_artifact"
        else:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            verdict = str(data.get("verdict") or "")
            if data.get("rollback_incomplete") or verdict in {"lib_pending", "fail", "pending_llm_fetch"}:
                reason = f"non_final_verdict:{verdict or 'unknown'}"
        if reason:
            pollution.append({"path": str(p), "reason": reason})

    return {
        "exists": True,
        "total_json": len(jsons),
        "pollution": pollution,
        "scratch_dir": str(ev_dir / "_scratch"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("py_file", help="circuit-synth Python 源文件")
    ap.add_argument("--skip-stock", action="store_true",
                    help="跳过 component-selecting 可买性 evidence gate（早期选型/审查用）")
    ap.add_argument("--no-csv", action="store_true",
                    help="不生成 BOM CSV")
    # Three-mode safety: default `dry-run` only PRINTS what would be archived;
    # explicit `apply` actually moves files into _archive/. Bare `--clean-orphans`
    # (no value) is treated as `apply` for backwards compat with existing
    # SKILL.md examples — it's still destructive but at least the user typed it.
    # `--clean-orphans=off` skips the orphan probe entirely.
    ap.add_argument("--clean-orphans", nargs="?", const="apply", default="dry-run",
                    choices=["off", "dry-run", "apply"],
                    help="orphan-PDF policy: dry-run (default; print only) | "
                         "apply (move to _archive/; bare --clean-orphans = apply) | "
                         "off (skip probe). Default dry-run prevents accidental "
                         "archiving of PDFs whose filename normalizer differs "
                         "from the BOM MPN spelling.")
    ap.add_argument("--audit", action="store_true",
                    help="审查模式：只跑结构性检查（fidelity A/B/C + 孤儿 PDF），"
                         "不查库存、不下 datasheet、不写 sentinel；"
                         "用于复查老项目，任何时候都能秒跑")
    ap.add_argument("--no-inject-mpn", action="store_true",
                    help="all_pass 后不自动把 MPN/Datasheet/Manufacturer 注回 .py "
                         "Component(...) 调用。默认会注，让 .kicad_sch 带实例属性。")
    args = ap.parse_args()

    # audit 模式自动 implies skip-stock + no-csv，且不写 sentinel
    if args.audit:
        args.skip_stock = True
        args.no_csv = True

    py_file = Path(args.py_file).resolve()
    if not py_file.exists():
        sys.exit(f"❌ {py_file} 不存在")

    print(f"=== component-preparing BOM gate 三联检：{py_file.name} ===")

    tools = _lazy_imports()
    ds_dir = tools["project_datasheets_dir"](py_file)
    print(f"  datasheets 目录：{ds_dir}")

    components = tools["extract_components_from_py"](py_file)
    print(f"  元件总数：{len(components)}")

    # 一次构建 symbol 索引（vendor 后会被 check_one_component 内部重建）
    sym_index = build_symbol_index()
    print(f"  symbol 索引：{len(sym_index)} 条（KiCad 标准 + lib_external）")

    results: List[Dict] = []
    for c in components:
        ref = c.get("ref")
        mpn = c.get("value")
        print(f"\n  → {ref} ({mpn or '<no-mpn>'})...")
        r = check_one_component(c, tools, ds_dir,
                                skip_stock=args.skip_stock,
                                sym_index=sym_index)
        results.append(r)
        # 临时只打印 B/C/library/stock/datasheet 状态；A（跨元件）后面跑完再补
        if r["issues"]:
            print(f"    ⚠ {'; '.join(r['issues'])}")
            for a in r["actions"]:
                print(f"        → {a}")

    # ============ Fidelity-A：MPN 一致性（跨元件，扫 CLAUDE.md） ============
    claude_md_path = find_project_claude_md(py_file)
    mpn_consistency = check_mpn_consistency(results, claude_md_path)
    if mpn_consistency.get("checked"):
        print(f"\n  🔍 Fidelity-A：MPN 一致性 vs {claude_md_path.name}")
        if mpn_consistency["mismatches"]:
            print(f"    ❌ {len(mpn_consistency['mismatches'])} 个 MPN 不一致：")
            # 把 mismatch 信息回填到各 component 的 issues
            for m in mpn_consistency["mismatches"]:
                print(f"      - {m['ref']}: .py value={m['py_value']!r} "
                      f"vs CLAUDE.md MPN={m['claude_md_mpns']}")
                for r in results:
                    if r["ref"] == m["ref"]:
                        r["fidelity_mpn_mismatch"] = m
                        r["issues"].append(
                            f"[fidelity-A MPN 不一致] .py value={m['py_value']!r} "
                            f"≠ CLAUDE.md BOM 表 MPN {m['claude_md_mpns']}"
                        )
                        r["actions"].append(
                            f"改 .py value 字段为 BOM 表里的 MPN（或反过来改 BOM 表）"
                        )
        else:
            print(f"    ✅ 全部 .py value 跟 CLAUDE.md BOM 一致")
        if mpn_consistency["missing_in_bom"]:
            print(f"    ⚠ {len(mpn_consistency['missing_in_bom'])} 个 ref 在 BOM 表里找不到（warn，不 fail）：")
            for m in mpn_consistency["missing_in_bom"]:
                print(f"      - {m['ref']} ({m['py_value']})")
    else:
        print(f"\n  ⚠ Fidelity-A 跳过：{mpn_consistency.get('reason', 'unknown')}")

    # ============ 最终 pass/fail 判定（含 fidelity） ============
    n_pass = 0
    n_fail = 0
    print(f"\n  📊 元件级最终判定：")
    for r in results:
        comp_pass = (
            r["library_ok"] and
            r["stock_ok"] is not False and
            r["datasheet_ok"] and
            r["fidelity_masquerade"] is None and
            r["fidelity_package"] is None and
            r["fidelity_mpn_mismatch"] is None and
            r.get("fidelity_pin_count") is None
        )
        if comp_pass:
            n_pass += 1
        else:
            n_fail += 1
            print(f"    ❌ {r['ref']}: {'; '.join(r['issues'])}")

    # 借用/兼容 footprint 的 pinout 人工核对清单（advisory，不影响 pass/fail，
    # 但显式抬到顶层让用户必须看到，而不是埋在 evidence 标签里）。
    pinout_required = [r for r in results if r.get("pinout_verification_required")]
    if pinout_required:
        print(f"\n  ⚠ pinout 需人工核对（{len(pinout_required)} 个借用/兼容 footprint，pin 顺序未确认）：")
        for r in pinout_required:
            pv = r["pinout_verification_required"]
            print(f"      - {r['ref']} ({pv['mpn']}) [{pv['library_status']}]: {pv['reason']}")
        print("    → 对照 datasheet 核对 pin 顺序后再进 draw-schematic（焊盘对得上 ≠ pin 对得上）")

    # ============ CSV BOM 生成 ============
    csv_path = None
    if not args.no_csv:
        csv_path = generate_bom_csv(results, py_file, ds_dir)
        print(f"\n  📄 BOM CSV: {csv_path}")

    # ============ Datasheets 目录管理 ============
    # `--clean-orphans` is now tri-state: off / dry-run (default, prints only) /
    # apply (move to _archive/). manage_datasheets_dir's clean= boolean only
    # controls actual file moves, so map dry-run → False to keep PDFs in place
    # but still surface what *would* be archived.
    _clean_mode = args.clean_orphans
    _do_archive = (_clean_mode == "apply")
    if _clean_mode == "off":
        ds_mgmt = {"total_pdfs": len(list(ds_dir.glob("*.pdf"))) if ds_dir.exists() else 0,
                   "orphans": [], "missing": [], "invalid_pdfs": [], "archived": []}
        print(f"\n  📁 datasheets/ 目录：{ds_mgmt['total_pdfs']} 个 PDF (orphan 探测已关闭)")
    else:
        ds_mgmt = manage_datasheets_dir(ds_dir, results, clean=_do_archive)
        print(f"\n  📁 datasheets/ 目录：{ds_mgmt['total_pdfs']} 个 PDF")
    if ds_mgmt.get("invalid_pdfs"):
        print(f"    ❌ 无效 / 占位 PDF：{len(ds_mgmt['invalid_pdfs'])} 个")
        for bad in ds_mgmt["invalid_pdfs"]:
            print(f"      - {bad}")
    if ds_mgmt["orphans"]:
        if ds_mgmt.get("archived"):
            print(f"    ✓ 孤儿 PDF 已归档到 datasheets/_archive/：{len(ds_mgmt['archived'])} 个")
            for o in ds_mgmt["orphans"]:
                print(f"      - {o}")
        else:
            print(f"    ⚠ 孤儿 PDF（不属于当前 BOM）：{len(ds_mgmt['orphans'])} 个 — 建议处理（不影响 gate）")
            for o in ds_mgmt["orphans"]:
                print(f"      - {o}")
            if _clean_mode == "dry-run":
                print(f"    👉 删除：python3 .claude/skills/component-preparing/scripts/check_readiness.py "
                      f"<py_file> --clean-orphans=apply")
                print(f"       或保留并写入文档：把每个孤儿 MPN 加回 BOM / 写说明到 docs/bom.md")
            # _clean_mode == "off" 时不给命令（user 主动关了探测）
    if ds_mgmt["missing"]:
        print(f"    ⚠ 缺失 datasheet（BOM 有 MPN 但没 PDF）：")
        for m in ds_mgmt["missing"]:
            print(f"      - {m}")

    # ============ component_selecting evidence 根目录卫生 ============
    ev_mgmt = audit_component_selecting_dir(ds_dir)
    if ev_mgmt.get("pollution"):
        print(f"\n  ❌ component_selecting/ 根目录有过程证据污染：{len(ev_mgmt['pollution'])} 个")
        for item in ev_mgmt["pollution"]:
            print(f"      - {Path(item['path']).name}: {item['reason']}")
        print("    → 运行 component-selecting --clean-candidate-artifacts --project-path <project> 审计，确认后加 --apply-cleanup 移入 _scratch/")

    # Fail-closed when there is nothing real to verify. Without this, an empty
    # or stub .py (extract_components_from_py returns []) silently produces
    # n_fail=0 → all_pass=True → sentinel green-lights Phase 3 with no BOM
    # actually checked. Symptoms in the wild: bom CSV with header-only row,
    # sentinel components=[], and downstream draw-schematic running unguarded.
    #
    # Only `len(components) == 0` triggers the vacuous fail. A design that
    # is legitimately passive-only (every Component has value="10K"/"100nF",
    # so _looks_like_real_mpn returns False) is still a real BOM; gating on
    # real_mpn_count would false-fail it.
    real_mpn_count = sum(1 for r in results if _looks_like_real_mpn(r.get("mpn")))
    vacuous_reason: Optional[str] = None
    if len(components) == 0:
        vacuous_reason = "py_extracted_zero_components"

    all_pass = (
        vacuous_reason is None
        and n_fail == 0
        and not ds_mgmt.get("invalid_pdfs")
        and not ev_mgmt.get("pollution")
    )
    sentinel = {
        "verified_at": datetime.utcnow().isoformat() + "Z",
        "py_file": str(py_file),
        "py_mtime": int(py_file.stat().st_mtime),
        "skip_stock": args.skip_stock,
        "audit_mode": args.audit,
        "components": results,
        "datasheets_management": ds_mgmt,
        "component_selecting_management": ev_mgmt,
        "mpn_consistency": mpn_consistency,
        "bom_csv": str(csv_path) if csv_path else None,
        "summary": {"total": len(components), "pass": n_pass, "fail": n_fail,
                    "real_mpn_count": real_mpn_count},
        "vacuous_reason": vacuous_reason,
        "all_pass": all_pass,
    }

    print(f"\n=== 总结 ===")
    print(f"  ✅ 过：{n_pass}")
    print(f"  ❌ 没过：{n_fail}")
    _unarchived_orphans = [
        o for o in ds_mgmt.get("orphans", [])
        if o not in (ds_mgmt.get("archived") or [])
    ]
    if _unarchived_orphans:
        print(f"  ⚠ 孤儿 PDF：{len(_unarchived_orphans)} 个 — 加 --clean-orphans=apply 归档，或回写 BOM")
    if vacuous_reason:
        print(f"  ⛔ 空 BOM gate: {vacuous_reason} — 提取出 "
              f"{len(components)} 个元件 / {real_mpn_count} 个真 MPN；"
              f"无法对未知输入开绿灯")

    if args.audit:
        # 审查模式：只输出报告 JSON 到 stdout 旁路 + 不写 sentinel
        audit_report_path = ds_dir / f".bom_readiness_audit_{int(time.time())}.json"
        audit_report_path.parent.mkdir(parents=True, exist_ok=True)
        audit_report_path.write_text(json.dumps(sentinel, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  📋 审查报告：{audit_report_path}")
        print(f"  ℹ️  审查模式不写 sentinel — draw-schematic 看不到这次结果")
        if all_pass:
            print(f"\n✅ 审查无问题")
        else:
            print(f"\n❌ 发现 {n_fail} 个问题（详见报告 JSON）")
            sys.exit(1)
        return

    # If all_pass and inject is enabled, modify .py BEFORE writing sentinel —
    # otherwise sentinel.py_mtime would refer to the pre-inject .py and the
    # next pipeline run would fail with "py changed since sentinel" until the
    # user re-ran readiness manually.
    if all_pass and not args.no_inject_mpn:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from inject_mpn_props import inject as _inject
            n_injected, log = _inject(py_file, sentinel, dry_run=False)
            if n_injected > 0:
                print(f"\n  💉 inject_mpn_props: {n_injected} 个 Component 加 MPN/Datasheet/Manufacturer kwargs")
                # Refresh py_mtime after the inject so pipeline.py phase 0
                # accepts the .py as in-sync with sentinel.
                sentinel["py_mtime"] = int(py_file.stat().st_mtime)
            else:
                print("\n  💉 inject_mpn_props: 无可注（已注或无 evidence）")
        except Exception as e:
            print(f"\n  ⚠ inject_mpn_props 跳过：{e}")

    sentinel_path = ds_dir / ".bom_readiness.json"
    sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel_path.write_text(json.dumps(sentinel, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Sentinel：{sentinel_path}")
    if all_pass:
        print(f"\n✅ BOM 验过，可进 draw-schematic")
    else:
        print(f"\n❌ 修以上问题再跑")
        sys.exit(1)


if __name__ == "__main__":
    main()
