---
name: context-weight
description: Measure and cut the context an agent loads before any work starts (CLAUDE.md, AGENTS.md, GEMINI.md, rules, @imports, skill listings) and how large sessions grow, using local Claude Code and Codex logs; then slim instruction files and prove no line was lost. Use when sessions feel heavy, compaction or usage limits arrive early, an instruction file keeps growing, Claude Code and Codex behave differently in one repo, or for "context บวม", "ลด token", "CLAUDE.md ใหญ่". Not the live meter for the current session.
---

# Context Weight

Whatever loads at startup is re-sent with every request in the session. A
150k-token CLAUDE.md costs 150k tokens per request before any work happens. This
skill measures that load, cuts it, and proves the cut with numbers.

The script is `scripts/cw.py` in this skill's directory. Resolve its absolute
path from the location of this file. It needs Python 3.8+ and nothing else. It
never changes the user's files, it prints numbers, never transcript content, and
the one file it writes is the picture asked for with `--svg` or `--open`.

**Live meter?** For "how full is my context right now", use the harness:
Claude Code `/context`, Codex `/status`. This skill is about the repo and the
history.

## 1. Weigh (read-only)

```bash
python3 <skill-dir>/scripts/cw.py report <repo> --days 30 --svg
```

`--svg` also draws the report as one picture in the system temp folder and
prints its path on the last line (`Picture: ...`). Give the user that path so
they can read the numbers at a glance; add `--open` to open it for them when they
ask to see it. Use `--svg FILE` to choose the place, never a path inside the repo
unless the user asks.

The text output has four parts:

- the instruction files each harness loads at startup, in estimated tokens;
- the skill listing against each harness's budget;
- measured session numbers for this repo;
- ranked findings.

Narrower views: `files <repo>`, `skills <repo>`, `sessions --top 20`,
`sessions --project <name> --compare-at YYYY-MM-DD`. Add `--json` to any of
them.

When you report to the user, give:

- startup tokens per harness;
- `start` (median context of a session's first request);
- `start%` (share of all input that is that startup load being re-sent);
- the `>threshold tok` share;
- the findings.

Say which numbers are estimates (`files`, `skills`) and which are measured
(`sessions`). The Claude Code skill budget depends on the model's context
window, which the logs do not record: `cw.py` assumes 200k and says so. If the
user runs a 1M-context model, rerun with `--window 1000000`.

## 2. Pick the lever the numbers point at

| Signal | Lever |
| --- | --- |
| repo instructions over ~5k tokens, high `start%` | Slim the instruction files (step 3) |
| `>threshold tok` over 50% | Session hygiene (step 4) |
| "harnesses follow different rules" | One canonical AGENTS.md (step 3) |
| Codex "never sees" warning | Slim, or move parts into nested AGENTS.md files |
| skill listing over budget | Uninstall unused skills or plugins. Ask first: it is the user's setup |

Start with the largest number. Leave alone anything the numbers do not point at.

## 3. Slim instruction files

The full procedure is in `references/slimming.md`. Read it before you edit.
Short form:

1. Back up the file, then record `cw.py files <repo>`.
2. Sort every section into one of five kinds: **rule** (keep it, make it
   terse), **pointer**, **reference**, **log**, or **duplicate**.
3. Move references and logs to `docs/agent-notes/`. Leave a one-line pointer
   with a plain path. Do not use `@`: Claude Code expands `@imports` at
   launch, and Codex ignores them.
4. Keep the core under 200 lines and about 5k tokens, inside Codex's 32 KiB
   chain limit.
5. Make AGENTS.md the one canonical file. CLAUDE.md holds `@AGENTS.md` plus
   any Claude-only lines.
6. Prove it: `verify-split` exits 0, `files` shows the drop, and the
   `--max-tokens` gate passes.

Moving text is safe. Deleting knowledge is the user's call: ask before you drop
any line that is not a proven duplicate.

## 4. Session hygiene

Most input tokens come from late requests in long sessions. Each request
re-sends everything before it.

- Use one task per session.
- When context passes about half the window, or the task changes, write a short
  handoff (goal, state, next step, files touched) and start fresh. In Claude
  Code that is `/clear`; in Codex it is `/new`. Prefer this over repeated
  compaction.
- Keep large tool output out of context: filter it, head it, or write it to a
  file and read only what you need.

## 5. Prove it

- **Right away:** compare `cw.py files <repo>` before and after, and check that
  `verify-split` exits 0.
- **Stop regrowth:** add `python3 <skill-dir>/scripts/cw.py files . --max-tokens 6000`
  to CI or pre-commit. It exits 1 when the repo's instructions grow past the
  limit or when Codex would cut any of them off, and 2 when the path is wrong,
  so a typo never passes.
- **After a few days of real use:** run
  `cw.py sessions --project <name> --compare-at <date of the change>` and report
  `start` and `start%` before and after.

Do not claim realised savings until that last command has run on sessions from
after the change.

## Exit codes

`0` ok, `1` a check failed (`--max-tokens`, `verify-split`), `2` bad input
(missing path, unreadable file, bad date). Errors go to stderr.

## Reference

- `references/harnesses.md`: what each harness loads and when, size limits,
  skill folders, with sources.
- `references/slimming.md`: the slimming procedure and verification.
