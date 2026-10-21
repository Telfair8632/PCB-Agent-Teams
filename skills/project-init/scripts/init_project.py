#!/usr/bin/env python3
"""新建 PCB 项目骨架。

用法：
    python init_project.py <project_name> [--goal "一句话目标"]

行为：
    Projects/<name>/
    ├── CLAUDE.md            ← 从模板渲染（决策快照，static compass）
    ├── STATUS.md            ← live dashboard（phase 进度 + artifact 索引 + change log）
    ├── .gitignore
    ├── datasheets/          ← component-preparing 写 evidence + sentinel
    ├── kicad/               ← circuit-synth .py + 生成的 sch/pcb
    ├── reference_designs/
    ├── layout/
    └── docs/

如目录已存在则报错退出（不覆盖）。
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
import sys
from datetime import date
from pathlib import Path

# Shared workspace discovery: pcbenv at the repo root.
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
import pcbenv

KICAD_ROOT = pcbenv.workspace_root()
SUBDIRS = ["datasheets", "kicad", "reference_designs", "layout", "docs"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Init a new PCB project skeleton.")
    parser.add_argument("name", help="项目名（用小写下划线，如 voltage_sensor_400v）")
    parser.add_argument(
        "--goal",
        default=None,
        help="一句话项目目标（可选，没填留占位）",
    )
    parser.add_argument(
        "--projects-root",
        default=str(KICAD_ROOT / "Projects"),
        help="Projects 根目录（默认 PCB-Agent-Teams/Projects）",
    )
    args = parser.parse_args()

    name = args.name.strip()
    if not name or "/" in name or name.startswith("."):
        print(f"❌ 项目名非法: {name!r}", file=sys.stderr)
        return 2

    root = Path(args.projects_root) / name
    if root.exists():
        print(f"❌ 已存在: {root}", file=sys.stderr)
        return 2

    templates_dir = Path(__file__).parent.parent / "templates"
    claude_template = templates_dir / "CLAUDE.md.tmpl"
    status_template = templates_dir / "STATUS.md.tmpl"
    for tmpl in (claude_template, status_template):
        if not tmpl.exists():
            print(f"❌ 模板缺失: {tmpl}", file=sys.stderr)
            return 1

    root.mkdir(parents=True)
    for sub in SUBDIRS:
        (root / sub).mkdir()

    today = date.today().isoformat()
    goal_text = args.goal or "(待 circuit-design 后填写)"

    def render(tmpl_path: Path) -> str:
        return (
            tmpl_path.read_text(encoding="utf-8")
            .replace("{{name}}", name)
            .replace("{{goal}}", goal_text)
            .replace("{{date}}", today)
        )

    (root / "CLAUDE.md").write_text(render(claude_template), encoding="utf-8")
    (root / "STATUS.md").write_text(render(status_template), encoding="utf-8")
    (root / ".gitignore").write_text(
        "\n".join([
            "# OS / editor noise",
            ".DS_Store",
            "",
            "# Python cache",
            "__pycache__/",
            "*.py[cod]",
            "",
            "# KiCad local UI/history cache",
            "kicad/**/.history/",
            "kicad/**/*.kicad_prl",
            "",
            "# Component-selection scratch space",
            "datasheets/component_selecting/_scratch/",
            "datasheets/component_selecting/_pending_*.json",
            "",
            "# Archived/temporary datasheets",
            "datasheets/_archive/",
            "",
        ]),
        encoding="utf-8",
    )

    print(f"✓ 项目骨架已创建: {root}")
    print(f"  子目录: {', '.join(SUBDIRS)}")
    print(f"  CLAUDE.md: {root / 'CLAUDE.md'}（决策快照，static）")
    print(f"  STATUS.md: {root / 'STATUS.md'}（live 进度 dashboard）")
    print(f"  .gitignore: {root / '.gitignore'}")
    print()
    print("下一步：")
    print(f"  1. 用 circuit-design 跟用户讨论电路")
    print(f"  2. 把 BOM + ASCII 拓扑写进 {root.name}/CLAUDE.md，并把 STATUS.md Phase 1 标 ✅")
    print(f"  3. 用 component-selecting → component-preparing 出 BOM gate")
    print(f"  4. 用 draw-schematic 生成原理图")
    return 0


if __name__ == "__main__":
    sys.exit(main())
