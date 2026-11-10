# Working on context-weight

- Test: `python3 -m unittest discover -s tests -v`. Every bug fix starts with a test that fails.
- `cw.py` stays standard library only and runs on Python 3.8. Do not use 3.9+ APIs such as
  `str.removeprefix`, `Path.is_relative_to`, `zip(strict=)` or `match`.
- `cw.py` never changes user files. Its one write is the picture from `report --svg`.
  Session commands print numbers and paths, never transcript text. `report`'s before and after
  runs git through `_git` only (read-only, offline, git found on `PATH`) and writes old instruction
  files only under a `TemporaryDirectory`, through `_rel_ok`.
- The picture is drawn from the same dict as `report --json`, so the two never disagree.
  After changing it, run `python3 assets/make_report_demo.py` and look at both SVGs.
- Exit codes: 0 ok, 1 a check failed, 2 bad input. A bad path must never exit 0 or 1.
- Loading rules live in `skills/context-weight/references/harnesses.md` and in `cw.py`.
  Change both together, with a source and the date you checked it.
- Use `_is_file` / `_is_dir` / `_exists`, not the `Path` methods: on Python 3.13 and older
  those raise `PermissionError` under an unreadable parent folder.
- A release bumps `VERSION` in `cw.py` and `version` in both `plugin.json` files; a test checks
  they match. Claude Code and Codex cache plugins by version, so an unbumped change never
  reaches people who installed the plugin.
- `.claude-plugin/icon.png` is `assets/icon.svg` drawn at 1024 px. Anthropic's plugin directory
  reads the icon only the first time the plugin is saved or submitted; later changes are ignored.
- Keep this file and SKILL.md small: `python3 skills/context-weight/scripts/cw.py files . --max-tokens 6000`.

## Lessons

- Codex guardian reviewer threads look like user sessions unless `session_meta.payload.source`
  is checked for a `subagent` key. Counting them nearly doubled the session count on a real machine.
- Claude Code budgets the skill listing in characters (1% of the window), not tokens. The window
  is not in Claude Code's logs, so it is assumed and printed as assumed.
- `verify-split` once passed while content was lost: it compared sets, skipped lines under 4
  characters and flattened code indentation. It now compares counts and keeps code shape.
- A fixed picture name in the shared temp folder (`/tmp/context-weight-<repo>.svg`) let another
  user plant a symlink and have `--svg` overwrite their target. The default now goes into a fresh
  `tempfile.mkdtemp` folder. SVG text also drops control characters and lone surrogates, which
  XML cannot hold.
- The Claude/Codex divergence check once ran only when both harnesses had project files
  (`if c and x`), so a large CLAUDE.md with no AGENTS.md, the worst case, went unflagged.
  A comparison must decide what an empty side means instead of skipping it.
- The picture's top row once took each tile's agent from a different maximum, so Claude's load
  sat beside Codex's measured startup. Numbers that read as one row must come from one agent.
- `--window` is Claude Code's window, which its logs lack. It once overrode the window Codex
  records, which hid a Codex listing over its real budget.
- `git log --since` with a date before 1970 returns no commits, and tests use `--days 40000`
  (1917). Leave `--since` out for windows that long.
- A slimming found by the size of the entry files is checked against the real startup load of
  the old commit: moving text into an `@import`ed file shrinks CLAUDE.md but loads the same.
  Check it against the commit itself, not today's files: 0.3.0 compared with today, so a repo
  whose CLAUDE.md grew past its old size after the cut (common with `/retro` lines) lost the
  slimming without a word.
- Claude Code deletes transcripts under `~/.claude/projects/` older than `cleanupPeriodDays`
  (30 days by default), so "no sessions before" for an older change usually means deleted logs.
- To name the branch commit behind a merged slimming, credit each branch cut with the lines it
  removed that the merge's own diff (against the main line) removes too. Weighing loads fails
  three ways, each seen in a review: a commit's own drop names the removal of text the branch
  pasted in; overlap with the merge's drop in totals names a cut the branch later put back
  (drop B, drop A, restore B); and main-line growth during the branch shifts the totals.
  Test fixtures need real lines: a file of one long line makes every edit remove "the" line.
- A repo's own config can make read-only git run programs: `core.fsmonitor` (ls-files),
  `log.showSignature` + `gpg.program` (log), and in a partial clone `cat-file` fetches missing
  blobs, running `core.sshCommand`. Git older than 2.44 ignores `GIT_NO_LAZY_FETCH`, and a repo's
  `protocol.ssh.allow=always` beats `-c protocol.allow=never`; only `GIT_ALLOW_PROTOCOL` wins.
  `_git` turns each off; a test per hook fails without it.
- The old-commit copy keeps in-repo links as links. Copying a linked file instead moved where its
  `@imports` resolve and listed a self-linked rules folder twice. With `core.symlinks=false` a link
  is a text file holding its target, and that text is what the agents read.
- On Windows a bare `"git"` in `subprocess` runs a `git.exe` from the current folder, and a git
  path with `\` or `C:` escapes a folder joined with it. Neither shows up on macOS or Linux.
