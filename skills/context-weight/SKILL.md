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
the one file it writes is the picture asked for with `--svg` or `--open`. For
`report`'s before and after it reads the repo's git history and copies an older
commit's instruction files into a temporary folder that it deletes.

**Live meter?** For "how full is my context right now", use the harness:
Claude Code `/context`, Codex `/status`. This skill is about the repo and the
history.

## 1. Weigh (read-only)

Every time this skill runs, start here and show the user the picture:

```bash
python3 <skill-dir>/scripts/cw.py report <repo> --days 30 --open
```

`--open` draws the report as one picture in a fresh folder in the system temp
folder, opens it in the user's viewer, and prints its path on the last line
(`Picture: ...`). Always give the user that path too: on a remote or headless
machine nothing opens, and `cw.py` says so on stderr. Use `--svg FILE` to choose
the place, never a path inside the repo unless the user asks.

The picture reads top to bottom: the verdict, the startup load, where it comes
from (this repo's files, files outside it, the skill listing), at most three
things to do, then the details. The text output starts with the same verdict
(`Verdict: ...`), then has up to five parts:

- **Before and after**, when git shows a slimming: uncommitted changes that cut
  the instruction files, or the newest commit or merge on the main line that
  did. It shows this repo's files before and right after the change (and now,
  when they grew back), and measured startup in the sessions before and after it;
- the instruction files each harness loads at startup, in estimated tokens;
- the skill listing against each harness's budget, with a `fix:` line when it
  is over;
- measured session numbers for this repo;
- findings, the most urgent first.

Narrower views: `files <repo>`, `skills <repo>`, `sessions --top 20`,
`sessions --project <name> --compare-at YYYY-MM-DD`. Add `--json` to any of
them; `report --json` also has `verdict` and, with `--open` or `--svg`,
`picture`.

When you report to the user, give:

- the verdict, in plain words and the user's language;
- startup tokens per harness and where they come from;
- `start` (median context of a session's first request);
- `start%` (share of all input that is that startup load being re-sent);
- the `>threshold tok` share;
- the top findings, each with the lever it names.

Say which numbers are estimates (`files`, `skills`) and which are measured
(`sessions`). The Claude Code skill budget depends on the model's context
window. `cw.py` takes the model from this repo's logs; with none it assumes
today's default 1M window and says so. For a 200k-context model, rerun with
`--window 200000`. For Codex it uses the window Codex logged, and assumes 272k
when there are no Codex logs.

## 2. Pick the lever the numbers point at

| Signal | Lever |
| --- | --- |
| repo instructions over ~5k tokens, high `start%` | Slim the instruction files (step 3) |
| `>threshold tok` over 50% | Session hygiene (step 4) |
| "harnesses follow different rules" | One canonical AGENTS.md (step 3) |
| Codex "never sees" warning | Slim, or move parts into nested AGENTS.md files |
| skill listing over budget | The finding's `Fix:` names the setting: turn off or hide unused skills, keep hand-run skills out of the listing, shorten the longest descriptions, or raise the budget. Ask first: it is the user's setup |

Start with the largest number. Leave alone anything the numbers do not point at.

## 3. Slim instruction files

The full procedure is in `references/slimming.md`. Read it before you edit.
Short form:

1. Back up every instruction file you will edit (CLAUDE.md and AGENTS.md
   both, when both exist), then record `cw.py files <repo>`.
2. Sort every section into one of five kinds: **rule** (keep it, make it
   terse), **pointer**, **reference**, **log**, or **duplicate**.
3. Move references and logs to `docs/agent-notes/`. Leave a one-line pointer
   with a plain path. Do not use `@`: Claude Code expands `@imports` at
   launch, and Codex ignores them.
4. Keep the core under 200 lines and about 5k tokens, inside Codex's 32 KiB
   chain limit.
5. Make AGENTS.md the one canonical file. CLAUDE.md holds `@AGENTS.md` plus
   any Claude-only lines.
6. Prove it: `verify-split` exits 0 for each backup, `files` shows the drop,
   and the `--max-tokens` gate passes. Then remove the backups before the
   commit (`references/slimming.md` step 6).

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
- **Stop regrowth:** add `cw.py files . --max-tokens 6000` to CI or
  pre-commit. It exits 1 when the repo's instructions grow past the limit or
  when Codex would cut any of them off, and 2 when the path is wrong, so a typo
  never passes. Never write `<skill-dir>` into a committed file: it is a path
  on this machine, and for a plugin install one version's. CI downloads
  `cw.py` pinned to a release tag or commit (the README has the recipe);
  pre-commit runs a copy kept in the repo (`install.py --project .`).
- **After a few days of real use:** run `cw.py report <repo>` again. It finds
  the slimming in git by itself and prints **Before and after**. When you finish
  slimming, tell the user: commit the change, work as usual for a few days, then
  run `/context-weight` again to see the measured result. If the change it
  picked is the wrong one, set the split with `--compare-at`: a commit id is the
  most exact, and `YYYY-MM-DDTHH:MM` works for a time just before the change. A
  fast-forward merge keeps the branch commit's time, so pass the merge time.

Do not claim realised savings while Before and after says "too early" or shows
no sessions after the change: fewer than 3 sessions are too few to trust. When
it says the files grew back, say so: the sessions after the change include that
growth. When it says Claude Code logs start later than the change, older
sessions were deleted (`cleanupPeriodDays`, 30 days by default); suggest a
higher value in `~/.claude/settings.json` only if the user wants longer
measurements, since it keeps more transcripts on disk.

## Exit codes

`0` ok, `1` a check failed (`--max-tokens`, `verify-split`), `2` bad input
(missing path, unreadable file, bad date). Errors go to stderr.

## Reference

- `references/harnesses.md`: what each harness loads and when, size limits,
  skill folders, with sources.
- `references/slimming.md`: the slimming procedure and verification.
