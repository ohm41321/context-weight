#!/usr/bin/env python3
"""context-weight: weigh what an agent harness loads before any work starts,
and how large its sessions grow.

Harnesses: Claude Code, Codex, Gemini CLI, OpenCode.
Reads instruction files, skill metadata and the token-usage numbers in local
session logs. Never prints message content and never changes your files; the one
file it writes is the picture you ask for with `report --svg`.

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
import glob
import json
import os
import re
import statistics
import sys
import tempfile
import webbrowser
from pathlib import Path
from xml.sax.saxutils import escape

VERSION = "0.2.0"
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
    except (TypeError, ValueError):
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
        entry = file_entry(target, f"{role} import", via=path)
        if entry:
            out.append(entry)
            _claude_imports(target, read_text(target) or "", depth + 1, seen, out, role)


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


def claude_files(cwd) -> dict:
    out: list[dict] = []
    seen: set[str] = set()
    notes: list[str] = []

    def add(p: Path, role: str) -> None:
        try:
            if not _is_file(p):
                return
            rp = str(p.resolve())
        except (OSError, RuntimeError):
            return
        if rp in seen:
            return
        seen.add(rp)
        entry = file_entry(p, role)
        if entry:
            out.append(entry)
            _claude_imports(Path(rp), read_text(p) or "", 1, seen, out, role)

    cd = claude_dir()
    add(cd / "CLAUDE.md", "user")
    eager, scoped = _rules(cd / "rules")
    for r in eager:
        add(r, "user rule")
    for d in top_down(cwd):
        add(d / "CLAUDE.md", "project")
        add(d / ".claude" / "CLAUDE.md", "project")
        add(d / "CLAUDE.local.md", "local")
    eager, scoped2 = _rules(project_root(cwd) / ".claude" / "rules")
    for r in eager:
        add(r, "project rule")
    # Auto memory: Claude Code loads the start of MEMORY.md (first 200 lines / 25 KB).
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project_root(cwd)))
    mem = cd / "projects" / slug / "memory" / "MEMORY.md"
    text = read_instruction(mem) if _is_file(mem) else None
    if text:
        head = "\n".join(text.splitlines()[:200]).encode("utf-8")[:25_000].decode("utf-8", errors="ignore")
        out.append({"path": str(mem), "role": "user memory", "via": None, "bytes": len(head.encode("utf-8")),
                    "chars": len(head), "lines": len(head.splitlines()), "tokens": est_tokens(head)})
    if scoped + scoped2:
        notes.append(f"{scoped + scoped2} path-scoped rule file(s) load on demand, not counted")

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


def _codex_config() -> tuple[int, list[str]]:
    text = read_text(codex_dir() / "config.toml") or ""
    max_bytes, fallbacks = CODEX_DOC_MAX_DEFAULT, []
    try:
        import tomllib  # Python 3.11+
        cfg = tomllib.loads(text)
        max_bytes = int(cfg.get("project_doc_max_bytes", max_bytes))
        fallbacks = [str(x) for x in cfg.get("project_doc_fallback_filenames", [])]
    except Exception:
        m = re.search(r"^project_doc_max_bytes\s*=\s*(\d+)", text, re.M)
        if m:
            max_bytes = int(m.group(1))
        m = re.search(r"^project_doc_fallback_filenames\s*=\s*\[(.*?)\]", text, re.M | re.S)
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


def gemini_files(cwd) -> dict:
    gd = home() / ".gemini"
    names = ["GEMINI.md"]
    try:
        settings = json.loads(read_text(gd / "settings.json") or "{}")
        fn = (settings.get("context") or {}).get("fileName") or settings.get("contextFileName")
        if fn:
            names = [fn] if isinstance(fn, str) else [str(x) for x in fn]
    except (ValueError, AttributeError):
        pass
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


def scan_files(cwd, harnesses) -> dict:
    root = project_root(cwd)
    result = {}
    for h in harnesses:
        r = FILE_SCANNERS[h](cwd)
        repo = [e for e in r["files"] if _inside(e["path"], root)]
        r["total_tokens"] = sum(e["tokens"] for e in r["files"])
        r["repo_tokens"] = sum(e["tokens"] for e in repo)                          # what the harness loads
        r["repo_file_tokens"] = sum(e.get("file_tokens", e["tokens"]) for e in repo)  # what the files hold
        result[h] = r
    if UNREADABLE:
        result["unreadable"] = list(UNREADABLE)
    # Same repo, different instructions per harness?
    c = [e for e in result.get("claude", {}).get("files", []) if e["role"] in ("project", "local")]
    x = [e for e in result.get("codex", {}).get("files", []) if e["role"] == "project"]
    if c and x:
        ct = sum(e["tokens"] for e in c)
        xt = sum(e["tokens"] for e in x)
        linked = any("@AGENTS.md" in (read_text(e["path"]) or "") for e in c) or any(
            Path(e["path"]).is_symlink() for e in c + x)
        if not linked and max(ct, xt) > 2 * max(1, min(ct, xt)):
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
        if window:
            w, w_src = window, "--window"
        else:
            w, w_src = _codex_window(), "codex logs"
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
    """Git root of the session's cwd; a deleted subdirectory folds into its repo."""
    p = Path(cwd)
    while not _exists(p) and p.parent != p:
        p = p.parent
    root = git_root(p)
    return str(root) if root else cwd


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
        if cut > 2_000:
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
        if h not in stats and f["repo_tokens"] - target > 2_000:
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
    tiles = []
    measured = {h: s for h, s in stats.items() if s}
    if measured:
        big = max(measured, key=lambda h: measured[h]["input_tokens"])
        s = measured[big]
        tiles.append(("Measured startup", f"~{fmt_k(s['baseline_median'])}", f"median, {AGENT_NAMES[big]}"))
        tiles.append(("Startup share", pct(s["startup_share"]), f"of {AGENT_NAMES[big]} input"))
        best = None
        for h, s2 in measured.items():
            f = files.get(h)
            cut = (f["repo_tokens"] - target) if f else 0
            if cut > 2_000:
                saved = cut * s2["requests"]
                if not best or saved > best[0]:
                    best = (saved, saved / s2["input_tokens"])
        if best:
            tiles.append(("Could save", f"~{fmt_k(best[0])}", f"tokens, {pct(best[1])} of input"))
        else:
            sessions_n = sum(x["sessions"] for x in measured.values())
            tiles.append(("Requests", f"{sum(x['requests'] for x in measured.values()):,}",
                          f"in {sessions_n:,} session{'' if sessions_n == 1 else 's'}"))
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
        g.badge(L + 7, y - 5, "good")
        g.text(L + 24, y, "Nothing to fix: startup context is within target.", 14)
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

_FENCE_LINE = re.compile(r"^(\s*)(`{3,}|~{3,})")


def _split_lines(text: str) -> list[str]:
    """Lines that carry content, normalised for comparison.

    Prose: surrounding whitespace dropped and inner runs collapsed, so re-wrapping or
    re-indenting a sentence is fine. Inside fenced code: indentation relative to the fence
    is kept, because it carries meaning there (YAML, Python). Fence markers and lines with
    no letter or digit (rules, table separators) are structure and are skipped."""
    out: list[str] = []
    fence = None  # (marker char, fence indent) while inside a code block
    for raw in text.splitlines():
        line = raw.expandtabs(4)
        m = _FENCE_LINE.match(line)
        if m:
            if fence is None:
                fence = (m.group(2)[0], len(m.group(1)))
                continue
            if m.group(2)[0] == fence[0] and not line.strip().strip(fence[0]):
                fence = None
                continue
        if not any(c.isalnum() for c in line):
            continue
        if fence is None:
            out.append(re.sub(r"\s+", " ", line.strip()))
        else:
            body = line[min(fence[1], len(line) - len(line.lstrip(" "))):].rstrip()
            indent = len(body) - len(body.lstrip(" "))
            out.append(" " * indent + re.sub(r"\s+", " ", body.strip()))
    return out


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def verify_split(original: str, new_paths: list[str], allow: str | None) -> dict:
    """Every content line of ORIGINAL must appear in the new files at least as many times as
    it did in ORIGINAL, unless the allow list names it."""
    from collections import Counter

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
    return hs


def _project_dir(path: str) -> str:
    """A project path must be an existing directory: a typo must not pass a CI gate."""
    if not _is_dir(path):
        raise UsageError(f"not a directory: {path}" if _exists(path) else f"no such directory: {path}")
    return path


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


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # a Windows console in cp1252 must not crash on a Thai or CJK path
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        return _main(argv)
    except UsageError as e:
        print(f"cw.py: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:  # `cw.py sessions | head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return EXIT_OK


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cw.py", description=__doc__.split("\n\n")[0])
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

    window_help = ("model context window in tokens (default: Codex reads it from its logs, "
                   f"otherwise {DEFAULT_WINDOW} is assumed)")
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
    p.add_argument("--target", type=int, default=5_000, help="target tokens for repo instructions (default 5000)")
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
        if getattr(a, flag, None):
            setattr(a, flag, _date(getattr(a, flag), "--" + flag.replace("_", "-")))
    for flag in ("max_tokens", "window", "threshold", "days", "top"):
        v = getattr(a, flag, None)
        if v is not None and v < 0:
            raise UsageError(f"--{flag.replace('_', '-')} must not be negative")

    if a.cmd == "files":
        res = scan_files(a.path, _harnesses(a.harness))
        _emit(res, a.json, print_files)
        for u in UNREADABLE:
            print(f"warning: cannot read {short(u)}; it is not counted", file=sys.stderr)
        if a.max_tokens is not None:
            root = project_root(a.path)
            blind = [u for u in UNREADABLE if _inside(u, root)]
            if blind:
                raise UsageError(f"--max-tokens cannot judge {len(blind)} repo file(s) it could not read")
        if a.max_tokens is not None:
            failed = False
            for h in HARNESSES:
                r = res.get(h)
                if not r:
                    continue
                if r["repo_file_tokens"] > a.max_tokens:
                    failed = True
                    print(f"FAIL {h}: repo instructions ~{fmt_k(r['repo_file_tokens'])} tokens > "
                          f"{fmt_k(a.max_tokens)}", file=sys.stderr)
                for e in r["files"]:
                    if e.get("truncated_bytes"):
                        failed = True
                        print(f"FAIL {h}: {short(e['path'])} has {e['truncated_bytes']:,} bytes past "
                              "project_doc_max_bytes - Codex never sees them", file=sys.stderr)
            return EXIT_FAIL if failed else EXIT_OK
        return EXIT_OK

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
            return 0
        groups = group_sessions(sessions, a.since, a.until, a.project)
        if a.json:
            _emit({f"{h} {k}": summarize(v, a.threshold) for (h, k), v in groups.items()}, True, None)
        else:
            print_sessions(groups, a.threshold, a.top)
        return 0

    if a.cmd == "report":
        hs = _harnesses(a.harness)
        root = str(project_root(a.path))
        since = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
        files = scan_files(a.path, hs)
        skills = scan_skills(a.path, hs, a.window)
        groups = {k: v for k, v in group_sessions(load_sessions(hs), since=since).items() if k[1] == root}
        stats = {h: summarize(v, a.threshold) for (h, _), v in groups.items()}
        found = findings(files, skills, stats, a.target)
        rep = {"project": root, "since": since, "files": files, "skills": skills,
               "sessions": stats, "findings": found}
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
            return 0
        print(f"project {short(root)}   sessions since {since}\n")
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
            print("none")
        if picture:
            print(f"\nPicture: {picture}")
        return 0

    if a.cmd == "verify-split":
        res = verify_split(a.old, a.new, a.allow)
        missing = res["missing"]
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
                print(f"  - {m[:160]}{extra}")
            if len(missing) > 40:
                print(f"  … {len(missing) - 40} more")
        return EXIT_FAIL if missing else EXIT_OK
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
