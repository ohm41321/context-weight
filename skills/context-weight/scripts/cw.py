#!/usr/bin/env python3
"""context-weight: weigh what an agent harness loads before any work starts,
and how large its sessions grow.

Harnesses: Claude Code, Codex, Gemini CLI, OpenCode.
Read-only. Reads instruction files, skill metadata and the token-usage numbers
in local session logs. Never prints message content.

  cw.py files    [PATH]          instruction files each harness loads at startup
  cw.py skills   [PATH]          skill listing size against each harness's budget
  cw.py sessions                 per-project context size measured from session logs
  cw.py report   [PATH]          all of the above for one project, plus findings
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
from pathlib import Path

VERSION = "0.1.0"
HARNESSES = ("claude", "codex", "gemini", "opencode")

CLAUDE_FILE_WARN_CHARS = 40_000   # Claude Code startup warning: "Large CLAUDE.md will impact performance"
CLAUDE_FILE_LINES = 200           # Claude Code docs: target under 200 lines per CLAUDE.md
CLAUDE_IMPORT_DEPTH = 4           # Claude Code docs: imports recurse at most four hops
CLAUDE_DESC_CAP = 1_536           # Claude Code: description + when_to_use truncated in the listing
CODEX_DOC_MAX_DEFAULT = 32 * 1024 # Codex: project_doc_max_bytes default


# --------------------------------------------------------------------------- helpers

def est_tokens(text: str) -> int:
    """Rough token count: ~3 ASCII chars per token, ~1 token per other char (Thai, CJK).

    Fitted against Claude Code logs (startup context minus a plain-project baseline)
    for one Thai-heavy and one English-heavy CLAUDE.md; within ~10% for both.
    `sessions` gives measured numbers when you need them.
    """
    n_ascii = sum(1 for c in text if ord(c) < 128)
    return round(n_ascii / 3 + (len(text) - n_ascii))


def read_text(path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
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
            if (d / ".git").exists():
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


def file_entry(path, role: str, via=None) -> dict | None:
    text = read_text(path)
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
        elif ref.startswith("/"):
            target = Path(ref)
        else:
            target = path.parent / ref
        try:
            if not target.is_file() or not _is_text(target):
                continue
            target = target.resolve()
        except OSError:
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
    if not directory.is_dir():
        return [], 0
    eager, scoped = [], 0
    for p in sorted(directory.rglob("*.md")):
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
            if not p.is_file():
                return
            rp = str(p.resolve())
        except OSError:
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
    text = read_text(mem) if mem.is_file() else None
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
        if p.is_file() and (read_text(p) or "").strip():
            out.append(file_entry(p, "global"))
            break
    max_bytes, fallbacks = _codex_config()
    root = project_root(cwd)
    dirs = [d for d in top_down(cwd) if d == root or root in d.parents]
    used = 0
    warnings = []
    for d in dirs:
        for name in ["AGENTS.override.md", "AGENTS.md", *fallbacks]:
            p = d / name
            text = read_text(p) if p.is_file() else None
            if not (text and text.strip()):
                continue
            entry = file_entry(p, "project")
            raw = text.encode("utf-8")
            kept = max(0, min(len(raw), max_bytes - used))
            used += kept
            if kept < len(raw):
                entry["truncated_bytes"] = len(raw) - kept
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
        if p.is_file() and str(p.resolve()) not in seen:
            seen.add(str(p.resolve()))
            e = file_entry(p, role)
            if e:
                out.append(e)

    for n in names:
        add(gd / n, "global")
    for d in top_down(cwd):
        for n in names:
            add(d / n, "project")
    return {"files": out, "warnings": [],
            "notes": [f"context file names: {', '.join(names)}; subdirectory files load just-in-time, not counted"]}


def opencode_files(cwd) -> dict:
    oc = home() / ".config" / "opencode"
    no_claude = bool(os.environ.get("OPENCODE_DISABLE_CLAUDE_CODE"))
    out: list[dict | None] = []
    if (oc / "AGENTS.md").is_file():
        out.append(file_entry(oc / "AGENTS.md", "global"))
    elif not no_claude and (claude_dir() / "CLAUDE.md").is_file():
        out.append(file_entry(claude_dir() / "CLAUDE.md", "global (CLAUDE.md fallback)"))
    root = project_root(cwd)
    here = Path(cwd).resolve()
    upward = [d for d in [here, *here.parents] if d == root or root in d.parents]
    for name in ["AGENTS.md"] + ([] if no_claude else ["CLAUDE.md"]):
        hit = next((d / name for d in upward if (d / name).is_file()), None)
        if hit:
            out.append(file_entry(hit, "project" if name == "AGENTS.md" else "project (CLAUDE.md fallback)"))
            break
    for cfg_path, base in ((oc / "opencode.json", oc), (root / "opencode.json", root)):
        try:
            cfg = json.loads(read_text(cfg_path) or "{}")
        except ValueError:
            continue
        for pattern in cfg.get("instructions") or []:
            if "://" in str(pattern):
                continue
            for m in sorted(glob.glob(str(base / os.path.expanduser(str(pattern))), recursive=True)):
                out.append(file_entry(m, "instructions"))
    return {"files": [e for e in out if e], "warnings": [], "notes": []}


FILE_SCANNERS = {"claude": claude_files, "codex": codex_files, "gemini": gemini_files, "opencode": opencode_files}


def scan_files(cwd, harnesses) -> dict:
    root = project_root(cwd)
    result = {}
    for h in harnesses:
        r = FILE_SCANNERS[h](cwd)
        r["total_tokens"] = sum(e["tokens"] for e in r["files"])
        r["repo_tokens"] = sum(e["tokens"] for e in r["files"]
                               if Path(e["path"]).resolve() == root or root in Path(e["path"]).resolve().parents)
        result[h] = r
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


def _claude_plugin_skill_dirs() -> list[tuple[Path, str]]:
    cd = claude_dir()
    try:
        enabled = [k for k, v in (json.loads(read_text(cd / "settings.json") or "{}").get("enabledPlugins") or {}).items() if v]
    except (ValueError, AttributeError):
        enabled = []
    try:
        installed = json.loads(read_text(cd / "plugins" / "installed_plugins.json") or "{}").get("plugins") or {}
    except (ValueError, AttributeError):
        installed = {}
    dirs = []
    for key in sorted(enabled):
        plugin, _, market = key.partition("@")
        entries = installed.get(key) or []
        candidates = [Path(e["installPath"]) for e in entries if isinstance(e, dict) and e.get("installPath")]
        candidates += [cd / "plugins" / "cache" / market / plugin / e["version"]
                       for e in entries if isinstance(e, dict) and e.get("version")]
        hit = next((c for c in candidates if (c / "skills").is_dir()), None)
        if hit:
            dirs.append((hit / "skills", plugin + ":"))
    return dirs


def scan_skills(cwd, harnesses, window: int) -> dict:
    result = {}
    all_dirs = _skill_dirs(cwd)
    for h in harnesses:
        found: dict[str, dict] = {}
        for directory, prefix in all_dirs[h]:
            if not directory.is_dir():
                continue
            for sm in sorted(directory.glob("*/SKILL.md")):
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
                    desc = desc[:CLAUDE_DESC_CAP]
                found[name] = {"path": str(sm), "desc_chars": len(desc),
                               "tokens": est_tokens(f"- {name}: {desc}\n")}
        tokens = sum(s["tokens"] for s in found.values())
        chars = sum(s["desc_chars"] for s in found.values())
        if h == "claude":
            budget, unit = window // 100, "tokens (1% of window)"
            over = tokens > budget
        elif h == "codex":
            budget, unit = window // 50, "tokens (2% of window)"
            over = tokens > budget
        else:
            budget, unit, over = None, "no published budget", False
        heaviest = sorted(found.items(), key=lambda kv: -kv[1]["tokens"])[:5]
        result[h] = {"count": len(found), "tokens": tokens, "desc_chars": chars, "budget": budget,
                     "budget_unit": unit, "over_budget": over,
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
    for f in sorted(base.glob(pattern)):
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
                if cwd is None and d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("type") != "assistant" or (d.get("isSidechain") and not include_subagents):
                    continue
                msg = d.get("message") or {}
                mid = msg.get("id") or d.get("requestId") or d.get("uuid")
                if mid in local:  # one API response is logged once per content block
                    continue
                local.add(mid)
                u = msg.get("usage") or {}
                ctx = sum(int(u.get(k) or 0) for k in
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


def codex_sessions(include_subagents=False):
    files = []
    for sub in ("sessions", "archived_sessions"):
        files += sorted((codex_dir() / sub).rglob("rollout-*.jsonl"))
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
                p = d.get("payload") or {}
                if d.get("type") == "session_meta":
                    cwd = cwd or p.get("cwd")
                    is_sub = is_sub or p.get("thread_source") == "subagent"
                    continue
                if p.get("type") != "token_count" or not p.get("info"):
                    continue
                info = p["info"]
                total = (info.get("total_token_usage") or {}).get("total_tokens")
                if total is not None and total == last_total:  # repeated rate-limit update
                    continue
                last_total = total
                ctx = int((info.get("last_token_usage") or {}).get("input_tokens") or 0)
                window = info.get("model_context_window") or window
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
    while not p.exists() and p.parent != p:
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
        print(f"{h:9} ~{fmt_k(r['total_tokens'])} tokens at startup ({len(r['files'])} files, "
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
    for h, r in result.items():
        budget = f"budget {fmt_k(r['budget'])} {r['budget_unit']}" if r["budget"] else r["budget_unit"]
        flag = "  OVER: descriptions get dropped, names stay" if r["over_budget"] else ""
        print(f"{h:9} {r['count']:4} skills  ~{fmt_k(r['tokens'])} tokens listing  ({budget}){flag}")
        if r["heaviest"]:
            print("          heaviest: " + ", ".join(f"{x['name']} {fmt_k(x['tokens'])}" for x in r["heaviest"]))


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
    for h, r in skills.items():
        if r["over_budget"]:
            out.append(f"[{h}] {r['count']} skills need ~{fmt_k(r['tokens'])} tokens, budget {fmt_k(r['budget'])}: "
                       "the least-used lose their descriptions and stop triggering reliably. "
                       "Uninstall unused skills or plugins")
    return out


# --------------------------------------------------------------------------- verify-split

def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _trivial(line: str) -> bool:
    return len(line) < 4 or set(line) <= set("-|:*=#`~ >")


def verify_split(original: str, new_paths: list[str], allow: str | None) -> tuple[list[str], int]:
    text = read_text(original)
    if text is None:
        raise SystemExit(f"cannot read {original}")
    want = list(dict.fromkeys(n for n in (_norm(x) for x in text.splitlines()) if n and not _trivial(n)))
    have: set[str] = set()
    for p in new_paths:
        path = Path(p)
        targets = sorted(path.rglob("*.md")) if path.is_dir() else [path]
        for t in targets:
            have.update(_norm(x) for x in (read_text(t) or "").splitlines())
    allowed = {_norm(x) for x in (read_text(allow) or "").splitlines()} if allow else set()
    missing = [x for x in want if x not in have and x not in allowed]
    return missing, len(want)


# --------------------------------------------------------------------------- CLI

def _harnesses(arg: str) -> list[str]:
    if arg == "all":
        return list(HARNESSES)
    hs = [h.strip() for h in arg.split(",")]
    bad = [h for h in hs if h not in HARNESSES]
    if bad:
        raise SystemExit(f"unknown harness: {', '.join(bad)} (use {', '.join(HARNESSES)} or all)")
    return hs


def _emit(obj, as_json: bool, printer) -> None:
    if as_json:
        json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
        print()
    else:
        printer(obj)


def main(argv=None) -> int:
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
    p.add_argument("--max-tokens", type=int, help="exit 1 if this repo's instructions exceed N tokens for any harness")

    p = sub.add_parser("skills", help="skill listing size against budgets")
    common(p)
    p.add_argument("--window", type=int, default=200_000, help="model context window in tokens (default 200000)")

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
    p.add_argument("--window", type=int, default=200_000)
    p.add_argument("--target", type=int, default=5_000, help="target tokens for repo instructions (default 5000)")

    p = sub.add_parser("verify-split", help="every line of OLD must exist in NEW files (or the allow list)")
    p.add_argument("old")
    p.add_argument("new", nargs="+", help="files or directories (*.md searched recursively)")
    p.add_argument("--allow", help="file listing lines removed on purpose")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "files":
        res = scan_files(a.path, _harnesses(a.harness))
        _emit(res, a.json, print_files)
        if a.max_tokens is not None:
            over = {h: r["repo_tokens"] for h, r in res.items() if h in HARNESSES and r["repo_tokens"] > a.max_tokens}
            for h, n in over.items():
                print(f"FAIL {h}: repo instructions ~{fmt_k(n)} tokens > {fmt_k(a.max_tokens)}", file=sys.stderr)
            return 1 if over else 0
        return 0

    if a.cmd == "skills":
        _emit(scan_skills(a.path, _harnesses(a.harness), a.window), a.json, print_skills)
        return 0

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
        if a.json:
            _emit({"project": root, "since": since, "files": files, "skills": skills,
                   "sessions": stats, "findings": found}, True, None)
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
        return 0

    if a.cmd == "verify-split":
        missing, total = verify_split(a.old, a.new, a.allow)
        if a.json:
            _emit({"checked": total, "missing": missing}, True, None)
        else:
            print(f"checked {total} lines of {a.old}: {len(missing)} missing")
            for m in missing[:40]:
                print(f"  - {m[:160]}")
            if len(missing) > 40:
                print(f"  … {len(missing) - 40} more")
        return 1 if missing else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
