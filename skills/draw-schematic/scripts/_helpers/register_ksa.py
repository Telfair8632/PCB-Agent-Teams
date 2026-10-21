"""Register lib_external/components.kicad_sym into kicad-sch-api's symbol cache.

Why this helper exists
----------------------
kicad-sch-api's `get_symbol_info("components:X")` does NOT honor KiCad's global
sym-lib-table or the KICAD_SYMBOL_DIR env var. Symbols vendored into
`lib_external/components.kicad_sym` come back as "library not found" unless we
register the library path explicitly.

Without this every part committed via component-selecting reports
`py_pin_count=0` and trips the PIN_COUNT_MISMATCH gate in verify_bom — and any
other downstream script (pipeline.py, fix_labels.py, add_pwr_flags.py) that
calls ksa with a `components:` lib_id silently misbehaves the same way.

Usage
-----
    from _helpers.register_ksa import register
    register()                    # idempotent, safe to call repeatedly
    # ...now ksa.get_symbol_info("components:AMC1311BDWVR") works.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Workspace root via shared pcbenv (KICAD_ROOT env override honored there).
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
import pcbenv

KICAD_ROOT = pcbenv.workspace_root()
COMPONENTS_LIB = KICAD_ROOT / "lib_external" / "components.kicad_sym"

_REGISTERED = False


def register() -> bool:
    """Add lib_external/components.kicad_sym to ksa's SymbolLibraryCache.

    Returns True on success (or if already registered this process), False if
    ksa is unavailable or registration failed. Never raises — callers should
    treat ksa lookups as best-effort.
    """
    global _REGISTERED
    if _REGISTERED:
        return True
    try:
        import kicad_sch_api as ksa  # local import: helper must not hard-require ksa
    except Exception:
        return False
    if not COMPONENTS_LIB.exists():
        return False
    try:
        ksa.get_symbol_cache().add_library_path(str(COMPONENTS_LIB))
        _REGISTERED = True
        return True
    except Exception:
        return False
