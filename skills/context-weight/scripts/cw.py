#!/usr/bin/env python3
"""context-weight: weigh what an agent harness loads before any work starts,
and how large its sessions grow.

Harnesses: Claude Code, Codex, Gemini CLI, OpenCode.
Reads instruction files, skill metadata, the token-usage numbers in local
session logs and, for `report`'s before and after, the repo's git history.
Never prints message content and never changes your files; the one file it
writes is the picture you ask for with `report --svg`. To weigh the instruction
files of an older commit it copies them into a private temporary folder and
deletes it afterwards.

  cw.py files    [PATH]          instruction files each harness loads at startup
  cw.py skills   [PATH]          skill listing size against each harness's budget
  cw.py sessions                 per-project context size measured from session logs
  cw.py report   [PATH]          all of the above for one project, plus findings
                                 (--svg draws it as a picture, --open opens it)
  cw.py verify-split OLD NEW...  prove no line was lost when a file was split

Token counts for files are estimates (see est_tokens). Session numbers are what
the provider reported.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import glob
import json
import os
import posixpath
import re
import statistics
import subprocess
import sys
import tempfile
import unicodedata
import webbrowser
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

VERSION = "0.3.2"
HARNESSES = ("claude", "codex", "gemini", "opencode")

CLAUDE_FILE_WARN_CHARS = 40_000   # Claude Code startup warning: "Large CLAUDE.md will impact performance"
CLAUDE_FILE_LINES = 200           # Claude Code docs: target under 200 lines per CLAUDE.md
CLAUDE_IMPORT_DEPTH = 4           # Claude Code docs: imports recurse at most four hops
CLAUDE_DESC_CAP = 1_536           # Claude Code: description + when_to_use truncated in the listing
CLAUDE_LISTING_FRACTION = 0.01    # Claude Code: skill listing gets 1% of the context window, in characters
CHARS_PER_WINDOW_TOKEN = 4        # 1% of a 200k window = the 8,000-character fallback older docs gave
CODEX_DOC_MAX_DEFAULT = 32 * 1024 # Codex: project_doc_max_bytes default
CODEX_LISTING_FRACTION = 0.02     # Codex: skill listing gets 2% of the context window
CODEX_LISTING_FALLBACK_CHARS = 8_000  # Codex: listing budget when the window is unknown
DEFAULT_WINDOW = 200_000          # assumed when neither --window nor a log gives the window
FINDING_MIN_CUT = 2_000           # a finding needs repo instructions this far above --target
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


# --------------------------------------------------------------------------- helpers

class UsageError(Exception):
    """Bad input from the user (missing path, unreadable file). Exits 2, never 1,
    so a CI gate cannot mistake it for 'over the limit' or for 'passed'."""


def _is_file(p) -> bool:
    # Path.is_file() raises PermissionError on Python <= 3.13 when a parent folder can't be read.
    try:
        return Path(p).is_file()
    except (OSError, ValueError):
        return False


def _is_dir(p) -> bool:
    try:
        return Path(p).is_dir()
    except (OSError, ValueError):
        return False


def _exists(p) -> bool:
    try:
        return Path(p).exists()
    except (OSError, ValueError):
        return False


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _int(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError, OverflowError):  # OverflowError: 1e999 in a log reads as infinity
        return 0


def _md_files(directory: Path) -> list[Path]:
    """*.md files under directory. Follows symlinked folders (Claude Code supports them in
    .claude/rules/), visiting each real folder once so a symlink loop cannot recurse."""
    out, seen = [], set()
    for top, dirs, files in os.walk(directory, followlinks=True):
        real = os.path.realpath(top)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        out += [Path(top) / f for f in files if f.lower().endswith(".md")]
    return sorted(out)

def est_tokens(text: str) -> int:
    """Rough token count: ~3 ASCII chars per token, ~1 token per other char (Thai, CJK).

    Fitted against Claude Code logs (startup context minus a plain-project baseline)
    for one Thai-heavy and one English-heavy CLAUDE.md; within ~10% for both.
    `sessions` gives measured numbers when you need them.
    """
    n_ascii = sum(1 for c in text if ord(c) < 128)
    return round(n_ascii / 3 + (len(text) - n_ascii))


def read_text(path) -> str | None:
    """UTF-8 text with any byte-order mark removed (a BOM would hide `---` frontmatter)."""
    try:
        return Path(path).read_bytes().decode("utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return None


def home() -> Path:
    return Path(os.path.expanduser("~"))


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or home() / ".claude")


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or home() / ".codex")


_GIT_ROOT_CACHE: dict[str, Path | None] = {}


def git_root(path) -> Path | None:
    key = str(path)
    if key in _GIT_ROOT_CACHE:
        return _GIT_ROOT_CACHE[key]
    p = Path(path)
    root = None
    try:
        p = p.resolve()
        for d in [p, *p.parents]:
            if _exists(d / ".git"):
                root = d
                break
    except OSError:
        pass
    _GIT_ROOT_CACHE[key] = root
    return root


def project_root(path) -> Path:
    return git_root(path) or Path(path).resolve()


def top_down(path) -> list[Path]:
    """Filesystem root ... path."""
    p = Path(path).resolve()
    return list(reversed([p, *p.parents]))


def short(path) -> str:
    s = str(path)
    h = str(home())
    if s == h or s.startswith(h + os.sep):
        s = "~" + s[len(h):]
    return s


def fmt_k(n: float) -> str:
    n = float(n)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML frontmatter reader: top-level `key: value` plus block scalars."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val in (">", "|", ">-", "|-", ">+", "|+"):
                val = ""
            out[key] = val.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


UNREADABLE: list[str] = []  # instruction files that exist but could not be read


def read_instruction(path) -> str | None:
    """read_text for an instruction file; remembers files that exist but cannot be read,
    so a gate can refuse to pass on numbers it could not measure."""
    text = read_text(path)
    if text is None and _is_file(path) and str(path) not in UNREADABLE:
        UNREADABLE.append(str(path))
    return text


def file_entry(path, role: str, via=None) -> dict | None:
    text = read_instruction(path)
    if text is None:
        return None
    return {
        "path": str(Path(path)),
        "role": role,
        "via": str(via) if via else None,
        "bytes": len(text.encode("utf-8")),
        "chars": len(text),
        "lines": len(text.splitlines()),
        "tokens": est_tokens(text),
    }


# --------------------------------------------------------------------------- instruction files

_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")  # same pattern Claude Code 2.1.x uses
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svgz", ".pdf", ".zip", ".gz",
               ".tgz", ".tar", ".7z", ".mp3", ".mp4", ".mov", ".wav", ".webm", ".woff", ".woff2", ".ttf",
               ".otf", ".so", ".dll", ".exe", ".bin", ".npy", ".npz", ".pt", ".onnx", ".xlsx", ".docx", ".pptx"}


def _is_text(path: Path) -> bool:
    """Claude Code skips non-text files named in @imports."""
    if path.suffix.lower() in _BINARY_EXT:
        return False
    try:
        with open(path, "rb") as fh:
            return b"\0" not in fh.read(4096)
    except OSError:
        return False


def _claude_imports(path: Path, text: str, depth: int, seen: set, out: list, role: str) -> None:
    if depth > CLAUDE_IMPORT_DEPTH:
        return
    body = _INLINE.sub("", _FENCE.sub("", text))
    for m in _IMPORT.finditer(body):
        ref = m.group(1).replace("\\ ", " ")
        if ref.startswith("~"):
            target = Path(os.path.expanduser(ref))
        elif ref.startswith("/") or Path(ref).is_absolute():
            target = Path(ref)
        else:
            target = path.parent / ref
        try:
            if not _is_file(target) or not _is_text(target):
                continue
            target = target.resolve()
        except (OSError, RuntimeError):
            continue
        if str(target) in seen:
            continue
        seen.add(str(target))
        if not _holds_text(target):
            continue  # Claude Code loads nothing from a file that holds nothing
        entry = file_entry(target, f"{role} import", via=path)
        if entry:
            out.append(entry)
            _claude_imports(target, read_text(target) or "", depth + 1, seen, out, role)


def _holds_text(p) -> bool:
    return _is_file(p) and bool((read_text(p) or "").strip())


def _rules(directory: Path) -> tuple[list[Path], int]:
    """Claude Code rules: files without `paths:` frontmatter load at launch."""
    if not _is_dir(directory):
        return [], 0
    eager, scoped = [], 0
    for p in _md_files(directory):
        if "paths" in frontmatter(read_text(p) or ""):
            scoped += 1
        else:
            eager.append(p)
    return eager, scoped


# Claude Code's built-in agents-md plugin (2.1.277+; read in 2.1.322 on 2026-11-12) decides
# whether AGENTS.md loads. See references/harnesses.md.
CLAUDE_MD_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
AGENTS_MD_NAMES = ("AGENTS.md", ".claude/AGENTS.md")
AGENTS_PLUGIN_IDS = ("cc-plugin-agents-md@builtin", "agents-md@builtin")
AGENTS_FALLBACK = "claude-md-or-agents-md"
INSTRUCTION_MODES = (AGENTS_FALLBACK, "claude-md", "claude-md-and-agents-md", "managed-only")
LEGACY_INSTRUCTIONS = {"none": "managed-only", "claude": "claude-md", "agents-fallback": AGENTS_FALLBACK,
                       "both": "claude-md-and-agents-md"}
_MODE_NOTES = {
    "claude-md": "Claude Code is set to read CLAUDE.md only (instructionFiles): no AGENTS.md loads",
    "claude-md-and-agents-md": "Claude Code is set to load AGENTS.md beside CLAUDE.md (instructionFiles)",
    "managed-only": "Claude Code is set to load managed instructions only (instructionFiles): no user or "
                    "project file loads, and managed files are not read here",
}


def _instruction_mode(cwd) -> tuple[str, str | None]:
    """Which project instruction files Claude Code loads, and a note unless it is the default.
    Its agents-md plugin loads AGENTS.md where no CLAUDE.md is (claude-md-or-agents-md), never
    (claude-md), beside CLAUDE.md (claude-md-and-agents-md), or no user or project file at all
    (managed-only). /plugin can turn the plugin off in any settings file, the highest one that
    names it deciding; its options are read from user settings only."""
    root = project_root(cwd)
    for p in (root / ".claude" / "settings.local.json", root / ".claude" / "settings.json",
              claude_dir() / "settings.json"):
        enabled = _dict(_json_file(p).get("enabledPlugins"))
        key = next((k for k in AGENTS_PLUGIN_IDS if k in enabled), None)
        if key is not None:
            if enabled[key] is not True:  # false, null or anything else turns it off
                return "claude-md", f"Claude Code's agents-md plugin is off in {short(p)}: no AGENTS.md loads"
            break
    configs = _dict(_json_file(claude_dir() / "settings.json").get("pluginConfigs"))
    options = next((_dict(_dict(configs[k]).get("options")) for k in AGENTS_PLUGIN_IDS if k in configs), {})
    mode = options.get("instructionFiles")
    if mode not in INSTRUCTION_MODES:
        mode = AGENTS_FALLBACK
    if "projectInstructions" in options and mode == AGENTS_FALLBACK:  # the option's older name
        old = options["projectInstructions"]
        mode = LEGACY_INSTRUCTIONS.get(old, "claude-md") if isinstance(old, str) else "claude-md"
    return mode, _MODE_NOTES.get(mode)


def _main_checkout(root: Path) -> Path | None:
    """The main checkout of the git worktree at `root`, or None. A worktree's .git is a file that
    names its gitdir, which holds `commondir` (a submodule's gitdir has none); the common dir is
    the main checkout's .git (a bare repository has no checkout)."""
    lines = (read_text(root / ".git") or "").strip().splitlines() if _is_file(root / ".git") else []
    if not lines or not lines[0].startswith("gitdir:"):
        return None
    gitdir = Path(lines[0][len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    common = (read_text(gitdir / "commondir") or "").strip()
    if not common:
        return None
    try:
        common_dir = (Path(common) if Path(common).is_absolute() else gitdir / common).resolve()
    except (OSError, RuntimeError):
        return None
    if common_dir.name != ".git" or not _is_dir(common_dir.parent):
        return None
    return common_dir.parent


def _nested_worktree(cwd) -> tuple[Path, Path] | None:
    """(worktree root, main checkout) for a worktree inside its main checkout, as Claude Code
    makes them (.claude/worktrees/<name>); None otherwise."""
    root = git_root(cwd)
    main = _main_checkout(root) if root else None
    if main is None:
        return None
    for p in root.parents:
        if _same_dir(p, main):
            return root, p
    return None


def _claude_walk(cwd) -> list[Path]:
    """Folders Claude Code reads CLAUDE.md files and rules from, top down: the start folder and
    each one above it, not the filesystem root. In a worktree inside its main checkout it skips
    the main checkout's own folders, since the worktree holds its own copy of their files."""
    dirs = [d for d in top_down(cwd) if d != Path(d.anchor)]
    nested = _nested_worktree(cwd)
    if nested:
        wt, main = nested
        dirs = [d for d in dirs if not (_inside(d, main) and not _inside(d, wt))]
    return dirs


_ENV_ON, _ENV_OFF = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def _auto_memory_file(cwd) -> Path | None:
    """MEMORY.md of Claude Code's auto memory here, or None when it is off. One per git
    repository, shared by its worktrees and subfolders: <config dir>/projects/<project>/memory/,
    where CLAUDE_CODE_PROJECT_DIR_NAME, set beside CLAUDE_CONFIG_DIR, names <project>, and
    autoMemoryDirectory (an absolute path or ~/...) moves it."""
    switch = os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "").strip().lower()
    if switch in _ENV_ON:
        return None
    settings = _claude_settings(cwd)
    if switch not in _ENV_OFF and settings.get("autoMemoryEnabled", True) in (False, None, 0, ""):
        return None  # a set but false switch turns it on whatever the settings say
    moved = settings.get("autoMemoryDirectory")
    if isinstance(moved, str) and (os.path.isabs(moved) or moved.startswith("~/")):
        return Path(os.path.expanduser(moved)) / "MEMORY.md"
    name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if not (name and os.environ.get("CLAUDE_CONFIG_DIR")):
        root = git_root(cwd)
        main = _main_checkout(root) if root else None
        name = re.sub(r"[^A-Za-z0-9]", "-", str(_true_case(main or project_root(cwd))))
    return claude_dir() / "projects" / name / "memory" / "MEMORY.md"


def claude_files(cwd, copy_root=None, claude_md_outside=False, mode=None) -> dict:
    """What Claude Code loads at startup in `cwd`: the user's CLAUDE.md and rules; in each folder
    of _claude_walk, CLAUDE.md, .claude/CLAUDE.md, CLAUDE.local.md and .claude/rules; AGENTS.md
    as the mode says (_instruction_mode); auto memory. A file that holds nothing loads nothing.

    `copy_root`: an old commit copied there is weighed. Only folders from it down count, and
    `claude_md_outside` says whether a CLAUDE.md the copy cannot hold (above the repo, or one git
    ignores) kept AGENTS.md out; `mode` then comes from the real repo's settings."""
    out: list[dict] = []
    seen: set[str] = set()
    notes: list[str] = []
    if mode is None:
        mode, note = _instruction_mode(cwd)
        if note:
            notes.append(note)

    def add(p: Path, role: str, into: list) -> None:
        try:
            if not _is_file(p):
                return
            rp = str(p.resolve())
        except (OSError, RuntimeError):
            return
        if rp in seen:
            return
        seen.add(rp)
        text = read_instruction(p)
        if not (text and text.strip()):
            return
        entry = file_entry(p, role)
        if entry:
            into.append(entry)
            _claude_imports(Path(rp), text, 1, seen, into, role)

    cd = claude_dir()
    walk = _claude_walk(cwd)
    if copy_root is not None:
        top = Path(copy_root).resolve()
        walk = [d for d in walk if _inside(d, top)]
    scoped = 0
    if mode != "managed-only":
        add(cd / "CLAUDE.md", "user", out)
        eager, scoped = _rules(cd / "rules")
        for r in eager:
            add(r, "user rule", out)
        for d in walk:
            add(d / "CLAUDE.md", "project", out)
            add(d / ".claude" / "CLAUDE.md", "project", out)
            add(d / "CLAUDE.local.md", "local", out)
            if not _same_dir(d / ".claude", cd):  # in the home folder ~/.claude/rules are the user's
                eager, n = _rules(d / ".claude" / "rules")
                scoped += n
                for r in eager:
                    add(r, "project rule", out)
    if mode in (AGENTS_FALLBACK, "claude-md-and-agents-md"):
        user_md = cd / "CLAUDE.md"
        has_claude_md = claude_md_outside or any(
            _holds_text(d / n) and not _same_dir(d / n, user_md) for d in walk for n in CLAUDE_MD_NAMES)
        if mode != AGENTS_FALLBACK or not has_claude_md:
            # An AGENTS.md part (the file or one it imports) that is a loaded file, or holds the
            # same text as a loaded project file, is skipped.
            known = {(read_text(e["path"]) or "").strip() for e in out if not e["role"].startswith("user")}
            agents: list[dict] = []
            for d in walk:
                for n in AGENTS_MD_NAMES:
                    add(d / n, "project", agents)
            agents = [e for e in agents if (read_text(e["path"]) or "").strip() not in known]
            out += agents
            if agents and mode == AGENTS_FALLBACK:
                notes.append("no CLAUDE.md here or above, so Claude Code (2.1.277+) loads AGENTS.md instead")
    # Auto memory: Claude Code loads the start of MEMORY.md (first 200 lines / 25 KB).
    mem = _auto_memory_file(cwd) if copy_root is None else None
    text = read_instruction(mem) if mem and _is_file(mem) else None
    if text:
        head = "\n".join(text.splitlines()[:200]).encode("utf-8")[:25_000].decode("utf-8", errors="ignore")
        out.append({"path": str(mem), "role": "user memory", "via": None, "bytes": len(head.encode("utf-8")),
                    "chars": len(head), "lines": len(head.splitlines()), "tokens": est_tokens(head)})
    if scoped:
        notes.append(f"{scoped} path-scoped rule file(s) load on demand, not counted")

    warnings = []
    for e in out:
        if e["role"] in ("project", "local", "user") and e["chars"] > CLAUDE_FILE_WARN_CHARS:
            warnings.append(f"{short(e['path'])}: {e['chars']:,} chars > {CLAUDE_FILE_WARN_CHARS:,} "
                            "(Claude Code shows 'Large CLAUDE.md will impact performance')")
        elif e["role"] in ("project", "local", "user") and e["lines"] > CLAUDE_FILE_LINES:
            warnings.append(f"{short(e['path'])}: {e['lines']} lines > {CLAUDE_FILE_LINES} (docs target)")
        if e["role"].endswith("import") and e["tokens"] > 5_000:
            warnings.append(f"{short(e['path'])}: imported with @ (~{fmt_k(e['tokens'])} tokens) - "
                            "imports load at launch, they do not save context")
    return {"files": out, "warnings": warnings, "notes": notes}


_TOML_KEY = r"""(?:[\w-]+|"[^"\n]*"|'[^'\n]*')"""
_TOML_TABLE = re.compile(rf"^\s*\[\[?\s*{_TOML_KEY}(?:\s*\.\s*{_TOML_KEY})*\s*\]\]?\s*(?:#.*)?$", re.M)
_TOML_DOC_MAX = re.compile(r"^\s*project_doc_max_bytes\s*=\s*\+?(0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|\d[\d_]*)"
                           r"\s*(?:#.*)?$", re.M)


def _codex_config() -> tuple[int, list[str]]:
    text = read_text(codex_dir() / "config.toml") or ""
    max_bytes, fallbacks = CODEX_DOC_MAX_DEFAULT, []
    try:
        import tomllib  # Python 3.11+
        cfg = tomllib.loads(text)
        max_bytes = int(cfg.get("project_doc_max_bytes", max_bytes))
        fallbacks = [str(x) for x in cfg.get("project_doc_fallback_filenames", [])]
    except Exception:
        # No tomllib (Python 3.8 to 3.10), or a value it cannot use: read the two keys by hand,
        # as TOML does. Top-level keys come before the first [table] header (a key under
        # [profiles.x] is not the global limit), and an integer may carry underscores and a
        # 0x, 0o or 0b prefix.
        header = _TOML_TABLE.search(text)
        top = text[:header.start()] if header else text
        m = _TOML_DOC_MAX.search(top)
        if m:
            try:
                max_bytes = int(m.group(1).replace("_", ""), 0)
            except ValueError:
                pass
        m = re.search(r"^\s*project_doc_fallback_filenames\s*=\s*\[(.*?)\]", top, re.M | re.S)
        if m:
            fallbacks = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    return max_bytes, fallbacks


def codex_files(cwd) -> dict:
    out: list[dict] = []
    for name in ("AGENTS.override.md", "AGENTS.md"):
        p = codex_dir() / name
        if _is_file(p) and (read_text(p) or "").strip():
            out.append(file_entry(p, "global"))
            break
    out = [e for e in out if e]
    max_bytes, fallbacks = _codex_config()
    root = project_root(cwd)
    dirs = [d for d in top_down(cwd) if d == root or root in d.parents]
    used = 0
    warnings = []
    for d in dirs:
        for name in ["AGENTS.override.md", "AGENTS.md", *fallbacks]:
            p = d / name
            text = read_instruction(p) if _is_file(p) else None
            if not (text and text.strip()):
                continue
            entry = file_entry(p, "project")
            raw = text.encode("utf-8")
            kept = max(0, min(len(raw), max_bytes - used))
            used += kept
            if kept < len(raw):
                entry["truncated_bytes"] = len(raw) - kept
                entry["file_tokens"] = entry["tokens"]  # the whole file, for the --max-tokens gate
                entry["tokens"] = est_tokens(raw[:kept].decode("utf-8", errors="ignore"))
                warnings.append(f"{short(p)}: {len(raw) - kept:,} bytes past project_doc_max_bytes "
                                f"({max_bytes:,}) - Codex never sees them")
            out.append(entry)
            break  # Codex includes at most one file per directory
    return {"files": out, "warnings": warnings, "notes": [f"project_doc_max_bytes={max_bytes:,}"]}


def _gemini_names() -> list[str]:
    """Context file names Gemini CLI loads (settings `context.fileName`, default GEMINI.md)."""
    try:
        settings = json.loads(read_text(home() / ".gemini" / "settings.json") or "{}")
        fn = (settings.get("context") or {}).get("fileName") or settings.get("contextFileName")
    except (ValueError, AttributeError):
        fn = None
    if isinstance(fn, str) and fn:
        return [fn]
    names = [x for x in fn if isinstance(x, str) and x] if isinstance(fn, list) else []
    return names or ["GEMINI.md"]  # a number, true or an object is not a file name


def gemini_files(cwd) -> dict:
    gd = home() / ".gemini"
    names = _gemini_names()
    out, seen = [], set()

    def add(p: Path, role: str) -> None:
        if _is_file(p) and str(p.resolve()) not in seen:
            seen.add(str(p.resolve()))
            e = file_entry(p, role)
            if e:
                out.append(e)

    for n in names:
        add(gd / n, "global")
    # Gemini CLI walks up from the working directory to the project root (the folder with .git).
    root = git_root(cwd)
    dirs = top_down(cwd)
    if root:
        dirs = [d for d in dirs if d == root or root in d.parents]
    for d in dirs:
        for n in names:
            add(d / n, "project")
    return {"files": out, "warnings": [],
            "notes": [f"context file names: {', '.join(names)}; subdirectory files load just-in-time, not counted"]}


def opencode_files(cwd) -> dict:
    oc = home() / ".config" / "opencode"
    no_claude = bool(os.environ.get("OPENCODE_DISABLE_CLAUDE_CODE"))
    out: list[dict | None] = []
    if _is_file(oc / "AGENTS.md"):
        out.append(file_entry(oc / "AGENTS.md", "global"))
    elif not no_claude and _is_file(claude_dir() / "CLAUDE.md"):
        out.append(file_entry(claude_dir() / "CLAUDE.md", "global (CLAUDE.md fallback)"))
    root = project_root(cwd)
    here = Path(cwd).resolve()
    upward = [d for d in [here, *here.parents] if d == root or root in d.parents]
    for name in ["AGENTS.md"] + ([] if no_claude else ["CLAUDE.md"]):
        hit = next((d / name for d in upward if _is_file(d / name)), None)
        if hit:
            out.append(file_entry(hit, "project" if name == "AGENTS.md" else "project (CLAUDE.md fallback)"))
            break
    for cfg_path, base in ((oc / "opencode.json", oc), (root / "opencode.json", root)):
        try:
            cfg = _dict(json.loads(read_text(cfg_path) or "{}"))
        except ValueError:
            continue
        patterns = cfg.get("instructions")
        for pattern in patterns if isinstance(patterns, list) else []:
            if not isinstance(pattern, str) or "://" in pattern:
                continue
            for m in sorted(glob.glob(str(base / os.path.expanduser(str(pattern))), recursive=True)):
                out.append(file_entry(m, "instructions"))
    return {"files": [e for e in out if e], "warnings": [], "notes": []}


FILE_SCANNERS = {"claude": claude_files, "codex": codex_files, "gemini": gemini_files, "opencode": opencode_files}


def _inside(path, root: Path) -> bool:
    p = Path(path).resolve()
    return p == root or root in p.parents


def scan_files(cwd, harnesses, copy_root=None, claude_md_outside=False, mode=None) -> dict:
    """Each harness's startup files. The other arguments are claude_files' and are given only
    when an old commit's copy is weighed."""
    root = project_root(cwd)
    result = {}
    for h in harnesses:
        r = claude_files(cwd, copy_root, claude_md_outside, mode) if h == "claude" else FILE_SCANNERS[h](cwd)
        repo = [e for e in r["files"] if _inside(e["path"], root)]
        r["total_tokens"] = sum(e["tokens"] for e in r["files"])
        r["repo_tokens"] = sum(e["tokens"] for e in repo)                          # what the harness loads
        r["repo_file_tokens"] = sum(e.get("file_tokens", e["tokens"]) for e in repo)  # what the files hold
        result[h] = r
    if UNREADABLE:
        result["unreadable"] = list(UNREADABLE)
    # Same repo, different instructions per harness? One side with none at all is the worst case,
    # so compare whenever both harnesses were measured and either side has project files. Only
    # files in the repo count: a CLAUDE.md in a parent folder loads for Claude Code alone in every
    # repo below it, and no change in this repo can share it with Codex.
    c = [e for e in result.get("claude", {}).get("files", [])
         if e["role"] in ("project", "local") and _inside(e["path"], root)]
    x = [e for e in result.get("codex", {}).get("files", []) if e["role"] == "project"]
    if "claude" in result and "codex" in result and (c or x):
        ct = sum(e["tokens"] for e in c)
        xt = sum(e["tokens"] for e in x)
        # Linked when both load one real file: an @import, a symlink, or AGENTS.md read by both.
        def real(e):
            try:
                return str(Path(e["path"]).resolve())
            except (OSError, RuntimeError):
                return e["path"]
        shared = {real(e) for e in result["claude"]["files"] if _inside(e["path"], root)} & {real(e) for e in x}
        if not shared and max(ct, xt) > 2 * max(1, min(ct, xt)):
            result.setdefault("cross", []).append(
                f"Claude Code loads ~{fmt_k(ct)} tokens of project instructions, Codex ~{fmt_k(xt)}: "
                "the harnesses follow different rules in this repo")
    return result


# --------------------------------------------------------------------------- skills

def _skill_dirs(cwd) -> dict[str, list[tuple[Path, str]]]:
    root = project_root(cwd)
    here = Path(cwd).resolve()
    upward = [d for d in [here, *here.parents] if d == root or root in d.parents]
    h = home()
    cd, xd = claude_dir(), codex_dir()

    def proj(rel: str) -> list[tuple[Path, str]]:
        return [(d / rel, "") for d in upward]

    return {
        "claude": [(cd / "skills", ""), *proj(".claude/skills"), *_claude_plugin_skill_dirs()],
        "codex": [*proj(".agents/skills"), (h / ".agents" / "skills", ""), (xd / "skills", ""),
                  (Path("/etc/codex/skills"), ""), (xd / "skills" / ".system", "")],
        "gemini": [*proj(".gemini/skills"), *proj(".agents/skills"), (h / ".gemini" / "skills", ""),
                   (h / ".agents" / "skills", "")],
        "opencode": [*proj(".opencode/skills"), *proj(".claude/skills"), *proj(".agents/skills"),
                     (h / ".config" / "opencode" / "skills", ""), (cd / "skills", ""), (h / ".agents" / "skills", "")],
    }


def _json_file(path) -> dict:
    try:
        return _dict(json.loads(read_text(path) or "{}"))
    except ValueError:
        return {}


def _claude_plugin_skill_dirs() -> list[tuple[Path, str]]:
    cd = claude_dir()
    enabled = [k for k, v in _dict(_json_file(cd / "settings.json").get("enabledPlugins")).items() if v]
    installed = _dict(_json_file(cd / "plugins" / "installed_plugins.json").get("plugins"))
    dirs = []
    for key in sorted(enabled):
        plugin, _, market = key.partition("@")
        entries = installed.get(key)
        entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
        candidates = [Path(str(e["installPath"])) for e in entries if e.get("installPath")]
        candidates += [cd / "plugins" / "cache" / market / plugin / str(e["version"])
                       for e in entries if e.get("version")]
        hit = next((c for c in candidates if _is_dir(c / "skills")), None)
        if hit:
            dirs.append((hit / "skills", plugin + ":"))
    return dirs


def _claude_settings(cwd) -> dict:
    """User, project and local settings, later files winning, as Claude Code merges them."""
    root = project_root(cwd)
    merged: dict = {}
    for p in (claude_dir() / "settings.json", root / ".claude" / "settings.json",
              root / ".claude" / "settings.local.json"):
        merged.update(_json_file(p))
    return merged


def _codex_window() -> int | None:
    """model_context_window from the newest Codex rollouts, if any."""
    files = []
    for sub in ("sessions", "archived_sessions"):
        d = codex_dir() / sub
        if _is_dir(d):
            try:
                files += list(d.rglob("rollout-*.jsonl"))
            except OSError:
                pass
    for f in sorted(files, key=lambda p: p.name, reverse=True)[:20]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"model_context_window"' not in line:
                        continue
                    m = re.search(r'"model_context_window"\s*:\s*(\d+)', line)
                    if m and int(m.group(1)) > 0:
                        return int(m.group(1))
        except OSError:
            continue
    return None


def _listing_budget(h: str, cwd, window: int | None) -> dict:
    """Budget for one harness's skill listing, with where each number came from."""
    if h == "claude":
        fixed = _int(os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET"))
        w, w_src = (window, "--window") if window else (DEFAULT_WINDOW, "default")
        if fixed > 0:
            return {"budget": fixed, "budget_unit": "chars", "window": w, "window_source": w_src,
                    "basis": "SLASH_COMMAND_TOOL_CHAR_BUDGET"}
        try:
            frac = float(_claude_settings(cwd).get("skillListingBudgetFraction") or CLAUDE_LISTING_FRACTION)
        except (TypeError, ValueError):
            frac = CLAUDE_LISTING_FRACTION
        return {"budget": int(w * CHARS_PER_WINDOW_TOKEN * frac), "budget_unit": "chars", "window": w,
                "window_source": w_src, "basis": f"{frac * 100:g}% of a {fmt_k(w)}-token window"}
    if h == "codex":
        # Codex records its own window; --window is Claude Code's and only fills in when the logs have none.
        w, w_src = _codex_window(), "codex logs"
        if not w and window:
            w, w_src = window, "--window"
        if not w:
            return {"budget": CODEX_LISTING_FALLBACK_CHARS, "budget_unit": "chars", "window": None,
                    "window_source": "unknown", "basis": "window unknown"}
        return {"budget": int(w * CODEX_LISTING_FRACTION), "budget_unit": "tokens", "window": w,
                "window_source": w_src, "basis": f"2% of a {fmt_k(w)}-token window"}
    return {"budget": None, "budget_unit": None, "window": None, "window_source": None, "basis": "no published budget"}


def scan_skills(cwd, harnesses, window: int | None = None) -> dict:
    result = {}
    all_dirs = _skill_dirs(cwd)
    desc_cap = _int(_claude_settings(cwd).get("skillListingMaxDescChars")) or CLAUDE_DESC_CAP
    for h in harnesses:
        found: dict[str, dict] = {}
        for directory, prefix in all_dirs[h]:
            if not _is_dir(directory):
                continue
            try:
                manifests = sorted(directory.glob("*/SKILL.md"))
            except OSError:
                continue
            for sm in manifests:
                if sm.parent.name.startswith("."):
                    continue
                fm = frontmatter(read_text(sm) or "")
                name = prefix + (fm.get("name") or sm.parent.name)
                if name in found:
                    continue
                desc = fm.get("description", "")
                if fm.get("when_to_use"):
                    desc += " " + fm["when_to_use"]
                if h == "claude":
                    desc = desc[:desc_cap]
                line = f"- {name}: {desc}\n"
                found[name] = {"path": str(sm), "desc_chars": len(desc), "chars": len(line),
                               "tokens": est_tokens(line)}
        tokens = sum(s["tokens"] for s in found.values())
        chars = sum(s["chars"] for s in found.values())
        b = _listing_budget(h, cwd, window)
        used = chars if b["budget_unit"] == "chars" else tokens
        over = b["budget"] is not None and used > b["budget"]
        heaviest = sorted(found.items(), key=lambda kv: -kv[1]["tokens"])[:5]
        result[h] = {"count": len(found), "tokens": tokens, "chars": chars,
                     "desc_chars": sum(s["desc_chars"] for s in found.values()), **b, "over_budget": over,
                     "heaviest": [{"name": k, "tokens": v["tokens"]} for k, v in heaviest]}
    return result


# --------------------------------------------------------------------------- sessions

class Session:
    """`continued` marks a session that resumes earlier context (after compaction);
    its first request is not a startup load, so it stays out of the baseline."""
    __slots__ = ("harness", "file", "cwd", "reqs", "window", "continued")

    def __init__(self, harness, file, cwd, reqs, window=None, continued=False):
        self.harness, self.file, self.cwd, self.reqs = harness, file, cwd, reqs
        self.window, self.continued = window, continued

    @property
    def start(self) -> str:
        return self.reqs[0][0] if self.reqs else ""


def local_day(ts: str) -> str:
    """Log timestamps are UTC; the dates users type are local."""
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


_CLAUDE_COMPACT = ('"compact_boundary"', '"isCompactSummary":true')


def claude_sessions(include_subagents=False):
    base = claude_dir() / "projects"
    pattern = "*/**/*.jsonl" if include_subagents else "*/*.jsonl"
    parsed = []
    try:
        logs = sorted(base.glob(pattern)) if _is_dir(base) else []
    except OSError:
        logs = []
    for f in logs:
        cwd, local, rows, compacted = None, set(), [], False
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if not rows and any(m in line.replace(" ", "") for m in _CLAUDE_COMPACT):
                    compacted = True  # file opens by continuing a compacted conversation
                if cwd is not None and '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                if cwd is None and isinstance(d.get("cwd"), str) and d["cwd"]:
                    cwd = d["cwd"]
                if d.get("type") != "assistant" or (d.get("isSidechain") and not include_subagents):
                    continue
                msg = _dict(d.get("message"))
                mid = msg.get("id") or d.get("requestId") or d.get("uuid")
                if not isinstance(mid, str):
                    continue
                if mid in local:  # one API response is logged once per content block
                    continue
                local.add(mid)
                u = _dict(msg.get("usage"))
                ctx = sum(_int(u.get(k)) for k in
                          ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if ctx > 0:
                    rows.append((str(d.get("timestamp", "")), ctx, mid))
        if rows and cwd:
            parsed.append((min(r[0] for r in rows), str(f), cwd, rows, compacted))
    # A continued session file repeats requests from the file it continues: count each once,
    # owned by the earliest file.
    seen: set = set()
    for _, f, cwd, rows, compacted in sorted(parsed):
        reqs = [(ts, ctx) for ts, ctx, mid in rows if mid not in seen]
        repeated = len(reqs) < len(rows)
        seen.update(mid for _, _, mid in rows)
        if reqs:
            yield Session("claude", f, cwd, reqs, continued=compacted or repeated)


def _codex_is_subagent(meta: dict) -> bool:
    """Spawned agents and guardian reviewers carry `source: {"subagent": ...}`. Older logs mark
    them with thread_source "subagent" or "guardian_review". User threads have thread_source
    "user", "chatgpt_handoff" or none, with source "cli", "vscode" and so on."""
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return True
    return meta.get("thread_source") in ("subagent", "guardian_review")


def codex_sessions(include_subagents=False):
    files = []
    for sub in ("sessions", "archived_sessions"):
        d = codex_dir() / sub
        try:
            files += sorted(d.rglob("rollout-*.jsonl")) if _is_dir(d) else []
        except OSError:
            pass
    for f in files:
        cwd, reqs, window, last_total, is_sub = None, [], None, None, False
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"token_count"' not in line and '"session_meta"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                p = _dict(d.get("payload"))
                if d.get("type") == "session_meta":
                    if not cwd and isinstance(p.get("cwd"), str):
                        cwd = p["cwd"]
                    is_sub = is_sub or _codex_is_subagent(p)
                    continue
                info = _dict(p.get("info"))
                if p.get("type") != "token_count" or not info:
                    continue
                total = _dict(info.get("total_token_usage")).get("total_tokens")
                if total is not None and total == last_total:  # repeated rate-limit update
                    continue
                last_total = total
                ctx = _int(_dict(info.get("last_token_usage")).get("input_tokens"))
                window = _int(info.get("model_context_window")) or window
                if ctx > 0:
                    reqs.append((str(d.get("timestamp", "")), ctx))
        if is_sub and not include_subagents:
            continue
        if reqs and cwd:
            yield Session("codex", str(f), cwd, reqs, window)


SESSION_READERS = {"claude": claude_sessions, "codex": codex_sessions}


def load_sessions(harnesses, include_subagents=False) -> list[Session]:
    out = []
    for h in harnesses:
        if h in SESSION_READERS:
            out.extend(SESSION_READERS[h](include_subagents))
    return out


def summarize(sessions: list[Session], threshold: int) -> dict | None:
    reqs = [c for s in sessions for _, c in s.reqs]
    if not reqs:
        return None
    firsts = [s.reqs[0][1] for s in sessions if not s.continued] or [s.reqs[0][1] for s in sessions]
    total = sum(reqs)
    over = [c for c in reqs if c > threshold]
    baseline = statistics.median(firsts)
    ordered = sorted(reqs)
    return {
        "sessions": len(sessions),
        "continued_sessions": sum(1 for s in sessions if s.continued),
        "requests": len(reqs),
        "baseline_median": round(baseline),
        "ctx_median": round(statistics.median(reqs)),
        "ctx_p90": ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))],
        "ctx_max": ordered[-1],
        "over_threshold_requests": len(over) / len(reqs),
        "over_threshold_tokens": sum(over) / total,
        "input_tokens": total,
        "startup_share": min(1.0, baseline * len(reqs) / total),
    }


def project_key(cwd: str) -> str:
    """Git repository of the session's cwd: a deleted subdirectory folds into its repo, and a
    worktree into its main checkout."""
    p = Path(cwd)
    while not _exists(p) and p.parent != p:
        p = p.parent
    root = git_root(p)
    if not root:
        return cwd
    if str(root) not in _MAIN_CHECKOUT:
        _MAIN_CHECKOUT[str(root)] = _main_checkout(root)
    return str(_MAIN_CHECKOUT[str(root)] or root)


_MAIN_CHECKOUT: dict[str, Path | None] = {}


_SAME_DIR: dict[tuple[str, str], bool] = {}


def _same_dir(a, b) -> bool:
    """One folder under two spellings? macOS and Windows disks ignore letter case, and
    Path.resolve keeps the case that was typed, while git and the session logs may record
    another. Ask the disk, not the string."""
    a, b = str(a), str(b)
    if a == b:
        return True
    if (a, b) not in _SAME_DIR:
        try:
            _SAME_DIR[(a, b)] = os.path.samefile(a, b)
        except (OSError, ValueError):
            _SAME_DIR[(a, b)] = False
    return _SAME_DIR[(a, b)]


def _true_case(path) -> Path:
    """`path` resolved, each part spelled as the disk spells it. A subfolder typed `SUB` for
    `sub` is the same folder to macOS and Windows but matches none of git's paths. A part in a
    folder that cannot be listed, or with no single match, keeps its typed spelling."""
    p = Path(path).resolve()
    out = Path(p.anchor)
    for part in p.parts[1:]:
        try:
            names = os.listdir(str(out))
        except OSError:  # can be entered, not listed: keep this part as typed, fix the rest
            names = [part]
        if part not in names:
            same = [n for n in names if n.lower() == part.lower() and _same_dir(out / n, out / part)]
            if len(same) == 1:  # else (another Unicode form, say) keep the typed spelling
                part = same[0]
        out = out / part
    return out


def group_sessions(sessions, since=None, until=None, project=None):
    groups: dict[tuple[str, str], list[Session]] = {}
    for s in sessions:
        day = local_day(s.start)
        if since and day < since:
            continue
        if until and day >= until:
            continue
        key = project_key(s.cwd)
        if project and project not in key:
            continue
        groups.setdefault((s.harness, key), []).append(s)
    return groups


# --------------------------------------------------------------------------- before and after

COMPARE_MIN_DROP_BYTES = 6_000  # about 2k tokens: a /retro line or a typo fix is not a slimming
COMPARE_MIN_DROP_SHARE = 0.20   # and the files must shrink by at least a fifth
COMPARE_MIN_SESSIONS = 3        # fewer sessions after a change are too few to trust
_NULL_SHA = "0" * 40
_WIN_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Read-only and offline whatever the repo's own config says: no fsmonitor program, no signature
# check program, no fetch of objects a partial clone lacks, no prompts, no pager.
_GIT_FLAGS = ("--no-pager", "-c", "core.quotepath=off", "-c", "core.fsmonitor=false",
              "-c", "log.showSignature=false", "-c", "protocol.allow=never")
# GIT_ALLOW_PROTOCOL also beats a repo's protocol.<name>.allow on git older than 2.44, which
# ignores GIT_NO_LAZY_FETCH.
_GIT_ENV_SET = {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "none", "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0"}
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")
_GIT_EXE: list = []


def _git_exe() -> str | None:
    """git's full path from the absolute PATH entries. A bare "git" on Windows first runs a
    git.exe from the current folder, which may be the very repo being weighed."""
    if not _GIT_EXE:
        names = ("git.exe", "git") if os.name == "nt" else ("git",)
        hit = None
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if hit or not d or not os.path.isabs(d):
                continue
            for n in names:
                p = os.path.join(d, n)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    hit = p
                    break
        _GIT_EXE.append(hit)
    return _GIT_EXE[0]


def _git(root, *args, data: bytes | None = None) -> bytes | None:
    """Output of one git command run in `root`, or None when git or the repo is unavailable."""
    exe = _git_exe()
    if not exe:
        return None
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update(_GIT_ENV_SET)
    try:
        p = subprocess.run([exe, *_GIT_FLAGS, "-C", str(root), *args], input=data, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _instruction_names() -> set[str]:
    """File names a harness loads by name, lower-cased: a case-insensitive disk (macOS, Windows)
    loads claude.md as CLAUDE.md. A name that does not load on this disk is weeded out later,
    when the old commit's files are scanned like the real ones."""
    _, fallbacks = _codex_config()
    return {n.lower() for n in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md",
                                *fallbacks, *_gemini_names())}


def _is_entry(rel: str, names: set[str]) -> bool:
    low = rel.lower()
    return posixpath.basename(low) in names or low.startswith(".claude/rules/") or "/.claude/rules/" in low


def _load_dir(rel: str) -> str:
    """The folder an entry file belongs to: CLAUDE.md, .claude/CLAUDE.md and .claude/rules/**
    all belong to the folder that holds `.claude`."""
    low = rel.lower()
    if low.startswith(".claude/"):
        return ""
    i = low.find("/.claude/")
    return rel[:i] if i >= 0 else posixpath.dirname(rel)


class _Repo:
    """One repo as the before and after sees it: which entry files load at startup from the
    folder `here` (repo-relative, "" for the root), and how links are checked out."""

    def __init__(self, root: Path, here: str, plain_links: bool):
        self.root, self.here, self.plain_links = root, here, plain_links
        self.names = _instruction_names()
        # With core.symlinks=false (Git for Windows' default) a link is checked out as a small
        # text file holding its target, and that text is what the agents read.
        self.skip = ("160000",) if plain_links else ("120000", "160000")
        self.listed = _opencode_patterns(read_text(root / "opencode.json"))  # files opencode.json lists
        self.snaps: dict = {}  # (rev, harnesses) -> _snapshot_load
        # Claude Code's instruction mode, from the real settings (a copy holds none of them), and
        # whether a CLAUDE.md no copy holds kept AGENTS.md out (_claude_md_kept_out).
        self.claude_mode: str | None = None
        self.claude_md_outside = False

    def on_path(self, rel: str) -> bool:
        d = _load_dir(rel)
        return d == "" or self.here == d or self.here.startswith(d + "/")

    def loads(self, rel: str) -> bool:
        """An entry file that loads at startup from `here`. Subfolder files load later, if at all."""
        return (_is_entry(rel, self.names) and self.on_path(rel)) or any(rx.match(rel) for _, rx in self.listed)

    def link_spot(self, rel: str) -> bool:
        """A link here can change what loads, though its own size says nothing about it."""
        low = rel.lower()
        return self.loads(rel) or ((low == ".claude/rules" or low.endswith("/.claude/rules"))
                                   and self.on_path(rel + "/x"))


def _links_now(repo: _Repo, rels) -> dict[str, str]:
    out = {}
    if repo.plain_links:
        return out
    for rel in rels:
        p = repo.root / rel
        if repo.link_spot(rel) and os.path.islink(p):
            try:
                out[rel] = os.readlink(str(p)).replace(os.sep, "/")
            except OSError:
                pass
    return out


def _links_at(repo: _Repo, items) -> dict[str, str] | None:
    if repo.plain_links:
        return {}
    ids = {rel: sha for mode, sha, rel in items if mode == "120000" and repo.link_spot(rel)}
    blobs = _blobs(repo.root, ids.values())
    if blobs is None:
        return None
    return {rel: blobs[sha].decode("utf-8", errors="replace") for rel, sha in ids.items()}


def _rel_ok(rel: str) -> bool:
    """A repo path that stays inside the copy on every OS: not absolute, no `.` or `..`, and no
    backslash or colon, which Windows reads as a separator or a drive."""
    if not rel or rel.startswith("/"):
        return False
    for part in rel.split("/"):
        if part in ("", ".", "..") or "\\" in part or ":" in part or "\0" in part:
            return False
        if os.name == "nt" and part.split(".")[0].upper() in _WIN_DEVICES:
            return False
    return True


def _in_repo(rel_dir: str, target: str) -> str | None:
    """`target` (a link or an import, relative to `rel_dir`) as a repo path, or None when it leaves the repo."""
    if not target or target.startswith("/") or "\\" in target:
        return None
    t = posixpath.normpath(posixpath.join(rel_dir, target))
    return None if t in (".", "..") or t.startswith("../") else t


def _glob_loose(pattern: str):
    """A regex matching at least the repo paths glob.glob(pattern, recursive=True) matches. A
    file it matches beyond those only costs a copy: the copy's own scan decides what loads."""
    pattern = re.sub(r"\[[^\]/]*\]", "?", pattern)
    rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(rx + r"\Z", re.S)


def _opencode_patterns(text: str | None) -> list:
    """(pattern, loose regex) for each repo path in opencode.json's `instructions`."""
    try:
        patterns = _dict(json.loads(text or "{}")).get("instructions")
    except ValueError:
        return []
    out = []
    for pat in patterns if isinstance(patterns, list) else []:
        if isinstance(pat, str) and "://" not in pat and not pat.startswith(("~", "/")):
            pat = posixpath.normpath(pat)
            if pat not in (".", "..") and not pat.startswith("../"):
                out.append((pat, _glob_loose(pat)))
    return out


def _tree(root, rev: str) -> list[tuple[str, str, str]] | None:
    """(mode, blob id, path) for every file in `rev`."""
    out = _git(root, "ls-tree", "-r", "-z", rev)
    if out is None:
        return None
    items = []
    for rec in out.split(b"\0"):
        meta, _, path = rec.partition(b"\t")
        bits = meta.split()
        if len(bits) != 3 or bits[1] != b"blob":
            continue
        items.append((bits[0].decode(), bits[2].decode(), path.decode("utf-8", errors="replace")))
    return items


def _tree_size(repo: _Repo, items) -> int:
    entries = [sha for mode, sha, rel in items if mode not in repo.skip and repo.loads(rel)]
    sizes = _blob_sizes(repo.root, entries)
    return sum(sizes.get(sha, 0) for sha in entries)


def _blob_sizes(root, shas) -> dict[str, int]:
    shas = sorted({x for x in shas if x and x != _NULL_SHA})
    if not shas:
        return {}
    out = _git(root, "cat-file", "--batch-check=%(objectname) %(objectsize)", data=("\n".join(shas) + "\n").encode())
    sizes = {}
    for line in (out or b"").decode(errors="replace").splitlines():
        bits = line.split()
        if len(bits) == 2 and bits[1].isdigit():
            sizes[bits[0]] = int(bits[1])
    return sizes


def _blobs(root, shas) -> dict[str, bytes] | None:
    """Contents of blobs by id; None when git fails or any blob is missing (a partial clone)."""
    shas = sorted(set(shas))
    if not shas:
        return {}
    out = _git(root, "cat-file", "--batch", data=("\n".join(shas) + "\n").encode())
    if out is None:
        return None
    blobs, i = {}, 0
    while i < len(out):
        nl = out.find(b"\n", i)
        if nl < 0:
            return None
        head = out[i:nl].split()
        if len(head) != 3 or not head[2].isdigit():
            return None  # "<id> missing": weighing without it would understate the old load
        size = int(head[2])
        blobs[head[0].decode()] = out[nl + 1: nl + 1 + size]
        i = nl + 1 + size + 1
    return blobs if len(blobs) == len(shas) else None


def _specs(repo: _Repo) -> list[str]:
    """Pathspecs for every file that can change what loads at startup."""
    specs = [f":(glob,icase)**/{n}" for n in sorted(repo.names)] + [":(glob,icase)**/.claude/rules/**",
                                                                      ":(glob,icase)**/.claude/rules"]
    return specs + [":(glob)" + pat for pat, _ in repo.listed] + (["opencode.json"] if repo.listed else [])


def _log_changes(repo: _Repo, *args: str) -> list[dict]:
    """Commits `git log *args` lists that changed a file that loads from `here` or a link that
    can change what loads, newest first: sha, date, the size change of the loading files in
    bytes (`delta`), whether a link changed, and `files`: (path, old blob, new blob, size
    change) for each loading file, a blob None where none was read (absent, link, submodule)."""
    out = _git(repo.root, "log", "--no-renames", "--raw", "-z", "--no-abbrev", "--format=%x01%H %cI",
               *args, "--", *_specs(repo))
    if out is None:
        return []
    commits, cur = [], None
    fields = out.decode("utf-8", errors="replace").split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i].lstrip("\n")
        i += 1
        if rec.startswith("\x01"):
            sha, _, date = rec[1:].partition(" ")
            cur = {"sha": sha, "date": date.strip(), "files": [], "links": False}
            commits.append(cur)
        elif rec.startswith(":") and cur is not None and i < len(fields):
            bits, path = rec.split(), fields[i]
            i += 1
            if len(bits) < 4:
                continue
            old_mode, new_mode = bits[0][1:], bits[1]
            if repo.loads(path):
                cur["files"].append((path, old_mode, new_mode, bits[2], bits[3]))
            if "120000" in (old_mode, new_mode) and not repo.plain_links and repo.link_spot(path):
                cur["links"] = True
    sizes = _blob_sizes(repo.root, [x for c in commits for f in c["files"] for x in (f[3], f[4])])
    for c in commits:
        files = []
        for path, old_mode, new_mode, old, new in c["files"]:
            old = old if old_mode not in repo.skip and old in sizes else None
            new = new if new_mode not in repo.skip and new in sizes else None
            files.append((path, old, new, sizes.get(new, 0) - sizes.get(old, 0)))
        c["files"], c["delta"] = files, sum(f[3] for f in files)
    return [c for c in commits if c["files"] or c["links"]]


def _drop(c: dict, keep) -> int:
    """Bytes a commit from _log_changes removed from the files in `keep`."""
    return -sum(d for path, _, _, d in c["files"] if path in keep)


def _history(repo: _Repo, head_size: int, since: str | None) -> list[dict]:
    """Main-line commits since `since` that changed an entry file, newest first, with the size
    of the entry files that load from `here` before and after each one, and whether it changed a
    link that can change what loads. A merge counts as one change against its first parent, so
    a slimming reviewed on a branch lands on the day it reached the main line."""
    window = [f"--since={since}"] if since else []  # git finds nothing for a date before 1970
    commits = _log_changes(repo, "--first-parent", "-m", *window, "HEAD")
    after = head_size
    for c in commits:
        c["after"], c["before"] = after, after - c["delta"]
        after = c["before"]
    return commits


def _is_cut(before: int, after: int, floor: int = COMPARE_MIN_DROP_BYTES) -> bool:
    return before - after >= floor and after <= before * (1 - COMPARE_MIN_DROP_SHARE)


def _work_files(root) -> tuple[set[str], set[str], list[str]] | None:
    """Tracked files and untracked files git does not ignore, as repo paths, and the ignored
    files and folders (folders end in "/")."""
    tracked = _git(root, "ls-files", "-z", "--cached")
    other = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    ignored = _git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
    if tracked is None or other is None or ignored is None:
        return None
    split = lambda b: [x.decode("utf-8", errors="replace") for x in b.split(b"\0") if x]
    return set(split(tracked)), set(split(other)), split(ignored)


def _work_size(repo: _Repo, rels) -> int:
    total = 0
    for rel in rels:
        p = repo.root / rel
        if repo.loads(rel) and (repo.plain_links or not os.path.islink(p)) and _is_file(p):
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _materialize(repo: _Repo, rev: str, dest: Path) -> bool:
    """Copy what loads at startup in `rev` under `dest`, marked as a repo root: the entry files,
    then what they @import and what opencode.json lists, hop by hop. Links inside the repo are
    made again as links, so imports and folders resolve as in a checkout; where the system
    cannot make links, the linked files are copied. Nothing is written outside `dest`."""
    root = repo.root
    items = _tree(root, rev)
    if items is None:
        return False
    files = {rel: sha for mode, sha, rel in items if mode not in repo.skip}
    link_ids = {} if repo.plain_links else {rel: sha for mode, sha, rel in items if mode == "120000"}
    link_text = _blobs(root, link_ids.values())
    if link_text is None:
        return False
    targets = {rel: _in_repo(posixpath.dirname(rel),
                             link_text[sha].decode("utf-8", errors="replace").strip("\n"))
               for rel, sha in link_ids.items()}

    def resolve(rel: str, hops: int = 0) -> str | None:
        """The regular file `rel` names, following links inside the repo."""
        if rel in files or hops > 8:
            return rel if rel in files else None
        parts = rel.split("/")
        for i in range(len(parts), 0, -1):  # the file itself, then each folder above it
            head = "/".join(parts[:i])
            if head in targets:
                t = targets[head]
                return resolve("/".join([t, *parts[i:]]), hops + 1) if t else None
        return None

    want = [rel for rel in files if repo.loads(rel)] + [rel for rel in targets if repo.loads(rel)]
    for link, t in targets.items():  # a linked folder on the load path, such as .claude/rules
        here = posixpath.dirname(link)
        if not t or t in files or not repo.on_path(link + "/x") or here == t or here.startswith(t + "/"):
            continue  # a link to its own folder or above would only list the same files again
        want += [link + rel[len(t):] for rel in files
                 if rel.startswith(t + "/") and repo.loads(link + rel[len(t):])]
    cfg_id = files.get("opencode.json")
    if cfg_id:
        want.append("opencode.json")
        cfg = _blobs(root, [cfg_id]) or {}
        for _, rx in _opencode_patterns(cfg.get(cfg_id, b"").decode("utf-8", errors="replace")):
            want += [rel for rel in files if rx.match(rel)]

    # 1. Pick the files: what loads, then what that imports, as far as Claude Code follows imports.
    bodies: dict[str, bytes] = {}
    aliases: dict[str, str] = {}  # a path that runs through a link -> the file it reaches
    seen: set[str] = set()
    queue = list(dict.fromkeys(want))
    for _ in range(CLAUDE_IMPORT_DEPTH + 2):
        todo = []
        for rel in queue:
            if rel not in seen and _rel_ok(rel):
                seen.add(rel)
                src = resolve(rel)
                if src and _rel_ok(src):
                    todo.append((rel, src))
        if not todo:
            break
        got = _blobs(root, [files[src] for _, src in todo if src not in bodies])
        if got is None:
            return False
        queue = []
        for rel, src in todo:
            bodies.setdefault(src, got.get(files[src], b""))
            if rel != src:
                aliases[rel] = src
            body = bodies[src]
            if b"\0" in body[:4096]:
                continue
            text = _INLINE.sub("", _FENCE.sub("", body.decode("utf-8", errors="replace")))
            for m in _IMPORT.finditer(text):
                ref = m.group(1).replace("\\ ", " ")
                for d in {posixpath.dirname(rel), posixpath.dirname(src)}:  # either way a link may read
                    t = _in_repo(d, ref)
                    if t:
                        queue.append(t)

    # 2. Write the regular files. No link exists yet, so no write can pass through one.
    base = os.path.realpath(str(dest))

    def inside(p: str) -> bool:
        try:
            return os.path.commonpath([base, os.path.realpath(p)]) == base
        except ValueError:  # another drive on Windows
            return False

    def put(rel: str, body: bytes) -> None:
        p = os.path.join(base, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if inside(os.path.dirname(p)) and not os.path.lexists(p):
                with open(p, "wb") as fh:
                    fh.write(body)
        except (OSError, ValueError):
            pass

    os.makedirs(os.path.join(base, ".git"), exist_ok=True)
    for src, body in sorted(bodies.items()):
        put(src, body)

    # 3. Make the links again, shallowest first, then drop any that leads out of the copy.
    made, failed = [], False
    for link in sorted(targets, key=lambda r: (r.count("/"), r)):
        t = targets[link]
        if not t or not _rel_ok(link):
            continue
        p = os.path.join(base, *link.split("/"))
        rel_target = posixpath.relpath(t, posixpath.dirname(link) or ".")
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if not inside(os.path.dirname(p)) or os.path.lexists(p):
                continue
            os.symlink(rel_target.replace("/", os.sep), p, target_is_directory=t not in files)
            made.append(p)
        except (OSError, NotImplementedError, ValueError):
            failed = True
    while True:
        out = [p for p in made if not inside(p)]
        if not out:
            break
        for p in out:
            os.unlink(p)
            made.remove(p)
    if failed and not made:  # no links on this system: copy what the links would have shown
        for rel, src in sorted(aliases.items()):
            put(rel, bodies[src])
    return True


def _repo_load(scan: dict, root: Path, ignored: list[str] | None):
    """Tokens each harness loads from the repo, and the size of each repo file, from a scan.
    Files git ignores (CLAUDE.local.md and the like) are left out when `ignored` lists them."""
    per_harness: dict[str, int] = {}
    per_file: dict[str, int] = {}
    root = root.resolve()
    skip = set(ignored or ())
    folders = [d for d in skip if d.endswith("/")]
    for h in HARNESSES:
        r = scan.get(h)
        if not r:
            continue
        total = 0
        for e in r["files"]:
            if not _inside(e["path"], root):
                continue
            p = Path(e["path"])
            try:
                rel = p.relative_to(root).as_posix()  # a linked file keeps its own name
            except ValueError:
                rel = p.resolve().relative_to(root).as_posix()
            if rel in skip or any(rel.startswith(d) for d in folders):
                continue
            total += e["tokens"]
            per_file[rel] = max(per_file.get(rel, 0), e.get("file_tokens", e["tokens"]))
        per_harness[h] = total
    return per_harness, per_file


def _when(ts: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _snapshot_load(repo: _Repo, rev: str, hs):
    """Repo tokens each harness would load at `rev`, from its files copied to a temporary folder.
    Each commit is weighed once per repo."""
    key = (rev, tuple(hs))
    if key not in repo.snaps:
        repo.snaps[key] = _weigh_snapshot(repo, rev, hs)
    return repo.snaps[key]


def _weigh_snapshot(repo: _Repo, rev: str, hs):
    seen_unreadable = len(UNREADABLE)
    try:
        with tempfile.TemporaryDirectory(prefix="context-weight-before-") as tmp:
            dest = Path(tmp) / (repo.root.name or "repo")
            if not _materialize(repo, rev, dest):
                return None
            cwd = dest.joinpath(*repo.here.split("/")) if repo.here else dest
            try:
                cwd.mkdir(parents=True, exist_ok=True)
            except OSError:  # the folder was a link that leads nowhere in the old commit
                cwd = dest
            scan = scan_files(str(cwd), hs, copy_root=dest, claude_md_outside=repo.claude_md_outside,
                              mode=repo.claude_mode)
            return _repo_load(scan, dest, None)
    finally:
        del UNREADABLE[seen_unreadable:]  # the copies are gone; warn only about the real files


def _loads_less(before: dict, after: dict) -> bool:
    """Did some harness's startup load from the repo really shrink? Moving text into a file
    that is still @imported, or into a CLAUDE.local.md git ignores, shrinks the entry files
    but not what loads."""
    return any(_is_cut(before.get(h, 0), after.get(h, 0), COMPARE_MIN_DROP_BYTES // 3) for h in before)


def _clean(text: str, limit: int | None = None) -> str:
    """Text from the repo, safe to print: no control or format characters, so a commit message
    cannot send terminal escapes or flip the line with bidi marks."""
    s = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" and unicodedata.category(ch) not in ("Zl", "Zp"))
    s = " ".join(s.split())
    return s if limit is None or len(s) <= limit else s[:limit - 1] + "…"


def _commit_info(root, sha: str) -> tuple[dt.datetime | None, str | None]:
    """When `sha` was committed, and its subject line made safe to print."""
    out = _git(root, "log", "-1", "--format=%cI%x00%s", sha)
    if not out:
        return None, None
    when, _, subject = out.decode("utf-8", errors="replace").strip("\n").partition("\0")
    return _when(when.strip()), _clean(subject)


def _gone(blobs: dict, files, keep) -> Counter:
    """Lines changes remove from the files in `keep`, net of lines they add there, so text
    moved from one of them to another is not gone."""
    out: Counter = Counter()
    for path, old, new, _ in files:
        if path in keep:
            out.update((blobs[old] if old else b"").split(b"\n"))
            out.subtract((blobs[new] if new else b"").split(b"\n"))
    return +out


def _branch_cut(repo: _Repo, merge: str, keep) -> str | None:
    """The commit a merge brought in that made its cut, so the report can name it. `keep` holds
    the files the agents asked load before the merge. Each of the 20 branch commits that cut
    them most by size is credited with the bytes of the lines it removed that the merge itself
    removed too (its diff against the main line). Removing text the branch added, a cut the
    branch put back, and whatever the main line changed meanwhile earn nothing. The biggest
    credit of at least COMPARE_MIN_DROP_BYTES wins, the newer commit on a tie; None names the
    merge instead."""
    landed = [c for c in _log_changes(repo, "--first-parent", "-m", f"{merge}^1..{merge}") if c["sha"] == merge]
    changes = _log_changes(repo, "--no-merges", f"{merge}^1..{merge}")  # newest first
    cuts = sorted((c for c in changes if _drop(c, keep) >= COMPARE_MIN_DROP_BYTES), key=lambda c: -_drop(c, keep))[:20]
    if not (landed and cuts):
        return None
    blobs = _blobs(repo.root, {x for c in landed + cuts for f in c["files"] for x in f[1:3] if x})
    if blobs is None:
        return None
    goal = _gone(blobs, landed[0]["files"], keep)
    best, most = None, (COMPARE_MIN_DROP_BYTES - 1, 0)
    for c in cuts:
        credit = sum((len(line) + 1) * min(n, goal[line]) for line, n in _gone(blobs, c["files"], keep).items())
        rank = (credit, -changes.index(c))
        if rank > most:
            best, most = c["sha"], rank
    return best


_AT_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?\Z")
_AT_COMMIT = re.compile(r"[0-9a-fA-F]{4,64}\Z")  # 64 in a SHA-256 repository


def _compare_at(s: str) -> tuple[str, object, str]:
    """report --compare-at: a day (local midnight), a time (local unless it has an offset), or a
    commit id. Only a hex id reaches git, so the value can never be read as an option."""
    if _AT_TIME.match(s):
        try:
            if len(s) == 10:
                t = dt.datetime.combine(dt.date.fromisoformat(s), dt.time())
            else:
                t = dt.datetime.fromisoformat(s[:10] + "T" + s[11:].replace("Z", "+00:00"))
            return "time", (t if t.tzinfo else t.astimezone()), s
        except (ValueError, OverflowError, OSError):
            raise UsageError(f"--compare-at {s}: not a real date or time")
    if _AT_COMMIT.match(s):
        return "commit", s.lower(), s
    raise UsageError(f"--compare-at wants YYYY-MM-DD, YYYY-MM-DDTHH:MM or a commit id, got {s!r}")


def _cut_since(repo: _Repo, rev: str, hs, snap, today):
    """The after side of a split at a day or a time, whose files are `snap`: for each agent, its
    first main-line cut since then, found as the report finds one by itself, not today's files,
    which may have grown back. Only commits that cut the files of an agent still without a cut
    are weighed, at most five; an agent with no cut among them is weighed as today."""
    need = [h for h in hs if snap[0].get(h, 0) > 0]
    own = {h: set((_snapshot_load(repo, rev, (h,)) or ({}, {}))[1]) for h in need}
    picked, stages, looked = {}, [], 0
    for c in reversed(_log_changes(repo, "--first-parent", "-m", f"{rev}..HEAD")):  # oldest first
        if looked >= 5 or len(picked) == len(need):
            break
        keep = set().union(*(own[h] for h in need if h not in picked))
        if not (c["links"] or _drop(c, keep) >= COMPARE_MIN_DROP_BYTES):
            continue
        looked += 1
        nxt = _snapshot_load(repo, c["sha"], hs)
        cut = [h for h in need if h not in picked and nxt and _loads_less({h: snap[0][h]}, {h: nxt[0].get(h, 0)})]
        if cut:
            picked.update(dict.fromkeys(cut, nxt))
            stages.append(nxt)
    if not picked:
        return today
    if len(picked) < len(need):
        stages.append(today)
    load = dict(today[0])
    load.update({h: s[0].get(h, 0) for h, s in picked.items()})
    files = {}
    for rel in set(snap[1]).union(*(s[1] for s in stages)):  # each file as it was first changed
        files[rel] = next((s[1].get(rel, 0) for s in stages if s[1].get(rel, 0) != snap[1].get(rel, 0)),
                          snap[1].get(rel, 0))
    return load, files


def _claude_md_kept_out(path, root: Path, ignored) -> bool:
    """Did a CLAUDE.md that no old commit's copy holds keep AGENTS.md out of Claude Code? One
    in a folder above the repo, or one git ignores (CLAUDE.local.md) on the way down to `path`,
    which is taken to have been there all along."""
    user_md = claude_dir() / "CLAUDE.md"
    skip = set(ignored or ())
    folders = [d for d in skip if d.endswith("/")]
    for d in _claude_walk(path):
        for n in CLAUDE_MD_NAMES:
            p = d / n
            if not _holds_text(p) or _same_dir(p, user_md):
                continue
            if not _inside(d, root):
                return True
            rel = (d / n).relative_to(root).as_posix()
            if rel in skip or any(rel.startswith(f) for f in folders):
                return True
    return False


def compare(path, hs, files_now: dict, sessions, days: int, threshold: int, at=None):
    """Before and after the last cut of this repo's instruction files.

    The cut is found in git: uncommitted changes that shrink the entry files that load from
    `path`, else the newest main-line commits in the window that shrank them by
    COMPARE_MIN_DROP_BYTES and a fifth or changed a link on the load path, and the first of
    those whose startup load really dropped against its parent. Files that grew back later do
    not hide it: today's size is shown next to it. `at` (from _compare_at) sets the split
    instead. Sizes come from git; session numbers on each side come from the logs. Returns
    None when there is nothing to compare."""
    root = git_root(path)
    top = _git(root, "rev-parse", "--show-toplevel") if root else None
    if top is None or not _same_dir(Path(top.decode(errors="replace").strip()).resolve(), root.resolve()) or \
            _git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}") is None:
        if at:
            raise UsageError("--compare-at needs git and a git repository with at least one commit")
        return None
    root = root.resolve()
    try:
        here = Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        here = "."
    plain = (_git(root, "config", "--bool", "core.symlinks") or b"").strip() == b"false"
    repo = _Repo(root, "" if here == "." else here, plain)
    work = _work_files(root)
    if work is None:
        return None
    tracked, other, ignored = work
    repo.claude_mode = _instruction_mode(path)[0]
    repo.claude_md_outside = _claude_md_kept_out(path, root, ignored)
    after, after_files = _repo_load(files_now, root, ignored)
    after_all, after_all_files = _repo_load(files_now, root, None)
    local = {h: after_all.get(h, 0) - after.get(h, 0) for h in after_all}  # what ignored files load now
    now = dt.datetime.now().astimezone()
    # (found, rev weighed as before or None for nothing, rev weighed as after or None for the
    # files today, split time, commit)
    candidates = []
    if at and at[0] == "time":
        rev = (_git(root, "rev-list", "-1", "--first-parent", f"--before={min(at[1], now).isoformat()}", "HEAD")
               or b"").decode().strip()
        if not rev:
            raise UsageError(f"--compare-at {at[2]}: no commit on this branch before then")
        candidates.append(("date", rev, None, at[1], None))
    elif at:
        sha = (_git(root, "rev-parse", "--verify", "-q", at[1] + "^{commit}") or b"").decode().strip()
        when = _commit_info(root, sha)[0] if sha else None
        if not when:
            raise UsageError(f"--compare-at {at[2]}: not a commit in this repository")
        parent = _git(root, "rev-parse", "--verify", "-q", sha + "^1")
        candidates.append(("commit", parent.decode().strip() if parent else None, sha, when, sha))
    else:
        head = _tree(root, "HEAD")
        if head is None:
            return None
        head_size = _tree_size(repo, head)
        relinked = _links_at(repo, head) != _links_now(repo, tracked | other)
        if relinked or _is_cut(head_size, _work_size(repo, tracked | other)):
            candidates.append(("uncommitted", "HEAD", None, now, None))
        start = now - dt.timedelta(days=days) if days < 365 * 50 else None  # git finds nothing before 1970
        cuts = links = 0
        for c in _history(repo, head_size, start.isoformat() if start else None):
            if cuts >= 3 and links >= 3:
                break
            is_cut = _is_cut(c["before"], c["after"])
            if not ((is_cut and cuts < 3) or (c["links"] and not is_cut and links < 3)):
                continue
            parent = _git(root, "rev-parse", "--verify", "-q", c["sha"] + "^1")
            when = _when(c["date"])
            if parent and when:
                cuts, links = cuts + is_cut, links + (not is_cut)
                candidates.append(("commit", parent.decode().strip(), c["sha"], when, c["sha"]))
    for found, rev, cut_rev, at_time, commit in candidates:
        snap = _snapshot_load(repo, rev, hs) if rev else ({}, {})
        cut = _snapshot_load(repo, cut_rev, hs) if cut_rev else (after, after_files)
        if snap and found == "date":
            cut = _cut_since(repo, rev, hs, snap, cut)
        if not (snap and cut):
            continue
        if at:
            break
        # Judge a commit against its own parent, not today: files that grew back since must not
        # hide it. Ignored local files (CLAUDE.local.md) still load, so text moved there counts.
        weigh = after_all if cut_rev is None else {h: cut[0].get(h, 0) + local.get(h, 0)
                                                   for h in set(cut[0]) | set(local)}
        if _loads_less(snap[0], weigh):
            break
    else:
        return None
    (before, before_files), (cut_load, cut_files) = snap, cut
    merge = None
    if commit and len((_git(root, "rev-parse", commit + "^@") or b"").split()) > 1:
        branch = _branch_cut(repo, commit, set(before_files))
        if branch:
            merge, commit = commit, branch
    subject = _commit_info(root, commit)[1] if commit else None
    changed = [{"path": rel, "before": before_files.get(rel, 0), "after": cut_files.get(rel, 0)}
               for rel in set(before_files) | set(cut_files)
               if before_files.get(rel, 0) != cut_files.get(rel, 0)]
    changed.sort(key=lambda x: (-abs(x["after"] - x["before"]), x["path"]))
    key = project_key(str(root))  # a worktree's sessions are its repository's
    mine = [(s, _when(s.start)) for s in sessions if s.reqs and _same_dir(project_key(s.cwd), key)]
    try:
        lo = at_time - dt.timedelta(days=days)
    except OverflowError:
        lo = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    sess = {}
    for h in hs:
        b = [s for s, t in mine if s.harness == h and t and lo <= t < at_time]
        a = [s for s, t in mine if s.harness == h and t and t >= at_time]
        if b or a:
            sess[h] = {"before": summarize(b, threshold), "after": summarize(a, threshold)}
    files = {h: {"before": before.get(h, 0), "after": cut_load.get(h, 0), "now": after.get(h, 0)} for h in hs
             if before.get(h, 0) or cut_load.get(h, 0) or after.get(h, 0)}
    out = {
        "found": found,
        "picked": bool(at),
        "at": at_time.isoformat(timespec="minutes"),  # a commit keeps the offset it was made in
        "commit": commit[:7] if commit else None,
        "merge": merge[:7] if merge else None,
        "subject": subject,
        "before_ref": "HEAD" if found == "uncommitted" else (rev[:7] if rev else None),
        "files": files,
        "changed": changed[:5],
        "untracked": sorted(rel for rel in after_files if rel in other) if found != "uncommitted" else [],
        "ignored": [{"path": rel, "tokens": after_all_files[rel]} for rel in sorted(after_all_files)
                    if rel not in after_files],
        "sessions": sess,
        "days": days,
        "min_sessions": COMPARE_MIN_SESSIONS,
    }
    # Claude Code deletes transcripts after cleanupPeriodDays (30 by default), so an empty
    # "before" may only mean the logs are gone. Say so, from the oldest log left on this machine.
    if "claude" in files or "claude" in sess:
        starts = [s.start for s in sessions if s.harness == "claude" and _when(s.start)]
        oldest = min(starts, key=_when) if starts else None
        b = (sess.get("claude") or {}).get("before")
        if oldest and _when(oldest) > lo and (not b or b["sessions"] < COMPARE_MIN_SESSIONS):
            keep = _cleanup_days(root)
            out["logs_from"] = {"claude": local_day(oldest)}
            out["cleanup_days"] = keep
            out["logs_deleted"] = (now - _when(oldest)).days >= keep - 1
    return out


def _cleanup_days(root: Path) -> int:
    """Claude Code's cleanupPeriodDays: user, then project, then local settings; 30 when unset."""
    days = 30
    for f in (claude_dir() / "settings.json", root / ".claude" / "settings.json",
              root / ".claude" / "settings.local.json"):
        try:
            v = json.loads(read_text(f) or "{}").get("cleanupPeriodDays")
        except (ValueError, AttributeError):
            continue
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            days = v
    return days


def _compare_label(c: dict, subject_max: int = 72) -> str:
    when = c["at"][:16].replace("T", " ")
    if c["found"] == "uncommitted":
        return "uncommitted changes against the last commit"
    if c["found"] == "commit":
        label = f"commit {c['commit']}"
        if c.get("subject"):
            subject = c["subject"] if len(c["subject"]) <= subject_max else c["subject"][:subject_max - 1] + "…"
            label += f' "{subject}"'
        if c.get("merge"):
            label += f", merged in {c['merge']}"
        return label + f", {when}" + (", --compare-at" if c.get("picked") else "")
    return f"split at {when[:10] if when.endswith(' 00:00') else when}, --compare-at"


def _grew_back(f: dict) -> bool:
    return f.get("now", f["after"]) - f["after"] >= COMPARE_MIN_DROP_BYTES // 3


def _compare_notes(c: dict, arrow: str = "->") -> list[str]:
    """What the numbers alone would hide: growth since the change, a split that cut nothing,
    and session logs Claude Code already deleted."""
    notes = []
    files = c["files"]
    grew = [h for h in _compare_agents(c)  # only agents the change cut: growth elsewhere is not "back"
            if h in files and files[h]["after"] < files[h]["before"] and _grew_back(files[h])]
    if grew:
        notes.append("Grew back since the change: " + ", ".join(
            f"{AGENT_NAMES[h]} ~{fmt_k(files[h]['after'])} {arrow} ~{fmt_k(files[h]['now'])} now"
            + (f", past the ~{fmt_k(files[h]['before'])} before it" if files[h]["now"] >= files[h]["before"] else "")
            for h in grew) + ". Sessions after it include that growth.")
    if c.get("picked") and c["files"] and not any(f["after"] < f["before"] for f in c["files"].values()):
        notes.append("The instruction files did not shrink at this split. Pass the time just before "
                     "the change (YYYY-MM-DDTHH:MM) or its commit id.")
    for h, day in sorted((c.get("logs_from") or {}).items()):
        if not c.get("logs_deleted"):
            notes.append(f"{AGENT_NAMES[h]} logs on this machine start {day}, so few sessions come before the change.")
            continue
        keep = c.get("cleanup_days", 30)
        notes.append(f"{AGENT_NAMES[h]} logs on this machine start {day}: it deletes transcripts older than "
                     f"cleanupPeriodDays ({keep} days{' by default' if keep == 30 else ''}).")
        notes.append(f'Set "cleanupPeriodDays" higher in {short(claude_dir() / "settings.json")} '
                     "to keep the sessions the next report needs.")
    return notes


def _compare_agents(c: dict) -> list[str]:
    return [h for h in HARNESSES if h in c["files"] or h in c["sessions"]]


def _compare_status(c: dict, h: str) -> tuple[str | None, str]:
    """(badge kind or None, words) for one agent's sessions on each side of the change."""
    s = c["sessions"].get(h) or {}
    b, a = s.get("before"), s.get("after")
    if not (b or a):
        return None, "no sessions on either side"
    if not a:
        return None, "no sessions after the change yet"
    n = a["sessions"]
    if n < c["min_sessions"]:
        return "warn", f"{n} session{'' if n == 1 else 's'} after: too early to trust"
    if not b:
        return None, f"{n} sessions after, none before in this window"
    m = b["sessions"]
    return "good", f"{m} session{'' if m == 1 else 's'} before, {n} after"


def _compare_hint(c: dict) -> str | None:
    if c["found"] == "uncommitted":
        return "Commit the change, work as usual for a few days, then run the report again."
    if any(_compare_status(c, h)[0] != "good" for h in c["sessions"]):
        return "Work as usual for a few days, then run the report again for the measured numbers."
    return None


def print_compare(c: dict) -> None:
    print(f"## Before and after ({_compare_label(c)})")
    print(f"files = this repo's instruction files, estimated tokens; startup and median = measured "
          f"sessions, the {c['days']} days before the change against everything since")
    for h in _compare_agents(c):
        f = c["files"].get(h, {"before": 0, "after": 0})
        line = f"{h:8} files {fmt_k(f['before'])} -> {fmt_k(f['after'])}"
        if fmt_k(f.get("now", f["after"])) != fmt_k(f["after"]):
            line += f", now {fmt_k(f['now'])}"
        s = c["sessions"].get(h) or {}
        b, a = s.get("before"), s.get("after")
        if b and a:
            line += (f"   startup {fmt_k(b['baseline_median'])} -> {fmt_k(a['baseline_median'])}"
                     f"   median {fmt_k(b['ctx_median'])} -> {fmt_k(a['ctx_median'])}")
        print(f"{line}   ({_compare_status(c, h)[1]})")
    if c["changed"]:
        print("changed: " + ", ".join(f"{x['path']} {fmt_k(x['before'])} -> {fmt_k(x['after'])}"
                                      for x in c["changed"]))
    if c["untracked"]:
        print("not committed yet, so teammates do not get it: " + ", ".join(c["untracked"]))
    if c["ignored"]:
        print("left out because git ignores them, so the old commit cannot show them: " +
              ", ".join(f"{x['path']} {fmt_k(x['tokens'])}" for x in c["ignored"]))
    for note in _compare_notes(c):
        print(note)
    hint = _compare_hint(c)
    if hint:
        print(hint)


# --------------------------------------------------------------------------- output

def print_files(result: dict) -> None:
    for h in HARNESSES:
        if h not in result:
            continue
        r = result[h]
        n = len(r["files"])
        print(f"{h:9} ~{fmt_k(r['total_tokens'])} tokens at startup ({n} file{'' if n == 1 else 's'}, "
              f"~{fmt_k(r['repo_tokens'])} from this repo)")
        for e in r["files"]:
            extra = f"  [-{e['truncated_bytes']:,} bytes]" if e.get("truncated_bytes") else ""
            print(f"  {e['role']:<28} {fmt_k(e['tokens']):>7}  {short(e['path'])}{extra}")
        for w in r["warnings"]:
            print(f"  ! {w}")
        for n in r["notes"]:
            print(f"  · {n}")
    for w in result.get("cross", []):
        print(f"! {w}")


def print_skills(result: dict) -> None:
    assumed = False
    for h, r in result.items():
        if r["budget"]:
            used = r["chars"] if r["budget_unit"] == "chars" else r["tokens"]
            budget = f"{fmt_k(used)} of {fmt_k(r['budget'])} {r['budget_unit']}: {r['basis']}"
            if r["window_source"] == "default":
                budget += " (assumed)"
                assumed = True
        else:
            budget = r["basis"]
        flag = "  OVER: descriptions get dropped, names stay" if r["over_budget"] else ""
        print(f"{h:9} {r['count']:4} skills  ~{fmt_k(r['tokens'])} tokens listing  ({budget}){flag}")
        if r["heaviest"]:
            print("          heaviest: " + ", ".join(f"{x['name']} {fmt_k(x['tokens'])}" for x in r["heaviest"]))
    if assumed:
        print(f"  · window assumed {fmt_k(DEFAULT_WINDOW)} tokens; pass --window 1000000 for a 1M-context model")


def _name(key: str, width: int) -> str:
    s = short(key)
    return s if len(s) <= width else "…" + s[-(width - 1):]


def print_sessions(groups: dict, threshold: int, top: int) -> None:
    rows = [(k, summarize(v, threshold)) for k, v in groups.items()]
    rows = sorted([r for r in rows if r[1]], key=lambda r: -r[1]["input_tokens"])[:top]
    if not rows:
        print("no sessions found")
        return
    t = fmt_k(threshold)
    print(f"{'harness':8} {'project':38} {'sess':>5} {'req':>6} {'start':>6} {'median':>7} {'p90':>6} "
          f"{'max':>6} {'>' + t + ' req':>10} {'>' + t + ' tok':>10} {'start%':>6} {'input':>7}")
    for (h, key), s in rows:
        print(f"{h:8} {_name(key, 38):38} {s['sessions']:5} {s['requests']:6} {fmt_k(s['baseline_median']):>6} "
              f"{fmt_k(s['ctx_median']):>7} {fmt_k(s['ctx_p90']):>6} {fmt_k(s['ctx_max']):>6} "
              f"{pct(s['over_threshold_requests']):>10} {pct(s['over_threshold_tokens']):>10} "
              f"{pct(s['startup_share']):>6} {fmt_k(s['input_tokens']):>7}")
    print("start = median context of a session's first request (what loads before work); "
          "start% = share of all input that is that startup load re-sent every request")


# --------------------------------------------------------------------------- findings

def findings(files: dict, skills: dict, stats: dict, target: int) -> list[str]:
    out = []
    for h, s in stats.items():
        f = files.get(h)
        if not (f and s):
            continue
        cut = f["repo_tokens"] - target
        if cut > FINDING_MIN_CUT:
            saved = cut * s["requests"]
            out.append(f"[{h}] repo instructions ~{fmt_k(f['repo_tokens'])} tokens are re-sent on every request. "
                       f"Slimming to ~{fmt_k(target)} would have saved ~{fmt_k(saved)} of "
                       f"{fmt_k(s['input_tokens'])} input tokens in this window ({pct(saved / s['input_tokens'])}, "
                       "estimated at today's file size). See references/slimming.md")
        if s["over_threshold_tokens"] > 0.5:
            out.append(f"[{h}] {pct(s['over_threshold_tokens'])} of input tokens came from requests above the "
                       "threshold: long sessions dominate. Hand off and start fresh sessions earlier")
    for h in HARNESSES:
        f = files.get(h)
        if not f:
            continue
        if h not in stats and f["repo_tokens"] - target > FINDING_MIN_CUT:
            out.append(f"[{h}] repo instructions ~{fmt_k(f['repo_tokens'])} tokens load at startup "
                       "(no session logs in this window to measure the cost)")
        for w in f["warnings"]:
            out.append(f"[{h}] {w}")
    for w in files.get("cross", []):
        out.append(f"[all] {w}. Keep one canonical AGENTS.md and import it from CLAUDE.md")
    for u in files.get("unreadable", []):
        out.append(f"[all] cannot read {short(u)}: it is not counted, and the agent cannot load it either")
    for h, r in skills.items():
        if r["over_budget"]:
            used = r["chars"] if r["budget_unit"] == "chars" else r["tokens"]
            out.append(f"[{h}] {r['count']} skills need ~{fmt_k(used)} {r['budget_unit']} of listing, budget "
                       f"{fmt_k(r['budget'])} ({r['basis']}): the least-used lose their descriptions and stop "
                       "triggering reliably. Uninstall unused skills or plugins")
    return out


def _over_target(files: dict, target: int) -> str | None:
    """With no findings, the agents whose repo instructions still pass the target: by less
    than a finding needs, but the report must not call that "within target"."""
    over = [f"{AGENT_NAMES[h]} is ~{fmt_k(files[h]['repo_tokens'] - target)} over" for h in HARNESSES
            if h in files and files[h]["repo_tokens"] > target]
    return f"No large fixes: {', '.join(over)} the ~{fmt_k(target)} target." if over else None


# --------------------------------------------------------------------------- picture (SVG)

AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI", "opencode": "OpenCode"}

_LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#7a7873", "grid": "#e1e0d9",
          "border": "rgba(11,11,11,0.10)", "repo": "#2a78d6", "other": "#eb6834", "warn": "#fab219",
          "crit": "#d03b3b", "good": "#0ca30c"}
_DARK = {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#9a988f", "grid": "#2c2c2a",
         "border": "rgba(255,255,255,0.10)", "repo": "#3987e5", "other": "#d95926", "warn": "#fab219",
         "crit": "#d03b3b", "good": "#0ca30c"}


def _css_vars(c: dict) -> str:
    return "".join(f"--{k}:{v};" for k, v in c.items())


def _svg_style(theme: str) -> str:
    light, dark = _css_vars(_LIGHT), _css_vars(_DARK)
    if theme == "light":
        palette = f"svg{{{light}}}"
    elif theme == "dark":
        palette = f"svg{{{dark}}}"
    else:
        palette = f"svg{{{light}}}@media (prefers-color-scheme: dark){{svg{{{dark}}}}}"
    return (palette +
            "text{font-family:system-ui,-apple-system,'Segoe UI',Roboto,'Noto Sans','Noto Sans Thai',sans-serif;"
            "fill:var(--ink)}"
            ".s{fill:var(--ink2)}.m{fill:var(--muted)}.b{font-weight:600}.n{font-variant-numeric:tabular-nums}"
            ".r{text-anchor:end}.bg{fill:var(--surface);stroke:var(--border)}.hl{stroke:var(--grid)}"
            ".tgt{stroke:var(--muted)}.repo{fill:var(--repo)}.other{fill:var(--other)}.track{fill:var(--grid)}"
            ".warn{fill:var(--warn)}.crit{fill:var(--crit)}.good{fill:var(--good)}.on{fill:#fff}.on-dark{fill:#0b0b0b}")


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _xml(s: str) -> str:
    """Escape for SVG text, and replace characters XML cannot hold (control codes, lone
    surrogates from undecodable file names) with U+FFFD so the picture always parses."""
    return escape(_XML_BAD.sub("\ufffd", s))


def _text_px(s: str, size: float) -> float:
    """Rough rendered width of `s` in a system sans: ASCII ~0.54em, anything else ~1em."""
    return sum(0.54 if ord(c) < 128 else 1.0 for c in s) * size


def _fit(s: str, px: float, size: float, left: bool = True) -> str:
    """Shorten `s` to fit `px`, dropping characters from the left (paths) or the right."""
    if _text_px(s, size) <= px:
        return s
    while s and _text_px("…" + s, size) > px:
        s = s[1:] if left else s[:-1]
    return "…" + s if left else s + "…"


def _wrap(s: str, px: float, size: float) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}" if cur else word
        if cur and _text_px(trial, size) > px:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return [_fit(x, px, size, left=False) for x in lines]


class _Svg:
    def __init__(self):
        self.parts: list[str] = []

    def text(self, x, y, s, size=14, cls="", title=None):
        tip = f"<title>{_xml(title)}</title>" if title else ""
        self.parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" class="{cls}">{tip}{_xml(s)}</text>')

    def lead(self, x, y, lead, rest, size=14):
        """One line that opens with a bold lead-in, such as an agent name."""
        self.parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}"><tspan class="b">{_xml(lead)}</tspan>'
                          f"{_xml(rest)}</text>")

    def rect(self, x, y, w, h, cls, rx=0, title=None):
        tip = f"<title>{_xml(title)}</title>" if title else ""
        self.parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(0.0, w):.1f}" height="{h:.1f}" '
                          f'rx="{rx}" class="{cls}">{tip}</rect>')

    def bar(self, x, y, w, h, cls, round_end=True, title=None):
        """Bar from a baseline at x: square at the baseline, 4px rounded at the data end."""
        if w <= 0:
            return
        r = min(4.0, w / 2, h / 2) if round_end else 0
        d = (f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}"
             f"a{r},{r} 0 0 1 -{r},{r}h-{w - r:.1f}z")
        tip = f"<title>{_xml(title)}</title>" if title else ""
        self.parts.append(f'<path d="{d}" class="{cls}">{tip}</path>')

    def line(self, x1, y1, x2, y2, cls):
        self.parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" class="{cls}" '
                          'stroke-width="1"/>')

    def badge(self, x, y, kind):
        """Status mark: a filled circle with a glyph, always paired with a text label by the caller."""
        cls, glyph, ink = {"crit": ("crit", "!", "on"), "warn": ("warn", "!", "on-dark"),
                           "good": ("good", "✓", "on")}[kind]
        self.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" class="{cls}"/>')
        self.parts.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" font-size="11" text-anchor="middle" '
                          f'class="b {ink}">{glyph}</text>')


def render_svg(rep: dict, theme: str = "auto") -> str:
    """The report as one SVG picture: what loads at startup, the heaviest files, the skill
    listing, measured sessions and what to do. Same numbers as the text report."""
    W, L, R = 960, 40, 920
    files, skills, stats = rep["files"], rep["skills"], rep["sessions"]
    target, days = rep["target"], rep["days"]
    agents = [h for h in HARNESSES if h in files]
    g = _Svg()
    y = 52.0

    g.text(L, y, "context-weight report", 13, "s b")
    g.text(R, y, f"last {days} days, since {rep['since']}", 13, "m r")
    y += 34
    g.text(L, y, _fit(short(rep["project"]), R - L, 26), 26, "b")
    y += 22
    g.text(L, y, "Instruction sizes are estimates. Session numbers are measured from local logs.", 13, "m")
    y += 26

    # hero: the heaviest startup load, then up to three stat tiles
    heavy = max(agents, key=lambda h: files[h]["total_tokens"], default=None)
    hero_top = y
    if heavy:
        g.text(L, y + 54, f"~{fmt_k(files[heavy]['total_tokens'])}", 52, "b")
        caption = f"tokens of instruction files load at startup in {AGENT_NAMES[heavy]}"
        for j, ln in enumerate(_wrap(caption, 300, 14)[:2]):
            g.text(L, y + 80 + 18 * j, ln, 14, "s")
    # Every tile describes the hero's agent: mixing agents in one row reads as one agent's numbers.
    tiles = []
    measured = {h: s for h, s in stats.items() if s}
    s = measured.get(heavy)
    if s:
        name = AGENT_NAMES[heavy]
        tiles.append(("Measured startup", f"~{fmt_k(s['baseline_median'])}", f"median, {name}"))
        tiles.append(("Startup share", pct(s["startup_share"]), f"of {name} input"))
        cut = files[heavy]["repo_tokens"] - target
        if cut > FINDING_MIN_CUT and s["input_tokens"]:
            saved = cut * s["requests"]
            tiles.append(("Could save", f"~{fmt_k(saved)}", f"tokens, {pct(saved / s['input_tokens'])} of input"))
        else:
            n = s["sessions"]
            tiles.append(("Requests", f"{s['requests']:,}", f"in {n:,} session{'' if n == 1 else 's'}"))
    elif heavy:
        tiles.append(("Sessions", "none", f"{AGENT_NAMES[heavy]}, last {days} days"))
    else:
        tiles.append(("Sessions", "none", f"in the last {days} days"))
    tile_w = 186
    for i, (label, value, note) in enumerate(tiles):
        x = R - tile_w * (len(tiles) - i) + 16
        g.text(x, y + 22, _fit(label, tile_w - 20, 12, left=False), 12, "m")
        g.text(x, y + 52, value, 26, "b n")
        g.text(x, y + 74, _fit(note, tile_w - 20, 12, left=False), 12, "s")
    y = hero_top + 118
    g.line(L, y, R, y, "hl")

    # before and after the last slimming, when git shows one
    c = rep.get("compare")
    if c:
        y += 38
        g.text(L, y, "Before and after", 17, "b")
        g.text(R, y, _fit(_compare_label(c, 40), 700, 13, left=False), 13, "m r")
        y += 20
        g.text(L, y, f"This repo's instruction files before and right after the change. Sessions: the "
                     f"{c['days']} days before it against everything since.", 13, "m")
        y += 30
        cx_files, cx_start, cx_status = L + 130, L + 340, L + 560
        for x, label in ((L, "Agent"), (cx_files, "Instruction files"), (cx_start, "Startup, measured"),
                         (cx_status, "Sessions")):
            g.text(x, y, label, 12, "m")
        g.line(L, y + 10, R, y + 10, "hl")
        for h in _compare_agents(c):
            y += 34
            f = c["files"].get(h, {"before": 0, "after": 0})
            g.text(L, y, AGENT_NAMES[h], 14)
            g.text(cx_files, y, f"~{fmt_k(f['before'])} → ~{fmt_k(f['after'])}", 14, "b n")
            if fmt_k(f.get("now", f["after"])) != fmt_k(f["after"]):
                g.text(cx_files, y + 15, f"now ~{fmt_k(f['now'])}", 11, "s" if _grew_back(f) else "m")
            sd = c["sessions"].get(h) or {}
            b, a = sd.get("before"), sd.get("after")
            if b and a:
                g.text(cx_start, y, f"~{fmt_k(b['baseline_median'])} → ~{fmt_k(a['baseline_median'])}", 14, "b n")
            elif b:
                g.text(cx_start, y, f"~{fmt_k(b['baseline_median'])} before", 14, "n")
            kind, words = _compare_status(c, h)
            x = cx_status
            if kind:
                g.badge(x + 7, y - 5, kind)
                x += 22
            g.text(x, y, _fit(words, R - x, 13, left=False), 13, "s" if kind else "m")
        if any(fmt_k(f.get("now", f["after"])) != fmt_k(f["after"]) for f in c["files"].values()):
            y += 12
        lines = [ln for note in _compare_notes(c, "→") for ln in _wrap(note, R - L, 13)]
        for i, ln in enumerate(lines):
            y += 30 if i == 0 else 20
            g.text(L, y, ln, 13, "m")
        hint = _compare_hint(c)
        if hint:
            y += 30
            g.text(L, y, hint, 13, "m")
        y += 30
        g.line(L, y, R, y, "hl")

    # startup load per agent: stacked bar, this repo vs everything outside it
    y += 38
    g.text(L, y, "Instruction files at startup", 17, "b")
    lx = R
    for cls, label in (("other", "Outside this repo"), ("repo", "This repo")):
        lx -= _text_px(label, 13) + 34
        g.rect(lx, y - 10, 12, 12, cls, rx=3)
        g.text(lx + 18, y, label, 13, "s")
    y += 20
    g.text(L, y, "Estimated tokens each agent reads before your first message, sent again with every request.",
           13, "m")
    y += 22
    x0, x1 = L + 130, R - 92
    top = max([files[h]["total_tokens"] for h in agents] + [target, 1])
    scale = (x1 - x0) / top
    rows_top = y
    for h in agents:
        f = files[h]
        repo_t, total = f["repo_tokens"], f["total_tokens"]
        other_t = max(0, total - repo_t)
        cy = y + 20
        g.text(L, cy + 5, AGENT_NAMES[h], 14)
        n = len(f["files"])
        if total == 0:
            g.text(x0, cy + 5, "nothing loads", 13, "m")
        else:
            w_repo, w_other = repo_t * scale, other_t * scale
            tip = f"{AGENT_NAMES[h]}: ~{fmt_k(total)} tokens from {n} file{'' if n == 1 else 's'}"
            g.bar(x0, cy - 9, w_repo, 18, "repo", round_end=w_other < 1,
                  title=f"{tip}; this repo ~{fmt_k(repo_t)}")
            gap = 2 if w_repo > 0 and w_other > 0 else 0
            g.bar(x0 + w_repo + gap, cy - 9, w_other - gap, 18, "other", title=f"{tip}; outside ~{fmt_k(other_t)}")
            g.text(x0 + total * scale + 10, cy + 5, f"~{fmt_k(total)}", 14, "b n")
        y += 40
    tx = x0 + target * scale
    g.line(tx, rows_top + 2, tx, y - 2, "tgt")
    g.text(tx + 4, rows_top + 2, f"target ~{fmt_k(target)}", 11, "m")
    y += 10

    # heaviest files, each counted once even when several agents load it
    seen: dict[str, dict] = {}
    root = Path(rep["project"])
    for h in agents:
        for e in files[h]["files"]:
            item = seen.setdefault(e["path"], {"tokens": 0, "agents": [], "repo": _inside(e["path"], root),
                                               "cut": 0})
            item["tokens"] = max(item["tokens"], e.get("file_tokens", e["tokens"]))
            item["agents"].append(AGENT_NAMES[h])
            item["cut"] = max(item["cut"], e.get("truncated_bytes") or 0)
    heaviest = sorted(seen.items(), key=lambda kv: -kv[1]["tokens"])[:5]
    if heaviest:
        g.line(L, y, R, y, "hl")
        y += 38
        g.text(L, y, "Heaviest files", 17, "b")
        y += 14
        peak = max(1, heaviest[0][1]["tokens"])
        for path, it in heaviest:
            y += 30
            g.text(L, y, _fit(short(path), 390, 14), 14)
            note = "loaded by " + ", ".join(it["agents"])
            g.text(L, y + 18, _fit(note, 390, 12, left=False), 12, "m")
            bw = 330 * it["tokens"] / peak
            g.bar(L + 410, y - 8, bw, 8, "repo" if it["repo"] else "other",
                  title=f"{short(path)}: ~{fmt_k(it['tokens'])} tokens")
            g.text(R, y, f"~{fmt_k(it['tokens'])}", 14, "b n r")
            if it["cut"]:
                g.badge(L + 410 + bw + 14, y - 4, "crit")
                g.text(R, y + 18, f"Codex cuts {it['cut']:,} bytes", 12, "s r")
            y += 18
        y += 18

    # skill listing against each agent's budget
    budgets = [(h, r) for h, r in skills.items() if r.get("budget")]
    if budgets:
        g.line(L, y, R, y, "hl")
        y += 38
        g.text(L, y, "Skill listing", 17, "b")
        y += 20
        g.text(L, y, "Names and descriptions of installed skills, also loaded at startup.", 13, "m")
        y += 12
        for h, r in budgets:
            y += 30
            used = r["chars"] if r["budget_unit"] == "chars" else r["tokens"]
            share = used / r["budget"]
            g.text(L, y + 4, AGENT_NAMES[h], 14)
            mx0, mw = L + 130, 360
            g.rect(mx0, y - 4, mw, 8, "track", rx=4)
            kind = "crit" if share > 1 else "warn" if share > 0.8 else "good"
            g.rect(mx0, y - 4, mw * min(1.0, share), 8, kind if kind != "good" else "repo", rx=4)
            g.text(mx0 + mw + 20, y + 4, f"{fmt_k(used)} of {fmt_k(r['budget'])} {r['budget_unit']}", 13, "n")
            if kind != "good":
                label = (f"{share:.1f}× budget" if share >= 1.1 else f"{pct(share - 1)} over budget"
                         if share > 1 else "near budget")
                g.badge(R - _text_px(label, 13) - 14, y, kind)
                g.text(R, y + 4, label, 13, "b r")
        assumed = [AGENT_NAMES[h] for h, r in budgets if r.get("window_source") == "default"]
        if assumed:
            y += 30
            g.text(L, y, f"{' and '.join(assumed)}: budget assumes a {DEFAULT_WINDOW:,}-token window. "
                         "For a 1M-context model, pass --window 1000000.", 12, "m")
        y += 26

    # measured sessions
    g.line(L, y, R, y, "hl")
    y += 38
    g.text(L, y, "Sessions in this repo", 17, "b")
    y += 20
    sub_text = (f"Measured from local session logs, last {days} days. Startup is a session's first request: "
                "instruction files plus the system prompt, tools and skill listing.")
    for j, ln in enumerate(_wrap(sub_text, R - L, 13)):
        g.text(L, y + 18 * j, ln, 13, "m")
    y += 18 * j
    if measured:
        cols = [("Sessions", 330), ("Requests", 420), ("Startup", 510), ("Median", 600), ("Max", 690),
                ("Startup share", 800), ("Input", R)]
        y += 34
        g.text(L, y, "Agent", 12, "m")
        for label, cx in cols:
            g.text(cx, y, label, 12, "m r")
        g.line(L, y + 10, R, y + 10, "hl")
        for h, s in measured.items():
            y += 34
            g.text(L, y, AGENT_NAMES[h], 14)
            vals = [f"{s['sessions']:,}", f"{s['requests']:,}", fmt_k(s["baseline_median"]),
                    fmt_k(s["ctx_median"]), fmt_k(s["ctx_max"]), pct(s["startup_share"]), fmt_k(s["input_tokens"])]
            for (_, cx), v in zip(cols, vals):
                g.text(cx, y, v, 14, "n r")
        y += 14
    else:
        y += 30
        g.text(L, y, "No session logs for this repo in this window.", 14, "s")
    y += 26

    # findings
    found = rep["findings"]
    g.line(L, y, R, y, "hl")
    y += 38
    g.text(L, y, "What to do", 17, "b")
    if not found:
        y += 32
        near = _over_target(files, target)
        g.badge(L + 7, y - 5, "warn" if near else "good")
        note = _fit(near, R - L - 24, 14) if near else "Nothing to fix: startup context is within target."
        g.text(L + 24, y, note, 14)
    for i, item in enumerate(found[:6], 1):
        y += 32
        g.text(L, y, f"{i}.", 14, "b n")
        m = re.match(r"\[(\w+)\] ", item)
        who = (AGENT_NAMES.get(m.group(1), "All agents") + ": ") if m else ""
        lines = _wrap(who + (item[m.end():] if m else item), R - L - 26, 14) or [""]
        for j, ln in enumerate(lines):
            if j == 0 and who and ln.startswith(who):
                g.lead(L + 26, y, who, ln[len(who):])
            else:
                g.text(L + 26, y + 20 * j, ln, 14)
        y += 20 * (len(lines) - 1)
    if len(found) > 6:
        y += 28
        g.text(L + 26, y, f"{len(found) - 6} more in the text report: cw.py report", 13, "m")
    y += 34
    g.line(L, y, R, y, "hl")
    y += 26
    g.text(L, y, f"cw.py {VERSION} · about 3 ASCII characters, or 1 Thai or CJK character, per token", 12, "m")
    g.text(R, y, "github.com/ohm41321/context-weight", 12, "m r")
    H = y + 28

    summary = (f"context-weight report for {short(rep['project'])}: " +
               "; ".join(f"{AGENT_NAMES[h]} loads about {fmt_k(files[h]['total_tokens'])} tokens at startup"
                         for h in agents))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H:.0f}" width="{W}" height="{H:.0f}" '
            f'role="img" aria-labelledby="cw-title cw-desc">'
            f'<title id="cw-title">context-weight report</title><desc id="cw-desc">{_xml(summary)}</desc>'
            f"<style>{_svg_style(theme)}</style>"
            f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1:.0f}" rx="14" class="bg"/>'
            + "".join(g.parts) + "</svg>\n")


def write_picture(rep: dict, out: str, theme: str) -> Path:
    path = Path(out).expanduser()
    try:
        path.write_text(render_svg(rep, theme), encoding="utf-8")
    except OSError as e:
        raise UsageError(f"cannot write {out}: {e.strerror or e}")
    return path


# --------------------------------------------------------------------------- verify-split

_FENCE_OPEN = re.compile(r"(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"(`{3,}|~{3,}) *$")
_LIST_MARKER = re.compile(r"(?:[-+*]|([0-9]{1,9})[.)])(?= |$)")  # ASCII digits: \d takes any
_THEMATIC_BREAK = re.compile(r"([-*_])(?: *\1){2,} *$")
_SETEXT_RULE = re.compile(r"(?:=+|-+) *$")
_ATX_HEADING = re.compile(r"#{1,6}(?: |$)")
_TABLE_RULE = re.compile(r"\|?[ \t\v\f]*:?-+:?[ \t\v\f]*(?:\|[ \t\v\f]*:?-+:?[ \t\v\f]*)*\|?[ \t\v\f]*$")
_TABLE_PIPE = re.compile(r"(?<!\\)\|[ \t\v\f]*")
# CommonMark's HTML blocks as cmark-gfm scans them: how types 1 to 6 start, how types 1 to 5 end
# (somewhere in a line, the first one too), and the lone tag that starts type 7. Letter case is
# ignored in ASCII only (re.A): plain re.I takes a long s for s, a dotless i for i, a Kelvin sign for k.
_HTML_START = (
    re.compile(r"<(?:pre|script|style|textarea)(?:[ \t\v\f>]|$)", re.I | re.A),
    re.compile(r"<!--"),
    re.compile(r"<\?"),
    re.compile(r"<![A-Z]"),
    re.compile(r"<!\[CDATA\[", re.I | re.A),
    re.compile(r"</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|"
               r"dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|"
               r"html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|section|"
               r"source|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?:[ \t\v\f]|/?>|$)", re.I | re.A),
)
_HTML_END = (re.compile(r"</(?:pre|script|style|textarea)>", re.I | re.A), re.compile("-->"), re.compile(r"\?>"),
             re.compile(">"), re.compile(r"\]\]>"))
_HTML_ATTR = r"""[ \t\v\f]+[A-Za-z_:][A-Za-z0-9_.:-]*(?:[ \t\v\f]*=[ \t\v\f]*(?:[^ \t\v\f"'=<>`]+|'[^']*'|"[^"]*"))?"""
_HTML_TAG = re.compile(r"<(?:[A-Za-z][A-Za-z0-9-]*(?:" + _HTML_ATTR + r")*[ \t\v\f]*/?>|/[A-Za-z][A-Za-z0-9-]*[ \t\v\f]*>)[ \t\f]*$")


def _nonspace(line: str, pos: int) -> int:
    """Where the first character that is not a space at or after `pos` is (len(line) if none)."""
    return len(line) - len(line[pos:].lstrip(" "))


def _index(raw: str, col: int) -> int:
    """The index in `raw` of the character that holds column `col` once tabs are expanded to
    four columns (len(raw) if none does): inside a tab, the tab."""
    width = 0
    for i, ch in enumerate(raw):
        width += 4 - width % 4 if ch == "\t" else 1
        if width > col:
            return i
    return len(raw)


def _cut(raw: str, col: int) -> str:
    """`raw` from column `col` on, as CommonMark keeps code: tabs stay tabs, and the columns
    of a tab that `col` splits, from `col` on, become spaces."""
    i = _index(raw, col)
    if i < len(raw) and raw[i] == "\t":
        start = len(raw[:i].expandtabs(4))
        if start < col:
            return " " * (start + 4 - start % 4 - col) + raw[i + 1:]
    return raw[i:]


def _cells(row: str) -> int:
    """How many cells GFM reads in a table row, as cmark-gfm counts them: a pipe ends a cell
    unless a backslash comes right before it, a pipe that opens the row opens no cell, and
    what follows the last pipe is a cell if anything does."""
    if row.startswith("|"):
        row = row[1:].lstrip(" \t\v\f")
    parts = _TABLE_PIPE.split(row)
    return len(parts) - 1 + (parts[-1] != "")


def _blocks(text: str):
    """Yield (is_code, line) for each line of `text` but fence markers: code as its block holds
    it, without the prefixes of its containers or the block's own indentation and with its tabs
    (what that indentation leaves of a tab, as spaces), any other line with tabs expanded to four
    columns, as CommonMark reads structure.

    What makes a line code follows GitHub Flavored Markdown (CommonMark and its tables), as
    GitHub shows it. Block quotes and list items hold blocks. A fence opens at most three
    spaces past where the text of its container starts (the margin outside one), and a
    backtick fence's info string holds no backtick, so a line that opens with inline code is
    prose. A block closes on a bare run of its own character at least as long as the one that
    opened it (cmark-gfm counts at most 255), within three spaces, or where its container
    ends. A line four or more spaces past where its container's text starts, not continuing a
    paragraph, is indented code. A paragraph's lazy lines, indented less than its container,
    keep that container open. A list item interrupts a paragraph only if it is not empty and,
    when numbered, starts at 1; a line of - under a paragraph underlines a heading. A
    delimiter row (`| --- | :-: |`) right under a paragraph line with as many cells turns that
    line into a table's header; a table is no paragraph, so a line four spaces in under a row
    is code and any list item can start there, and it ends at a line with no cell. An HTML
    block (CommonMark's seven types) holds no code: a comment, `<pre>`, `<script>`, `<style>`,
    `<textarea>`, `<?`, `<!X` or `<![CDATA[` runs to the line that holds its end, past blank
    lines; a block tag such as `<div>`, or any other tag alone on its line, runs to a blank
    line, and the lone tag cannot interrupt a paragraph. Lines end only at \\n, \\r\\n or \\r.
    Not modelled: footnote definitions (GitHub reads them), and a paragraph made only of link
    reference definitions, which takes no heading underline (cmark-gfm reads a `---` or `===`
    under one as text)."""
    stack: list = []  # open containers, outermost first: [width, empty] for a list item, None for a block quote
    leaf = None  # the open leaf block: "para", "table", "code" (indented), (char, length, indent) for a fence,
    # or 1 to 7 for an HTML block of that CommonMark type
    last, tried = "", False  # the open paragraph's last line as it holds it; whether a delimiter row came
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.expandtabs(4)
        pos = n = 0  # n: how many containers this line continues
        for c in stack:
            at = _nonspace(line, pos)
            if c is None:
                if at - pos > 3 or not line.startswith(">", at):
                    break
                pos = at + 1 + line.startswith(" ", at + 1)
            elif at - pos >= c[0]:
                pos += c[0]
            elif at == len(line) and not c[1]:
                pos = at  # a blank line continues a list item that holds something
            else:
                break
            n += 1
        if n < len(stack) and leaf != "para":
            del stack[n:]  # the containers it misses end, and the code they held
            leaf = None
        at = _nonspace(line, pos)
        if isinstance(leaf, tuple):
            char, length, indent = leaf
            m = _FENCE_CLOSE.match(line, at)
            if at - pos <= 3 and m and m.group(1)[0] == char and len(m.group(1)) >= length:
                leaf = None
            else:
                yield True, _cut(raw, min(pos + indent, at))
            continue
        if leaf == "code":
            if at - pos >= 4 or at == len(line):
                yield True, _cut(raw, pos + 4)
                continue
            leaf = None
        if isinstance(leaf, int):  # an HTML block
            if leaf <= 5 or at < len(line):
                if leaf <= 5 and _HTML_END[leaf - 1].search(line, at):
                    leaf = None
                yield False, line
                continue
            leaf = None  # types 6 and 7 end before a blank line
        if leaf == "table" and not _cells(line[at:]):
            leaf = None  # a line with no cell (blank, or a lone pipe) ends the table
        lazy = leaf == "para"  # the line may continue a paragraph, lazily if it missed a container
        in_para = lazy and n == len(stack) and at < len(line)
        opened = False
        new_item = None
        kind = None
        while True:  # open the containers the line starts, then at most one leaf block
            indent, rest = at - pos, line[at:]
            if indent >= 4 or not rest:
                break
            if rest[0] == ">":
                del stack[n:]
                stack.append(None)
                pos = at + 1 + line.startswith(" ", at + 1)
            else:
                m = _FENCE_OPEN.match(rest)
                if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                    # cmark-gfm keeps the length in a byte, and strips from the code as many columns
                    # as the fence's indentation has characters (a tab is one)
                    kind = (m.group(1)[0], min(len(m.group(1)), 255), _index(raw, at) - _index(raw, pos))
                    break
                if rest[0] == "<":
                    html = next((i for i, start in enumerate(_HTML_START, 1) if start.match(rest)), 0)
                    if html or (not in_para and _HTML_TAG.match(rest)):
                        kind = html or 7  # type 7 cannot interrupt a paragraph
                        break
                if _ATX_HEADING.match(rest) or (in_para and _SETEXT_RULE.match(rest)) or _THEMATIC_BREAK.match(rest):
                    kind = "line"
                    break
                mk = _LIST_MARKER.match(rest)
                if not mk:
                    break
                after = rest[mk.end():]
                empty = not after.strip(" ")
                if in_para and (empty or (mk.group(1) is not None and int(mk.group(1)) != 1)):
                    break  # no list item can interrupt this paragraph: the line continues it
                gap = len(after) - len(after.lstrip(" "))
                wide = empty or gap > 4  # an item's text starts one space past its marker if more is code
                del stack[n:]
                new_item = [indent + mk.end() + (1 if wide else gap), empty]
                stack.append(new_item)
                pos = at + mk.end() + (min(gap, 1) if wide else gap)
            n, leaf, lazy, in_para, opened = len(stack), None, False, False, True
            at = _nonspace(line, pos)
        if kind is not None:
            del stack[n:]
            leaf = None if kind == "line" else kind
            if not isinstance(kind, tuple):
                yield False, line  # a heading, a rule or an HTML block's first line
                if isinstance(kind, int) and kind <= 5 and _HTML_END[kind - 1].search(line, at):
                    leaf = None  # an HTML block that ends where it starts
        elif at == len(line):
            del stack[n:]  # a blank line ends a paragraph, and the containers it misses
            leaf = None
            if not opened:
                continue  # and puts nothing in an item: one opened empty still holds nothing
        elif at - pos >= 4 and not lazy:
            del stack[n:]
            leaf = "code"
            yield True, _cut(raw, pos + 4)
        else:
            if leaf == "table":
                pass  # a row: under a table any line with a cell is one
            elif in_para and at - pos < 4 and not tried and _TABLE_RULE.match(line, at):
                tried = True  # cmark-gfm tries a paragraph once: this row makes a table or text
                if _cells(line[at:]) == _cells(last):
                    leaf = "table"
            elif lazy:  # the line continues a paragraph, lazily if it missed a container
                last = line[at:] if in_para else line[pos:]
            else:  # a new paragraph
                del stack[n:]
                leaf, last, tried = "para", line[at:], False
            yield False, line
        for c in stack:
            if c is not None and c is not new_item:
                c[1] = False  # every open item holds something now, but one opened empty here


def _split_lines(text: str) -> list[str]:
    """Lines that carry content, normalised for comparison.

    Each line is compared on its own. Prose: surrounding whitespace dropped and inner runs
    collapsed, so re-indenting a line or changing its spacing is fine, but moving a line break
    changes both lines and both show as missing. Code, fenced or indented, as _blocks finds
    it: indentation relative to its block is kept, because it carries meaning there (YAML,
    Python), so code may move into or out of a list item or a block quote. Fence markers and
    lines with no letter or digit (rules, table separators) are structure and are skipped."""
    out: list[str] = []
    for is_code, line in _blocks(text):
        if not any(c.isalnum() for c in line):
            continue
        if is_code:
            body = line.rstrip()
            out.append(body[:len(body) - len(body.lstrip(" \t"))] + _norm(body))  # indentation as it is, tabs too
        else:
            out.append(_norm(line))
    return out


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _shown(line: str) -> str:
    """A line as the text output prints it: a tab as \\t and any other control or format
    character escaped, so a tab that became spaces shows and no line sends terminal escapes."""
    def one(ch: str) -> str:
        if ch == "\t":
            return "\\t"
        if unicodedata.category(ch) not in ("Cc", "Cf"):
            return ch
        cp = ord(ch)
        return f"\\x{cp:02x}" if cp < 0x100 else f"\\u{cp:04x}" if cp < 0x10000 else f"\\U{cp:08x}"
    return "".join(one(ch) for ch in line)


def verify_split(original: str, new_paths: list[str], allow: str | None) -> dict:
    """Every content line of ORIGINAL must appear in the new files at least as many times as
    it did in ORIGINAL, unless the allow list names it."""
    if not original or "" in new_paths:  # an unset variable, not the current folder
        raise UsageError("an empty path was given")
    text = read_text(original)
    if text is None:
        raise UsageError(f"cannot read {original}")
    want = Counter(_split_lines(text))
    try:
        orig_real = Path(original).resolve()
    except (OSError, RuntimeError):
        orig_real = None
    have: Counter = Counter()
    read = []
    seen_real: set = set()
    for p in new_paths:
        if _is_dir(p):
            targets = _md_files(Path(p))
        elif _is_file(p):
            targets = [Path(p)]
        else:
            raise UsageError(f"cannot read {p}")
        for t in targets:
            try:
                t_real = t.resolve()
            except (OSError, RuntimeError):
                raise UsageError(f"cannot read {t}")
            if t_real == orig_real:
                continue  # the backup itself never counts as the new copy
            if t_real in seen_real:
                continue  # one file reached twice (symlink, or a file plus its folder) counts once
            seen_real.add(t_real)
            body = read_text(t)
            if body is None:
                raise UsageError(f"cannot read {t}")
            have.update(_split_lines(body))
            read.append(str(t))
    allowed: set[str] = set()
    if allow:
        listed = read_text(allow)
        if listed is None:
            raise UsageError(f"cannot read {allow}")
        allowed = {_norm(x) for x in listed.splitlines()}
    missing, copies = [], {}
    for line, n in want.items():
        short_by = n - have[line]
        if short_by > 0 and _norm(line) not in allowed:
            missing.append(line)
            if n > 1:
                copies[line] = [short_by, n]
    return {"checked": sum(want.values()), "unique": len(want), "files_read": len(read),
            "missing": missing, "missing_copies": copies}


# --------------------------------------------------------------------------- CLI

def _harnesses(arg: str) -> list[str]:
    if arg == "all":
        return list(HARNESSES)
    hs = [h.strip() for h in arg.split(",") if h.strip()]
    bad = [h for h in hs if h not in HARNESSES]
    if bad or not hs:
        raise UsageError(f"unknown harness: {', '.join(bad) or arg!r} (use {', '.join(HARNESSES)} or all)")
    return list(dict.fromkeys(hs))  # claude,claude must not read every log twice


def _project_dir(path: str) -> str:
    """A project path must be an existing directory: a typo must not pass a CI gate, and an
    empty one (an unset variable) must not stand for the current folder."""
    if not path:
        raise UsageError("empty project path (pass . for the current folder)")
    if not _is_dir(path):
        raise UsageError(f"not a directory: {path}" if _exists(path) else f"no such directory: {path}")
    return path


def _since(days: int) -> str:
    """First day of a window of `days` days; a window longer than the calendar starts in year 1."""
    try:
        return (dt.date.today() - dt.timedelta(days=days)).isoformat()
    except OverflowError:
        return dt.date.min.isoformat()


def _date(s: str | None, flag: str) -> str | None:
    if s is None:
        return None
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise UsageError(f"{flag} wants YYYY-MM-DD, got {s!r}")


def _emit(obj, as_json: bool, printer) -> None:
    if as_json:
        json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
        print()
    else:
        printer(obj)


_RESULT = [EXIT_OK]  # the exit code decided so far, kept for a reader that leaves early
_WRITE_ERRORS: list = []  # every write to stdout or stderr that failed in this run


class _WriteFailed(Exception):
    """A write to stdout or stderr failed. Not an OSError, so no `except OSError` meant for a
    file that cannot be read takes it for one."""


class _Output:
    """stdout or stderr during a run: a failed write is recorded, then raised as _WriteFailed."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        try:
            return self.stream.write(text)
        except OSError as e:
            _WRITE_ERRORS.append(e)
            raise _WriteFailed() from e

    def flush(self):
        try:
            self.stream.flush()
        except OSError as e:
            _WRITE_ERRORS.append(e)
            raise _WriteFailed() from e

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _reader_left(e: OSError) -> bool:
    """The reader closed the pipe (`| head`). Windows reports that as EINVAL."""
    return isinstance(e, BrokenPipeError) or (os.name == "nt" and e.errno == errno.EINVAL)


def _settle_streams(streams) -> None:
    """Flush stdout and stderr. One that cannot take its output (`| head`, a full disk) is
    pointed at devnull, as Python's docs advise, so the flush at exit cannot fail again and
    exit 120."""
    for stream in streams:
        try:
            stream.flush()
        except (OSError, ValueError) as e:
            if isinstance(e, OSError):
                _WRITE_ERRORS.append(e)
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())
            except (OSError, ValueError, AttributeError):
                pass


class _Parser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        # As Python 3.13+ does: before it, a failed write of usage or help escaped and skipped
        # argparse's own exit code. _Output has recorded the failure.
        try:
            super()._print_message(message, file)
        except (OSError, _WriteFailed):
            pass


def main(argv=None) -> int:
    # Started with no stdout or stderr at all (`>&-`, pythonw), Python sets them to None, and
    # print(file=None) would even send errors to stdout. Write to devnull instead.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    for stream in (sys.stdout, sys.stderr):
        try:  # a Windows console in cp1252 must not crash on a Thai or CJK path
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    _RESULT[0] = EXIT_OK
    del _WRITE_ERRORS[:]
    streams = (sys.stdout, sys.stderr)
    sys.stdout, sys.stderr = _Output(streams[0]), _Output(streams[1])
    try:
        try:
            _RESULT[0] = _main(argv)
        except UsageError as e:
            _RESULT[0] = EXIT_USAGE
            print(f"cw.py: error: {e}", file=sys.stderr)
        except SystemExit as e:  # argparse, after --help, --version or a bad argument
            _RESULT[0] = EXIT_OK if e.code is None else e.code if isinstance(e.code, int) else EXIT_USAGE
    except _WriteFailed:  # `cw.py files . --max-tokens 6000 | head`: a failed gate still exits 1
        pass
    finally:
        sys.stdout, sys.stderr = streams
    _settle_streams(streams)  # a failed write shows up here at the latest, not at interpreter exit
    lost = next((e for e in _WRITE_ERRORS if not _reader_left(e)), None)
    if lost is not None:  # not a reader that left: output is lost (a full disk), so the run fails
        _RESULT[0] = EXIT_USAGE
        try:
            print(f"cw.py: error: cannot write output: {lost.strerror or lost}", file=sys.stderr)
        except (OSError, ValueError):
            pass
        _settle_streams((sys.stderr,))  # stderr may fail too (`2>&1` into the same full disk)
    return _RESULT[0]


def _main(argv=None) -> int:
    ap = _Parser(prog="cw.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, path=True):
        if path:
            p.add_argument("path", nargs="?", default=".", help="project directory (default: .)")
        p.add_argument("--harness", default="all", help="claude,codex,gemini,opencode or all")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("files", help="instruction files each harness loads at startup")
    common(p)
    p.add_argument("--max-tokens", type=int, help="exit 1 if this repo's instructions exceed N tokens for any "
                   "harness, or if Codex would cut any of them off; exit 2 on a bad path")

    window_help = (f"Claude Code's context window in tokens, which its logs do not record (default {DEFAULT_WINDOW}). "
                   "Codex reads its own window from its logs and uses this only when they have none")
    p = sub.add_parser("skills", help="skill listing size against budgets")
    common(p)
    p.add_argument("--window", type=int, help=window_help)

    p = sub.add_parser("sessions", help="context size per project from session logs")
    common(p, path=False)
    p.add_argument("--project", help="substring of the project path")
    p.add_argument("--since", help="YYYY-MM-DD, sessions starting on/after")
    p.add_argument("--until", help="YYYY-MM-DD, sessions starting before")
    p.add_argument("--compare-at", help="YYYY-MM-DD, print before/after this date")
    p.add_argument("--threshold", type=int, default=150_000, help="large-context threshold (default 150000)")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--include-subagents", action="store_true")

    p = sub.add_parser("report", help="files + skills + sessions for one project, with findings")
    common(p)
    p.add_argument("--days", type=int, default=30, help="session window in days (default 30)")
    p.add_argument("--threshold", type=int, default=150_000)
    p.add_argument("--window", type=int, help=window_help)
    p.add_argument("--target", type=int, default=5_000, help="target tokens for repo instructions (default 5000); "
                   f"findings start {FINDING_MIN_CUT:,} tokens above it")
    p.add_argument("--compare-at", metavar="WHEN",
                   help="YYYY-MM-DD, YYYY-MM-DDTHH:MM or a commit id: compare before and after this "
                   "instead of the slimming the report finds in git by itself")
    p.add_argument("--svg", nargs="?", const="", metavar="FILE",
                   help="also draw the report as a picture (SVG); without FILE it goes to the temp folder")
    p.add_argument("--theme", choices=("auto", "light", "dark"), default="auto",
                   help="picture colors; auto follows the viewer's light or dark mode (default)")
    p.add_argument("--open", action="store_true", help="draw the picture and open it (implies --svg)")

    p = sub.add_parser("verify-split", help="every line of OLD must exist in NEW files (or the allow list)")
    p.add_argument("old")
    p.add_argument("new", nargs="+", help="files or directories (*.md searched recursively)")
    p.add_argument("--allow", help="file listing lines removed on purpose")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    if getattr(a, "path", None) is not None:
        _project_dir(a.path)
    for flag in ("since", "until", "compare_at"):
        if getattr(a, flag, None) and not (a.cmd == "report" and flag == "compare_at"):
            setattr(a, flag, _date(getattr(a, flag), "--" + flag.replace("_", "-")))
    if a.cmd == "report" and a.compare_at:
        a.compare_at = _compare_at(a.compare_at)
    for flag in ("max_tokens", "window", "threshold", "days", "top", "target"):
        v = getattr(a, flag, None)
        if v is not None and v < 0:
            raise UsageError(f"--{flag.replace('_', '-')} must not be negative")

    if a.cmd == "files":
        res = scan_files(a.path, _harnesses(a.harness))
        # Judge the gate before printing: a reader that leaves early must not turn a fail into 0.
        blind, fails = [], []
        if a.max_tokens is not None:
            root = project_root(a.path)
            blind = [u for u in UNREADABLE if _inside(u, root)]
            for h in HARNESSES:
                r = res.get(h)
                if not r:
                    continue
                if r["repo_file_tokens"] > a.max_tokens:
                    fails.append(f"FAIL {h}: repo instructions ~{fmt_k(r['repo_file_tokens'])} tokens > "
                                 f"{fmt_k(a.max_tokens)}")
                for e in r["files"]:
                    if e.get("truncated_bytes"):
                        fails.append(f"FAIL {h}: {short(e['path'])} has {e['truncated_bytes']:,} bytes past "
                                     "project_doc_max_bytes - Codex never sees them")
        _RESULT[0] = EXIT_USAGE if blind else EXIT_FAIL if fails else EXIT_OK
        _emit(res, a.json, print_files)
        for u in UNREADABLE:
            print(f"warning: cannot read {short(u)}; it is not counted", file=sys.stderr)
        if blind:
            raise UsageError(f"--max-tokens cannot judge {len(blind)} repo file(s) it could not read")
        for line in fails:
            print(line, file=sys.stderr)
        return _RESULT[0]

    if a.cmd == "skills":
        _emit(scan_skills(a.path, _harnesses(a.harness), a.window), a.json, print_skills)
        return EXIT_OK

    if a.cmd == "sessions":
        sessions = load_sessions(_harnesses(a.harness), a.include_subagents)
        if a.compare_at:
            before = group_sessions(sessions, a.since, a.compare_at, a.project)
            after = group_sessions(sessions, a.compare_at, a.until, a.project)
            if a.json:
                _emit({"before": {f"{h} {k}": summarize(v, a.threshold) for (h, k), v in before.items()},
                       "after": {f"{h} {k}": summarize(v, a.threshold) for (h, k), v in after.items()}}, True, None)
            else:
                print(f"== before {a.compare_at}")
                print_sessions(before, a.threshold, a.top)
                print(f"\n== from {a.compare_at}")
                print_sessions(after, a.threshold, a.top)
            return EXIT_OK
        groups = group_sessions(sessions, a.since, a.until, a.project)
        if a.json:
            _emit({f"{h} {k}": summarize(v, a.threshold) for (h, k), v in groups.items()}, True, None)
        else:
            print_sessions(groups, a.threshold, a.top)
        return EXIT_OK

    if a.cmd == "report":
        hs = _harnesses(a.harness)
        path = str(_true_case(a.path))  # git and the logs spell folders as the disk does
        root = str(project_root(path))
        since = _since(a.days)
        files = scan_files(path, hs)
        skills = scan_skills(path, hs, a.window)
        all_sessions = load_sessions(hs)
        # Sessions logged under another spelling of this folder (letter case) are this project too.
        groups: dict[tuple[str, str], list[Session]] = {}
        for (h, key), v in group_sessions(all_sessions, since=since).items():
            if _same_dir(key, project_key(root)):  # a worktree's sessions are its repository's
                groups.setdefault((h, root), []).extend(v)
        stats = {h: summarize(v, a.threshold) for (h, _), v in groups.items()}
        found = findings(files, skills, stats, a.target)
        cmp = compare(path, hs, files, all_sessions, a.days, a.threshold, a.compare_at)
        rep = {"project": root, "since": since, "files": files, "skills": skills,
               "sessions": stats, "findings": found}
        if cmp:
            rep["compare"] = cmp
        picture = None
        if a.svg is not None or a.open:
            out = a.svg
            if not out:
                # A fresh private folder: a fixed name in a shared /tmp could be a planted symlink.
                try:
                    folder = tempfile.mkdtemp(prefix="context-weight-")
                except OSError as e:
                    raise UsageError(f"cannot make a folder for the picture in {tempfile.gettempdir()}: {e}")
                slug = re.sub(r"[^\w.-]+", "-", Path(root).name) or "repo"
                out = os.path.join(folder, f"context-weight-{slug}.svg")
            picture = write_picture(dict(rep, days=a.days, target=a.target), out, a.theme)
            if a.open and not webbrowser.open(picture.resolve().as_uri()):
                print(f"cw.py: could not open a viewer; open {picture} yourself", file=sys.stderr)
        if a.json:
            if picture:
                rep["picture"] = str(picture)
            _emit(rep, True, None)
            return EXIT_OK
        print(f"project {short(root)}   sessions since {since}\n")
        if cmp:
            print_compare(cmp)
            print()
        print("## Instruction files loaded at startup (estimated tokens)")
        print_files(files)
        print("\n## Skill listing")
        print_skills(skills)
        print(f"\n## Sessions in this project (measured)")
        print_sessions(groups, a.threshold, 10)
        print("\n## Findings")
        for i, f in enumerate(found, 1):
            print(f"{i}. {f}")
        if not found:
            print(_over_target(files, a.target) or "none")
        if picture:
            print(f"\nPicture: {picture}")
        return EXIT_OK

    if a.cmd == "verify-split":
        res = verify_split(a.old, a.new, a.allow)
        missing = res["missing"]
        _RESULT[0] = EXIT_FAIL if missing else EXIT_OK  # kept if the reader leaves before the end
        if a.json:
            _emit(res, True, None)
        else:
            print(f"checked {res['checked']} lines of {a.old} against {res['files_read']} file(s): "
                  f"{len(missing)} missing")
            for m in missing[:40]:
                extra = ""
                if m in res["missing_copies"]:
                    short_by, n = res["missing_copies"][m]
                    extra = f"  ({short_by} of {n} copies)"
                print(f"  - {_shown(m[:160])}{extra}")
            if len(missing) > 40:
                print(f"  … {len(missing) - 40} more")
        return _RESULT[0]
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
