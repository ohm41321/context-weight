#!/usr/bin/env python3
"""Install the context-weight skill where agent harnesses look for skills.

  python3 install.py                  link into your personal skill folders (all repos)
  python3 install.py --copy           copy instead of link
  python3 install.py --project DIR    copy into one repo, so it can be committed with it
  python3 install.py --uninstall      remove what this script installed

Personal folders:  ~/.agents/skills  read by Codex, Gemini CLI, OpenCode
                   ~/.claude/skills  read by Claude Code and OpenCode ($CLAUDE_CONFIG_DIR honoured)
Repo folders:      DIR/.agents/skills and DIR/.claude/skills

Never replaces a folder it did not install: an existing entry is replaced only when it is a
symlink or a copy whose SKILL.md is named context-weight. Works on macOS, Linux and Windows
(on Windows, linking falls back to a copy when symlinks are not allowed).
"""
from __future__ import annotations

import argparse
import os
import re
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


def ours(entry: Path) -> bool:
    """True when entry is something this script may replace or remove."""
    if entry.is_symlink():
        return True
    skill = entry / "SKILL.md"
    try:
        text = skill.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    return re.search(r"^name:\s*['\"]?context-weight['\"]?\s*$", text, re.M) is not None


def remove(entry: Path) -> None:
    if entry.is_symlink() or entry.is_file():
        entry.unlink()
    else:
        shutil.rmtree(entry)


def copy(dest: Path) -> None:
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def install_one(folder: Path, mode: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / NAME
    if entry.is_symlink() or entry.exists():
        if not ours(entry):
            return f"skip {entry}: exists and was not installed by context-weight"
        if mode == "link" and entry.is_symlink() and entry.resolve() == SRC:
            return f"ok   {entry} -> {SRC}"
        remove(entry)
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
    if not (entry.is_symlink() or entry.exists()):
        return None
    if not ours(entry):
        return f"skip {entry}: was not installed by context-weight"
    remove(entry)
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

    for folder in targets:
        msg = uninstall_one(folder) if a.uninstall else install_one(folder, mode)
        if msg:
            print(msg)
    if not a.uninstall:
        print("Restart the agent (or reload skills) to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
