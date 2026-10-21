"""Cross-skill import setup for renderer modules.

Adds the kidoc ``scripts/`` directory and the check-schematic / check-pcb
``scripts/`` directories to ``sys.path`` so that ``sexp_parser``
(and other kicad utilities) can be imported.

After 2026-05 skill consolidation, kidoc lives at
``skills/release/scripts/kidoc/`` and the kicad parser utilities
(``sexp_parser`` etc.) were duplicated into both ``check-schematic/scripts/``
and ``check-pcb/scripts/``.

Usage (at module level in any renderer)::

    from figures.renderers._path_setup import setup_kicad_imports
    setup_kicad_imports()
"""

from __future__ import annotations

import os
import sys

_setup_done = False


def setup_kicad_imports() -> None:
    """Add kidoc/ and check-* scripts/ to sys.path."""
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    # _path_setup.py lives at .../release/scripts/kidoc/figures/renderers/_path_setup.py
    # renderers/ -> figures/ -> kidoc/
    kidoc_dir = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    if kidoc_dir not in sys.path:
        sys.path.insert(0, kidoc_dir)

    # kidoc/ -> scripts/ -> release/ -> skills/
    skills_root = os.path.dirname(os.path.dirname(os.path.dirname(kidoc_dir)))
    for sibling in ("check-schematic", "check-pcb"):
        candidate = os.path.join(skills_root, sibling, "scripts")
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)
