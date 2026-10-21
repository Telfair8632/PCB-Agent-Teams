#!/usr/bin/env python3
"""clean_orphan_datasheets — Phase 2.5 强制自动跑。

规则：`Projects/<name>/datasheets/*.pdf` 里的每一个 PDF 必须对应当前
`datasheets/component_selecting/<MPN>.json` evidence 中的真件。

理由：MPN swap 之后旧 PDF 残留 → 项目目录污染、bom-readiness 报 orphan、
user 误以为还有那件、未来对话误用旧 datasheet 引用。`datasheets/` 必须
是**当前 frozen BOM 的 ground truth**，不是历史归档。

工作流：
  1. 读所有 evidence JSON，从 `datasheet.path` 字段建"应该存在"的 PDF 集合
  2. 扫 `datasheets/*.pdf`，识别孤儿（不在集合里）
  3. default: 打印孤儿列表 + exit 0（dry-run，安全）
  4. `--apply`: 真删除孤儿 PDF + exit 0（破坏性）
  5. 任意 evidence MPN 缺 PDF（且 datasheet.status != blocked_by_vendor）→ 警告

用法：
  python3 clean_orphan_datasheets.py Projects/<name>             # 干跑
  python3 clean_orphan_datasheets.py Projects/<name> --apply     # 实删

Phase 2.5 step ① 抓完 datasheet 后**强制以 --apply 跑这个脚本**，由 SKILL.md
约束 LLM 不能跳。
"""
from __future__ import annotations


# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import argparse
import json
import sys
from pathlib import Path


def _safe_filename(mpn: str) -> str:
    import re
    return re.sub(r"_+", "_", re.sub(r"[/\\:*?\"<>|,;\s]", "_", mpn)).strip("_")


def collect_expected_pdfs(project_dir: Path) -> tuple[set[Path], list[dict]]:
    """从所有 evidence JSON 收集应该存在的 PDF 路径集合。

    匹配优先级（任一成立 → expected）：
      1. evidence `datasheet.path` 字段直接命中（绝对路径或工作区相对路径）
      2. `datasheets/<safe_filename(mpn)>.pdf` 存在（针对 MPN/path 不一致的 case）
      3. `datasheets/<safe_filename(mpn_lookup)>.pdf` 存在（lookup 字段兜底）
    """
    ev_dir = project_dir / "datasheets" / "component_selecting"
    ds_dir = project_dir / "datasheets"
    expected: set[Path] = set()
    missing: list[dict] = []
    if not ev_dir.is_dir():
        return expected, missing
    for ev_path in sorted(ev_dir.glob("*.json")):
        try:
            ev = json.loads(ev_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "longlist" in ev or "shortlist" in ev:
            continue
        ds_block = ev.get("datasheet") or {}
        ds_path_str = ds_block.get("path")
        ds_status = ds_block.get("status")
        mpn = ev.get("mpn") or ev_path.stem
        mpn_lookup = ev.get("mpn_lookup") or mpn

        candidates: list[Path] = []
        if ds_path_str:
            ps = ds_path_str
            candidates.append(Path(ps).resolve() if Path(ps).is_absolute() else (Path.cwd() / ps).resolve())
        candidates.append((ds_dir / f"{_safe_filename(mpn)}.pdf").resolve())
        candidates.append((ds_dir / f"{_safe_filename(mpn_lookup)}.pdf").resolve())

        hit = next((c for c in candidates if c.exists()), None)
        if hit:
            expected.add(hit)
        elif ds_status in ("blocked_by_vendor", "missing", "todo"):
            pass  # acknowledged missing — not an orphan, not an error
        else:
            missing.append({
                "mpn": mpn,
                "expected_path": ds_path_str,
                "ds_status": ds_status,
            })
    return expected, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean orphan datasheets PDFs from project")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="Actually delete orphans (default: dry-run)")
    parser.add_argument("--quiet", action="store_true", help="Only print summary + json")
    args = parser.parse_args()

    if not args.project_dir.is_dir():
        print(f"error: project not found: {args.project_dir}", file=sys.stderr)
        return 2

    proj = args.project_dir.resolve()
    ds_dir = proj / "datasheets"
    if not ds_dir.is_dir():
        print(f"info: no datasheets/ dir at {ds_dir} — nothing to do")
        return 0

    expected, missing = collect_expected_pdfs(proj)
    actual = {p.resolve() for p in ds_dir.glob("*.pdf")}
    orphans = sorted(actual - expected)

    if not args.quiet:
        print(f"=== clean_orphan_datasheets: {proj.name} ===")
        print(f"  expected (from evidence): {len(expected)} PDFs")
        print(f"  actual (in datasheets/):  {len(actual)} PDFs")
        print(f"  orphans (to remove):      {len(orphans)}")
        if missing:
            print(f"  evidence MPN missing PDF: {len(missing)}")
            for m in missing[:5]:
                print(f"    - {m['mpn']} (status={m['ds_status']!r})")

    if not orphans:
        if not args.quiet:
            print("\n✅ no orphans — datasheets/ matches BOM")
        return 0

    if not args.quiet:
        print("\nOrphan PDFs:")
        for p in orphans:
            print(f"  - {p.name}")

    if not args.apply:
        if not args.quiet:
            print("\n[DRY-RUN] re-run with --apply to delete.")
        return 0

    deleted = []
    for p in orphans:
        try:
            p.unlink()
            deleted.append(p.name)
        except Exception as exc:
            print(f"  ! failed to delete {p}: {exc}", file=sys.stderr)
    if not args.quiet:
        print(f"\n✅ deleted {len(deleted)} orphan PDF(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
