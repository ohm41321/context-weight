#!/usr/bin/env python3
"""Draw the README's sample report pictures from made-up data.

Builds a throwaway HOME with an oversized CLAUDE.md, a long AGENTS.md, sixty skills and
synthetic Claude Code and Codex session logs, then runs `cw.py report --svg` on it.
Nothing from the real machine goes into the pictures.

    python3 assets/make_report_demo.py        # writes assets/report-light.svg and report-dark.svg
"""
import datetime as dt
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CW = HERE.parent / "skills" / "context-weight" / "scripts" / "cw.py"

PROSE = ("Run the full test suite before every commit. Keep each module under four hundred lines. "
         "Prefer small pure functions, name things after what they do, and log every external call. ")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def filler(tokens: int) -> str:
    """About `tokens` estimated tokens of plain English (cw.py counts ~3 ASCII chars per token)."""
    text = ""
    n = 0
    while len(text) < tokens * 3:
        n += 1
        text += f"## Rule {n}\n\n{PROSE * 3}\n\n"
    return text[: tokens * 3]


def claude_log(home: Path, repo: Path, name: str, start: dt.datetime, ctxs) -> None:
    recs = [{"type": "user", "cwd": str(repo), "timestamp": start.isoformat()}]
    for i, c in enumerate(ctxs):
        ts = (start + dt.timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
        recs.append({"type": "assistant", "cwd": str(repo), "timestamp": ts, "isSidechain": False,
                     "message": {"id": f"{name}-{i}", "usage": {"input_tokens": 3, "cache_read_input_tokens": c - 3,
                                                                "cache_creation_input_tokens": 0}}})
    write(home / ".claude" / "projects" / "-repo" / f"{name}.jsonl", "".join(json.dumps(r) + "\n" for r in recs))


def codex_log(home: Path, repo: Path, name: str, start: dt.datetime, ctxs) -> None:
    ts = start.isoformat().replace("+00:00", "Z")
    recs = [{"type": "session_meta", "timestamp": ts, "payload": {"cwd": str(repo), "thread_source": "user"}}]
    total = 0
    for c in ctxs:
        total += c
        recs.append({"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": total},
            "last_token_usage": {"input_tokens": c, "cached_input_tokens": c // 2},
            "model_context_window": 258_400}}})
    day = start.strftime("%Y/%m/%d")
    write(home / ".codex" / "sessions" / day / f"rollout-{name}.jsonl", "".join(json.dumps(r) + "\n" for r in recs))


def build(home: Path) -> Path:
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    write(home / ".claude" / "CLAUDE.md", filler(2_100))
    write(repo / "CLAUDE.md", filler(146_000) + "\n@docs/runbook.md\n")
    write(repo / "docs" / "runbook.md", filler(6_200))
    write(repo / ".claude" / "rules" / "style.md", filler(2_600))
    write(repo / "AGENTS.md", filler(16_500))
    for i in range(60):
        desc = (f"Helper number {i} for a task the team does now and then. " * 12)[:700]
        write(home / ".claude" / "skills" / f"skill-{i:02}" / "SKILL.md",
              f"---\nname: skill-{i:02}\ndescription: {desc}\n---\nBody.\n")

    for i in range(8):
        write(home / ".agents" / "skills" / f"tool-{i}" / "SKILL.md",
              f"---\nname: tool-{i}\ndescription: Shared helper {i} for Codex and other agents.\n---\nBody.\n")

    rnd = random.Random(7)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    for s in range(9):
        start = now - dt.timedelta(days=2 + s * 3, hours=rnd.randint(0, 6))
        n = rnd.randint(180, 330)
        ctxs = [int(176_000 + (k / n) ** 1.4 * rnd.randint(150_000, 260_000)) for k in range(n)]
        claude_log(home, repo, f"demo-{s}", start, ctxs)
    for s in range(4):
        start = now - dt.timedelta(days=3 + s * 5)
        n = rnd.randint(40, 90)
        codex_log(home, repo, f"demo-{s}", start, [int(24_000 + k * 900) for k in range(n)])
    return repo


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp).resolve()
        repo = build(home)
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DISABLE_CLAUDE_CODE",
                            "SLASH_COMMAND_TOOL_CHAR_BUDGET")}
        env.update(HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
        for theme in ("light", "dark"):
            out = HERE / f"report-{theme}.svg"
            subprocess.run([sys.executable, str(CW), "report", str(repo), "--harness", "claude,codex",
                            "--svg", str(out), "--theme", theme], env=env, check=True, stdout=subprocess.DEVNULL)
            print(f"wrote {out.relative_to(HERE.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
