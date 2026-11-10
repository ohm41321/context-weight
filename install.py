#!/usr/bin/env python3
"""Install the context-weight skill where agent harnesses look for skills.

  python3 install.py                  link into your personal skill folders (all repos)
  python3 install.py --copy           copy instead of link
  python3 install.py --project DIR    copy into one repo, so it can be committed with it
  python3 install.py --uninstall      remove what this script installed

Personal folders:  ~/.agents/skills  read by Codex, Gemini CLI, OpenCode
                   ~/.claude/skills  read by Claude Code and OpenCode ($CLAUDE_CONFIG_DIR honoured)
Repo folders:      DIR/.agents/skills and DIR/.claude/skills

Never deletes a folder it did not create: an existing entry is replaced only when it is a
symlink or a copy this script made (it carries a .installed-by-context-weight marker), and
never when it is the source checkout itself. Works on macOS, Linux and Windows
(on Windows, linking falls back to a copy when symlinks are not allowed).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

NAME = "context-weight"
SRC = Path(__file__).resolve().parent / "skills" / NAME


def personal_targets() -> list[Path]:
    home = Path(os.path.expanduser("~"))
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    return [home / ".agents" / "skills", claude / "skills"]


def project_targets(repo: Path) -> list[Path]:
    return [repo / ".agents" / "skills", repo / ".claude" / "skills"]


MARKER = ".installed-by-context-weight"


class InstallError(Exception):
    pass


def real(p: Path) -> Path:
    return Path(os.path.realpath(p))


def overlaps_source(entry: Path) -> bool:
    """entry is the source checkout, sits inside it, or contains it. Never delete or copy over it."""
    r, src = real(entry), real(SRC)
    return r == src or src in r.parents or r in src.parents


def ours(entry: Path) -> bool:
    """A copy this script made: it carries MARKER. A folder without it belongs to the user."""
    try:
        return entry.is_dir() and not entry.is_symlink() and (entry / MARKER).is_file()
    except OSError:
        return False


def copy(dest: Path) -> None:
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", MARKER))
    (dest / MARKER).write_text("Installed by context-weight install.py. `install.py --uninstall` removes this folder.\n",
                               encoding="utf-8")


def install_one(folder: Path, mode: str) -> str:
    entry = folder / NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise InstallError(f"cannot create {folder}: {e}")
    if entry.is_symlink():
        if mode == "link" and real(entry) == real(SRC):
            return f"ok   {entry} -> {SRC}"
        entry.unlink()  # a link holds no data; replace it
    elif overlaps_source(entry):
        return f"skip {entry}: that is the source checkout itself"
    elif entry.exists():
        if not ours(entry):
            return (f"skip {entry}: exists and was not installed by install.py "
                    "(move it away to replace it)")
        shutil.rmtree(entry)
    if mode == "link":
        try:
            entry.symlink_to(SRC, target_is_directory=True)
            return f"link {entry} -> {SRC}"
        except (OSError, NotImplementedError):
            pass  # Windows without symlink rights: fall back to a copy
    copy(entry)
    return f"copy {entry}"


def uninstall_one(folder: Path) -> str | None:
    entry = folder / NAME
    if entry.is_symlink():
        entry.unlink()
        return f"removed {entry}"
    if not entry.exists():
        return None
    if overlaps_source(entry) or not ours(entry):
        return f"skip {entry}: was not installed by install.py"
    shutil.rmtree(entry)
    return f"removed {entry}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="install.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--copy", action="store_true", help="copy instead of symlink")
    ap.add_argument("--project", metavar="DIR", help="install into this repo (always a copy)")
    ap.add_argument("--uninstall", action="store_true", help="remove what this script installed")
    a = ap.parse_args(argv)

    if not (SRC / "SKILL.md").is_file():
        print(f"install.py: error: {SRC / 'SKILL.md'} not found; run from a full checkout", file=sys.stderr)
        return 2
    if a.project:
        repo = Path(a.project).expanduser().resolve()
        if not repo.is_dir():
            print(f"install.py: error: no such directory: {a.project}", file=sys.stderr)
            return 2
        targets, mode = project_targets(repo), "copy"
    else:
        targets, mode = personal_targets(), "copy" if a.copy else "link"

    failed = False
    for folder in targets:
        try:
            msg = uninstall_one(folder) if a.uninstall else install_one(folder, mode)
        except (InstallError, OSError) as e:
            print(f"install.py: error: {e}", file=sys.stderr)
            failed = True
            continue
        if msg:
            print(msg)
    if not a.uninstall and not failed:
        print("Restart the agent (or reload skills) to pick it up.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
