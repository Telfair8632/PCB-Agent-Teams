"""kicad-cli invocation helpers for kidoc.

Discovery (PATH, Flatpak, Snap, macOS app bundles, Homebrew, Nix, Windows
Program Files, Chocolatey, MSYS2, custom drives, and the KICAD_CLI env var)
lives in pcbenv at the workspace root — one canonical implementation shared
by every skill. This module keeps the kidoc-side execution helpers: running
the CLI (multi-word wrapper commands included), Flatpak-safe temp/output
redirection, and SVG / 3D export wrappers.

Zero external dependencies — Python stdlib only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Shared environment discovery lives at the workspace root.
for _p in Path(__file__).resolve().parents:
    if (_p / "pcbenv.py").is_file():
        sys.path.insert(0, str(_p))
        break
else:
    raise ImportError("pcbenv.py not found above " + __file__)
import pcbenv


def find_kicad_cli() -> str | None:
    """Find the kicad-cli executable.

    Delegates to pcbenv.find_kicad_cli(): PATH first, then Flatpak, Snap,
    macOS app bundles, Homebrew, Nix, Windows Program Files / Chocolatey /
    MSYS2 / custom drives; the KICAD_CLI env var overrides everything.

    Returns a command string that can be passed to ``_run_cli()``, or None.
    """
    return pcbenv.find_kicad_cli()


# ======================================================================
# CLI execution helpers
# ======================================================================

def is_flatpak(cli_cmd: str) -> bool:
    """Check if the cli_cmd is a Flatpak invocation."""
    return 'flatpak' in cli_cmd


def _safe_temp_dir() -> str:
    """Return a temp directory writable by Flatpak-sandboxed apps.

    Flatpak's filesystem sandbox typically only allows access to $HOME,
    /media, and /run/media — NOT /tmp.  Use a directory under $HOME
    for temp files when interacting with Flatpak apps.
    """
    cache = os.path.join(os.path.expanduser('~'), '.cache', 'kidoc', 'tmp')
    os.makedirs(cache, exist_ok=True)
    return cache


def _run_cli(cli_cmd: str, args: list[str],
             timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a kicad-cli command, handling both path and multi-word commands
    (splitting rules shared: pcbenv.cli_argv — real paths with spaces stay
    one argv element, wrapper commands get split)."""
    return subprocess.run(
        pcbenv.cli_argv(cli_cmd, args), capture_output=True, text=True,
        timeout=timeout, encoding="utf-8", errors="replace")


def kicad_cli_version(cli_cmd: str) -> str | None:
    """Get kicad-cli version string, or None if the command fails."""
    try:
        result = _run_cli(cli_cmd, ['--version'], timeout=15)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _resolve_output_path(cli_cmd: str, output_path: str) -> tuple[str, str | None]:
    """Resolve output path for Flatpak compatibility.

    If the CLI is Flatpak and the output is under /tmp, redirect to a
    Flatpak-accessible directory and return (safe_path, original_path).
    The caller should copy from safe_path to original_path after export.
    Otherwise returns (output_path, None).
    """
    if is_flatpak(cli_cmd) and output_path.startswith('/tmp'):
        safe_dir = _safe_temp_dir()
        basename = os.path.basename(output_path)
        safe_path = os.path.join(safe_dir, basename)
        return safe_path, output_path
    return output_path, None


def _resolve_output_dir(cli_cmd: str, output_dir: str) -> tuple[str, str | None]:
    """Like _resolve_output_path but for directory outputs."""
    if is_flatpak(cli_cmd) and output_dir.startswith('/tmp'):
        safe_dir = os.path.join(_safe_temp_dir(), os.path.basename(output_dir)
                                or 'kicad_export')
        os.makedirs(safe_dir, exist_ok=True)
        return safe_dir, output_dir
    return output_dir, None


def _copy_back(safe_path: str, original_path: str) -> None:
    """Copy file or directory contents from safe path back to original."""
    import shutil as _shutil
    if os.path.isdir(safe_path):
        os.makedirs(original_path, exist_ok=True)
        for f in os.listdir(safe_path):
            src = os.path.join(safe_path, f)
            dst = os.path.join(original_path, f)
            if os.path.isfile(src):
                _shutil.copy2(src, dst)
    elif os.path.isfile(safe_path):
        os.makedirs(os.path.dirname(original_path) or '.', exist_ok=True)
        _shutil.copy2(safe_path, original_path)


def export_pcb_svg(cli_cmd: str, pcb_path: str, output_path: str,
                   layers: str = 'F.Cu,F.SilkS,Edge.Cuts') -> bool:
    """Export PCB layers to SVG using kicad-cli.  Returns True on success."""
    safe_path, original = _resolve_output_path(cli_cmd, output_path)
    try:
        result = _run_cli(cli_cmd, [
            'pcb', 'export', 'svg',
            '--layers', layers,
            '--output', safe_path,
            pcb_path,
        ])
        if result.returncode == 0 and original:
            _copy_back(safe_path, original)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def export_pcb_3d(cli_cmd: str, pcb_path: str, output_path: str,
                  side: str = 'top', width: int = 2000,
                  height: int = 1500) -> bool:
    """Export PCB 3D render to PNG using kicad-cli.  Returns True on success."""
    safe_path, original = _resolve_output_path(cli_cmd, output_path)
    try:
        result = _run_cli(cli_cmd, [
            'pcb', 'render',
            '--output', safe_path,
            '--side', side,
            '--width', str(width),
            '--height', str(height),
            '--quality', 'high',
            '--background', 'transparent',
            pcb_path,
        ])
        if result.returncode == 0 and original:
            _copy_back(safe_path, original)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def export_sch_svg(cli_cmd: str, sch_path: str, output_dir: str) -> bool:
    """Export schematic to SVG using kicad-cli.  Returns True on success."""
    safe_dir, original = _resolve_output_dir(cli_cmd, output_dir)
    try:
        result = _run_cli(cli_cmd, [
            'sch', 'export', 'svg',
            '--output', safe_dir,
            sch_path,
        ])
        if result.returncode == 0 and original:
            _copy_back(safe_dir, original)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
