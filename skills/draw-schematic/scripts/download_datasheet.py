#!/usr/bin/env python3
"""自动下载 LCSC datasheet PDF 到项目 datasheets/。

为啥需要：每当 pipeline 替换元件（如 J1 MPN 1714984 → 1868076），新元件
的 datasheet 必须跟着进项目。否则用户去 PCB / 焊接阶段对照 pin 含义时
找不到正确文档，按旧 MPN 的 datasheet 焊会出问题。

LCSC 标准 URL 模式：
    https://www.lcsc.com/datasheet/C<lcsc_id>.pdf

用法（模块）:
    from download_datasheet import download_datasheet, project_datasheets_dir

    pdf_path = download_datasheet(
        lcsc_id="C3819933",
        mpn="1868076",
        save_dir=project_datasheets_dir(py_file_path)
    )

或命令行:
    python download_datasheet.py <lcsc_id> <mpn> --out-dir <path>
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
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

# Shared workspace discovery: pcbenv at the repo root.
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
import pcbenv


# LCSC 的 /datasheet/Cxxx.pdf 是 HTML landing page，真 PDF 在
# datasheet.lcsc.com/datasheet/pdf/<hash>.pdf?productCode=Cxxx
_LCSC_LANDING_URL = "https://www.lcsc.com/datasheet/{lcsc_id}.pdf"
_LCSC_PDF_PAT = re.compile(
    r'https://datasheet\.lcsc\.com/datasheet/pdf/[a-f0-9]+\.pdf\?productCode=C\d+'
)


def _resolve_lcsc_pdf_url(lcsc_id: str) -> Optional[str]:
    """抓 LCSC landing page，从 HTML 里提真 PDF URL。"""
    url = _LCSC_LANDING_URL.format(lcsc_id=lcsc_id)
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36",
        })
        with urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  ⚠ 抓 LCSC landing 失败 {lcsc_id}: {e}", file=sys.stderr)
        return None
    m = _LCSC_PDF_PAT.search(html)
    return m.group(0) if m else None


def project_datasheets_dir(py_file: Path) -> Path:
    """从 .py 路径找项目的 datasheets/ 目录。

    约定：项目结构
        Projects/<name>/
            ├── datasheets/    ← 这里
            ├── kicad/
            │   └── <name>.py
            └── CLAUDE.md

    从 .py 往上找，直到找到包含 datasheets/ 的目录。找不到 → 回退到
    workspace 共享 lib_external/datasheets/。
    """
    p = py_file.resolve().parent
    for _ in range(5):  # 最多往上找 5 层
        ds = p / "datasheets"
        if ds.exists() and ds.is_dir():
            return ds
        if p == p.parent:
            break
        p = p.parent

    # Fallback：workspace 共享
    fallback = pcbenv.workspace_root() / "lib_external" / "datasheets"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def download_datasheet(lcsc_id: str, mpn: str, save_dir: Path,
                       overwrite: bool = False) -> Optional[Path]:
    """下载 LCSC datasheet。返回保存路径，失败返回 None。

    文件名约定：`<MPN>_<LCSC_ID>_datasheet.pdf`，便于 grep + 唯一性。
    """
    if not save_dir.exists():
        save_dir.mkdir(parents=True, exist_ok=True)

    safe_mpn = mpn.replace("/", "_").replace(" ", "_")
    out_path = save_dir / f"{safe_mpn}_{lcsc_id}_datasheet.pdf"
    if out_path.exists() and not overwrite:
        return out_path

    pdf_url = _resolve_lcsc_pdf_url(lcsc_id)
    if not pdf_url:
        print(f"  ⚠ {lcsc_id} landing 页里找不到真 PDF URL",
              file=sys.stderr)
        return None

    try:
        req = Request(pdf_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36",
            "Accept": "application/pdf,*/*",
            "Referer": _LCSC_LANDING_URL.format(lcsc_id=lcsc_id),
        })
        with urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception as e:
        print(f"  ⚠ 下 datasheet 失败 {lcsc_id}: {e}", file=sys.stderr)
        return None

    if not data or not data[:4] == b"%PDF":
        print(f"  ⚠ {lcsc_id} 返回非 PDF 内容（{len(data)} bytes，前缀 {data[:8]!r}）",
              file=sys.stderr)
        return None

    out_path.write_bytes(data)
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lcsc_id", help="LCSC ID（如 C3819933）")
    ap.add_argument("mpn", help="MPN（如 1868076）")
    ap.add_argument("--out-dir", help="保存目录；默认 lib_external/datasheets/")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    save_dir = Path(args.out_dir) if args.out_dir else \
               pcbenv.workspace_root() / "lib_external" / "datasheets"

    p = download_datasheet(args.lcsc_id, args.mpn, save_dir, args.overwrite)
    if p:
        print(f"✅ {p}")
    else:
        print("❌ 下载失败", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
