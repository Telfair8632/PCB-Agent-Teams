"""Render Jinja2 templates → markdown files.

Templates live in ../templates/ relative to this file.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

TEMPLATE_NAMES = ("ORDER_GUIDE.md", "coverage_matrix.md", "fab_options.md")


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def render_all(context: dict, out_dir: Path) -> dict[str, Path]:
    env = _env()
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name in TEMPLATE_NAMES:
        template = env.get_template(f"{name}.tmpl")
        rendered = template.render(**context)
        path = out_dir / name
        path.write_text(rendered, encoding="utf-8")
        written[name] = path
    return written
