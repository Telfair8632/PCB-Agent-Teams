#!/usr/bin/env python3
"""bootstrap.py — install this workspace's skills into every agent harness.

The fork keeps all skills in the repo's ``skills/`` directory (harness
agnostic). Each supported harness discovers skills in its own directory, so
this installer links every ``skills/<name>`` into:

    ZCode       ~/.zcode/skills/<name>
    Claude Code ~/.claude/skills/<name>

Links are junctions on Windows (no admin rights needed) and symlinks on
POSIX. Only the skills this repo owns are managed: a harness entry that is
a real directory (some other skill you installed there by hand) is never
touched, and stale links pointing at the pre-2026-10 ``.claude/skills``
layout are refreshed to the new location.

Also generates the harness-compat instruction file: AGENTS.md is the
canonical tracked copy; CLAUDE.md is regenerated from it (with a generated
header) for Claude Code, which only reads CLAUDE.md.

Usage:
    python bootstrap.py               # install/refresh all known harnesses
    python bootstrap.py --harness zcode,claude
    python bootstrap.py --list        # show plan, change nothing
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Harness id -> (home-dir marker, skills dir). A harness is only installed
# when its marker exists (the harness itself was used at least once).
HARNESSES = {
    "zcode": (Path.home() / ".zcode", Path.home() / ".zcode" / "skills"),
    "claude": (Path.home() / ".claude", Path.home() / ".claude" / "skills"),
}

GENERATED_HEADER = (
    "<!-- GENERATED from AGENTS.md by bootstrap.py — edit AGENTS.md, then\n"
    "     re-run: python bootstrap.py .  Claude Code reads CLAUDE.md only. -->\n\n"
)


def repo_skills() -> list[Path]:
    """Skill directories owned by this repo (skills/<name> with a SKILL.md)."""
    out = []
    for child in sorted((REPO_ROOT / "skills").iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            out.append(child)
    return out


def _link_kind(path: Path) -> str | None:
    """'symlink', 'junction', or None for a real directory/file.

    Works on dangling links too (lstat-based), unlike Path.exists().
    """
    if path.is_symlink():
        return "symlink"
    if os.name == "nt" and os.path.isjunction(path):
        return "junction"
    return None


def _link_target(path: Path) -> Path | None:
    """Where an existing link/junction points, or None if not a link.

    os.readlink handles both symlinks and junctions (Python >= 3.8),
    including dangling ones. Junction targets come back with a verbatim
    ``\\\\?\\`` (or ``\\\\?\\UNC\\``) prefix — strip it so callers can
    compare against plain paths.
    """
    try:
        raw = os.readlink(path)
    except (OSError, ValueError):
        return None
    if isinstance(raw, bytes):
        return Path(raw.decode("utf-8", errors="replace"))
    if raw.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + raw[8:])
    if raw.startswith("\\\\?\\"):
        return Path(raw[4:])
    return Path(raw)


def _make_link(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_link(link: Path) -> None:
    if os.name == "nt":
        # Junctions/symlink dirs must be removed with rmdir, never recursively.
        os.rmdir(link)
    else:
        link.unlink()


def install_harness(harness_id: str, skills: list[Path], apply: bool) -> int:
    marker, skills_dir = HARNESSES[harness_id]
    if not marker.exists():
        print(f"[{harness_id}] skip — {marker} not found (harness not in use)")
        return 0
    changes = 0
    for src in skills:
        dst = skills_dir / src.name
        kind = _link_kind(dst)
        if kind:
            current = _link_target(dst)
            if current and current.resolve() == src.resolve():
                continue  # already correct
            print(f"[{harness_id}] refresh {dst.name}: {current} -> {src}")
            if apply:
                _remove_link(dst)
                _make_link(src, dst)
            changes += 1
        elif dst.exists():
            print(f"[{harness_id}] KEEP  {dst.name} — real directory, not owned "
                  f"by this repo; resolve manually if you want it linked")
            continue
        else:
            print(f"[{harness_id}] link  {dst.name} -> {src}")
            if apply:
                skills_dir.mkdir(parents=True, exist_ok=True)
                _make_link(src, dst)
            changes += 1
    if apply and changes:
        print(f"[{harness_id}] {changes} change(s) applied")
    return changes


def generate_claude_compat(apply: bool) -> int:
    """Regenerate root CLAUDE.md from AGENTS.md (AGENTS.md is canonical)."""
    agents = REPO_ROOT / "AGENTS.md"
    claude = REPO_ROOT / "CLAUDE.md"
    if not agents.is_file():
        return 0
    wanted = GENERATED_HEADER + agents.read_text(encoding="utf-8")
    if claude.is_file() and claude.read_text(encoding="utf-8") == wanted:
        return 0
    print("[compat] regenerate CLAUDE.md from AGENTS.md")
    if apply:
        claude.write_text(wanted, encoding="utf-8", newline="\n")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--harness", default=",".join(HARNESSES),
                    help="comma-separated harness ids (default: all known)")
    ap.add_argument("--list", action="store_true",
                    help="show the plan without changing anything")
    args = ap.parse_args()

    apply = not args.list
    skills = repo_skills()
    if not skills:
        print("no skills found under skills/ — wrong workspace?", file=sys.stderr)
        return 1
    print(f"{len(skills)} repo skills: {', '.join(s.name for s in skills)}")

    total = 0
    for hid in [h.strip() for h in args.harness.split(",") if h.strip()]:
        if hid not in HARNESSES:
            print(f"unknown harness: {hid} (known: {', '.join(HARNESSES)})",
                  file=sys.stderr)
            return 1
        total += install_harness(hid, skills, apply)
    total += generate_claude_compat(apply)

    if not apply:
        print("\n(dry run — re-run without --list to apply)")
    elif total == 0:
        print("everything already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
