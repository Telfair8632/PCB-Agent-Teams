"""pcbenv — one environment-discovery module for every PCB-Agent-Teams script.

Before this module each skill shipped its own copy of "find kicad-cli",
"find KiCad's bundled python", "resolve the workspace .venv", "walk up to the
project CLAUDE.md" — seven CLI finders, four python finders, six venv
resolvers, three CLAUDE.md walks — and they disagreed (ascending vs
descending version sort, env-var support vs none, POSIX-only venv paths).
This module is the single source of truth.

Canonical precedence (documented once, shared everywhere):
  explicit argument  >  environment variable  >  PATH  >  known install
  roots (newest version first)  >  command wrappers (flatpak / snap run).

Import bootstrap for a script at any depth — works from real paths AND from
harness junctions such as ``~/.claude/skills/<skill>/...`` because
``Path.resolve()`` follows junctions/symlinks back to this repo::

    import sys
    from pathlib import Path
    for _p in Path(__file__).resolve().parents:
        if (_p / "pcbenv.py").is_file():
            sys.path.insert(0, str(_p))
            break
    else:
        raise ImportError("pcbenv.py not found above " + __file__)
    import pcbenv

Stdlib only — must import identically under the workspace venv, a system
python, and KiCad's bundled interpreter.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The workspace root is where this module lives (repo root: CLAUDE.md +
# .venv + .claude/skills). Computed from the *resolved* file so junctioned
# installs land here too.
ROOT = Path(__file__).resolve().parent

# KiCad installs on a custom drive (fork default: F:\KiCad flat layout).
_CUSTOM_DRIVE_ROOTS = [r"F:\KiCad"]


def configure_stdio_utf8() -> None:
    """Reconfigure stdout/stderr to UTF-8 (cp936 consoles/pipes otherwise
    crash on status glyphs). Idempotent — safe to call repeatedly."""
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


# Importing pcbenv implies script-style execution — apply the guard once,
# matching the per-entry-script bootstrap every pipeline already carries.
configure_stdio_utf8()


# ---------------------------------------------------------------------
# Workspace / skills roots
# ---------------------------------------------------------------------

def workspace_root(root: str | os.PathLike | None = None) -> Path:
    """Workspace root (repo root with CLAUDE.md + .venv).

    Honors the KICAD_ROOT env var as an override for relocated layouts.
    """
    if root is not None:
        return Path(root).resolve()
    env = os.environ.get("KICAD_ROOT")
    if env:
        return Path(env).resolve()
    return ROOT


def skills_root(root: str | os.PathLike | None = None) -> Path:
    """Directory holding the skill folders (check-pcb, draw-pcb, ...)."""
    ws = workspace_root(root)
    for rel in (".claude/skills", "skills"):
        cand = ws / rel
        if cand.is_dir():
            return cand
    return ws / ".claude/skills"


# ---------------------------------------------------------------------
# Version-aware globbing
# ---------------------------------------------------------------------

def version_sorted(paths, descending: bool = False) -> list:
    """Sort path-like values by the numeric tokens inside them, so
    ``KiCad/10.0`` ranks above ``KiCad/9.0`` (lexicographic sort would
    put 9.0 last and pick the *oldest* install)."""
    return sorted(
        paths,
        key=lambda s: [int(t) for t in re.findall(r"\d+", str(s))],
        reverse=descending,
    )


def _version_glob(pattern: str) -> list[str]:
    """glob.glob + newest-first numeric sort."""
    return version_sorted(glob.glob(pattern), descending=True)


def _program_files_roots() -> list[str]:
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        val = os.environ.get(var)
        if val:
            roots.append(val)
    return roots


# ---------------------------------------------------------------------
# kicad-cli
# ---------------------------------------------------------------------

_UNIX_CLI_KNOWN = [
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/snap/kicad/current/bin/kicad-cli",
    "/snap/kicad/current/usr/bin/kicad-cli",
]


def find_kicad_cli(explicit: str | None = None) -> str | None:
    """Locate the kicad-cli executable (or a wrapper command).

    Returns a path, a multi-word wrapper command ("flatpak run ..."), or
    None. Callers must build argv via :func:`cli_argv` because the result
    may be multi-word.
    """
    if explicit:
        return str(explicit) if Path(explicit).exists() else None

    env_cli = os.environ.get("KICAD_CLI")
    if env_cli and Path(env_cli).exists():
        return env_cli

    # PATH covers apt/dnf/pacman/brew/nix-env installs and custom installs
    # whose bin/ was added to PATH (the common Windows case).
    found = shutil.which("kicad-cli")
    if found:
        return found

    system = sys.platform

    # macOS app bundles (glob: naming varies across KiCad versions)
    if system == "darwin":
        for pat in (
            "/Applications/KiCad/KiCad*.app/Contents/MacOS/kicad-cli",
            "/Applications/KiCad*.app/Contents/MacOS/kicad-cli",
        ):
            hits = [p for p in glob.glob(pat) if os.path.isfile(p)]
            if hits:
                return sorted(hits)[-1]

    for p in _UNIX_CLI_KNOWN:
        if os.path.isfile(p):
            return p

    if system == "win32" or os.name == "nt":
        # Versioned install dirs under Program Files — newest first.
        for pf in _program_files_roots():
            for hit in _version_glob(os.path.join(pf, "KiCad", "*", "bin", "kicad-cli.exe")):
                if os.path.isfile(hit):
                    return hit
        # Custom-drive installs (versioned then flat layout).
        for drive_root in _CUSTOM_DRIVE_ROOTS:
            for pat in (
                os.path.join(drive_root, "*", "bin", "kicad-cli.exe"),
                os.path.join(drive_root, "bin", "kicad-cli.exe"),
            ):
                for hit in _version_glob(pat):
                    if os.path.isfile(hit):
                        return hit
        # Chocolatey shim.
        choco = os.path.join(
            os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey"),
            "bin", "kicad-cli.exe")
        if os.path.isfile(choco):
            return choco
        # MSYS2 environments.
        for msys_root in (r"C:\msys64", r"C:\msys32"):
            for env_dir in ("mingw64", "mingw32", "ucrt64", "clang64"):
                cand = os.path.join(msys_root, env_dir, "bin", "kicad-cli.exe")
                if os.path.isfile(cand):
                    return cand

    if system == "linux":
        wrap = _check_flatpak_cli()
        if wrap:
            return wrap
        wrap = _check_snap_run_cli()
        if wrap:
            return wrap

    # Nix profiles (any platform).
    for p in (
        os.path.expanduser("~/.nix-profile/bin/kicad-cli"),
        "/nix/var/nix/profiles/default/bin/kicad-cli",
    ):
        if os.path.isfile(p):
            return p

    return None


# Wrapper command prefixes this module itself can return from discovery.
_WRAPPER_PREFIXES = ("flatpak ", "snap ")


def _is_wrapper_command(cli: str) -> bool:
    """True for multi-word wrapper commands ("flatpak run ...", "snap run
    ..."): either an explicitly known prefix, or the first word is an
    executable on PATH while the whole string is not a single file."""
    if cli.startswith(_WRAPPER_PREFIXES):
        return True
    if os.path.isfile(cli):
        return False
    first = cli.split(None, 1)[0]
    return bool(shutil.which(first))


def cli_argv(cli: str, args=()) -> list[str]:
    """Build an argv list from a find_kicad_cli() result.

    Wrapper commands ("flatpak run --command=kicad-cli org.kicad.KiCad")
    must be split into words; executable paths must not be — on Windows a
    path like ``C:\\Program Files\\KiCad\\bin\\kicad-cli.exe`` contains
    spaces but is one argv element.
    """
    parts = cli.split() if _is_wrapper_command(cli) else [cli]
    return parts + [str(a) for a in args]


def _check_flatpak_cli() -> str | None:
    if not shutil.which("flatpak"):
        return None
    try:
        result = subprocess.run(
            ["flatpak", "run", "--command=kicad-cli", "org.kicad.KiCad", "--version"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace")
        if result.returncode == 0:
            return "flatpak run --command=kicad-cli org.kicad.KiCad"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _check_snap_run_cli() -> str | None:
    if not shutil.which("snap"):
        return None
    try:
        result = subprocess.run(
            ["snap", "run", "kicad.kicad-cli", "--version"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace")
        if result.returncode == 0:
            return "snap run kicad.kicad-cli"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


# ---------------------------------------------------------------------
# KiCad bundled Python (has pcbnew; the workspace .venv does not)
# ---------------------------------------------------------------------

_UNIX_PY_KNOWN = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/3.9/bin/python3.9",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/3.10/bin/python3.10",
    "/usr/lib/kicad/python3",
]


def find_kicad_python(explicit: str | None = None) -> str | None:
    """Locate KiCad's bundled Python interpreter (imports pcbnew)."""
    if explicit:
        return str(explicit) if Path(explicit).exists() else None

    env_py = os.environ.get("KICAD_PYTHON")
    if env_py and Path(env_py).exists():
        return env_py

    for p in _UNIX_PY_KNOWN:
        if os.path.isfile(p):
            return p

    if os.name == "nt":
        roots = [os.path.join(pf, "KiCad") for pf in _program_files_roots()]
        roots += _CUSTOM_DRIVE_ROOTS
        for base in roots:
            for pat in (os.path.join(base, "*", "bin", "python.exe"),
                        os.path.join(base, "bin", "python.exe")):
                for hit in _version_glob(pat):
                    if os.path.isfile(hit):
                        return hit
    return None


# ---------------------------------------------------------------------
# Workspace .venv
# ---------------------------------------------------------------------

def venv_relpath(os_name: str) -> str:
    """Interpreter path inside a venv, relative to the venv dir, for the
    given os.name ('nt' → Scripts/python.exe, else bin/python)."""
    if os_name == "nt":
        return os.path.join("Scripts", "python.exe")
    return os.path.join("bin", "python")


def venv_python(root: str | os.PathLike | None = None) -> str:
    """Workspace .venv interpreter path (existence not guaranteed —
    check with venv_exists() or Path(...).exists())."""
    return str(workspace_root(root) / ".venv" / venv_relpath(os.name))


def venv_exists(root: str | os.PathLike | None = None) -> bool:
    return Path(venv_python(root)).is_file()


# ---------------------------------------------------------------------
# KiCad share dirs (symbols/ / footprints/ live under share/kicad)
# ---------------------------------------------------------------------

_UNIX_SHARE_KNOWN = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
    "/usr/share/kicad",
    "/usr/local/share/kicad",
    "/snap/kicad/current/share/kicad",
]


def kicad_share_dirs() -> list[Path]:
    """All existing KiCad data roots (…/share/kicad), newest version first."""
    out: list[Path] = []
    for p in _UNIX_SHARE_KNOWN:
        if os.path.isdir(p):
            out.append(Path(p))
    if os.name == "nt":
        bases = [os.path.join(pf, "KiCad") for pf in _program_files_roots()]
        bases += _CUSTOM_DRIVE_ROOTS
        for base in bases:
            for pat in (os.path.join(base, "*", "share", "kicad"),
                        os.path.join(base, "share", "kicad")):
                for hit in _version_glob(pat):
                    if os.path.isdir(hit) and Path(hit) not in out:
                        out.append(Path(hit))
    return out


def find_kicad_subdir(name: str) -> Path | None:
    """First existing <share/kicad>/<name> dir (e.g. 'symbols',
    'footprints', 'plugins')."""
    for share in kicad_share_dirs():
        cand = share / name
        if cand.is_dir():
            return cand
    return None


# ---------------------------------------------------------------------
# Project CLAUDE.md
# ---------------------------------------------------------------------

def find_project_claude_md(project_dir: str | os.PathLike,
                           root: str | os.PathLike | None = None) -> Path | None:
    """Closest CLAUDE.md above `project_dir`, stopping at (and excluding)
    the workspace root — the workspace CLAUDE.md is a routing index, not a
    per-project spec. Falls back to a recursive search below project_dir.
    """
    ws = workspace_root(root)
    start = Path(project_dir).resolve()
    walk = start
    for _ in range(6):
        cand = walk / "CLAUDE.md"
        if cand.is_file() and walk != ws:
            return cand
        if walk.parent == walk or walk == ws:
            break
        walk = walk.parent
    for cand in sorted(start.rglob("CLAUDE.md")):
        if cand.parent != ws:
            return cand
    return None
