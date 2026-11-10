# Changelog

## 0.3.1 (2026-11-10)

Fixes from running 0.3.0 on four real repos.

- A slimming is now judged against its own parent commit, not against today's files. Before, a
  CLAUDE.md that grew past its old size after the cut hid the slimming, with no Before and
  after at all.
- `files` in `compare` has three sizes: `before` and `after` the change, and `now`. `after` used
  to be today's size; today's size is now `now`, and `changed` lists what the change itself
  changed. When the cut files grew back by 2,000 tokens or more, the report says so.
- A slimming merged from a branch still splits the sessions at the merge, but `commit` now names
  the branch commit that removed most of the lines the merge itself removed from the files the
  agents asked load; `merge` names the merge. Removing text the branch added itself, a cut the
  branch put back, and what the main line changed meanwhile earn no credit, and when no single
  commit removed at least 6,000 bytes of them the merge is named. `subject` gives the commit's whole
  subject line without control or format characters, so it cannot send terminal escapes; the
  text and the picture shorten it.
- `report --compare-at` also takes a time (`YYYY-MM-DDTHH:MM`, local unless it has an offset)
  or a commit id, SHA-1 or SHA-256. A commit is compared with its parent; `found` is then
  `"commit"` with `picked: true`, and `before_ref` is `null` for a root commit. For a day or a
  time, each agent's `after` is its own first main-line cut since then, found the way the
  report finds one by itself, so another agent's cut, a file growing or a small trim is
  skipped; an agent with no cut is weighed as today. A split where nothing
  shrank says so. A day after 2099, which git cannot read, now splits after the last commit.
- When the before side has fewer than 3 sessions and the oldest Claude Code log on this machine
  is newer than the window, `compare.logs_from` gives its date and `cleanup_days` the
  `cleanupPeriodDays` in effect (30 when unset). Only when the oldest log is about that old
  (`logs_deleted`) does the report say Claude Code deleted older transcripts.

## 0.3.0 (2026-11-10)

- `report` shows **Before and after** once you have slimmed. It finds the change in git by
  itself: uncommitted changes that cut the instruction files, or else the newest commit on the
  main line that cut them by at least 6,000 bytes and a fifth, or that changed a linked
  instruction file or rules folder. Only files that load at startup from the report's folder
  count, so a subfolder's CLAUDE.md does not move the split. The old commit is weighed the way
  the agents would load it: `@imports`, `opencode.json` instructions and in-repo links included,
  with links checked out as text where `core.symlinks=false`. A cut that loads the same is
  skipped, such as text moved into an imported file or into a `CLAUDE.local.md` git ignores. A slimming merged from a branch counts from the merge commit; a fast-forward has none,
  so it counts from the commit's own time. A later `/retro` line does not move the split. It then sets this
  repo's files then and now beside measured startup and median context in the sessions before
  and after the change, in the text, `--json` (`compare`) and the picture.
- Fewer than 3 sessions after the change are marked "too early to trust", and the report says
  to work as usual for a few days and run it again.
- `report --compare-at YYYY-MM-DD` sets the split when the found one is wrong. Without a git
  repository, or with no commit before that day, it exits 2.
- The old commit's instruction files, and the files they import, are copied into a temporary
  folder that is deleted afterwards; a path that would land outside it is skipped. Git is found
  on `PATH`, never in the current folder, and runs read-only and offline whatever the repo's
  config says: no fsmonitor program, no signature check, no fetch in a partial clone (also on
  git older than 2.44, through `GIT_ALLOW_PROTOCOL`).

## 0.2.2 (2026-11-10)

- The picture's top row now describes one agent. The big number, measured startup, startup
  share and possible saving all belong to the agent with the heaviest startup load. Before,
  the tiles could take Codex's measured numbers beside Claude Code's load and saving.
- `--window` sets Claude Code's window only. Codex keeps the window its logs record and uses
  `--window` only when they record none. Before, `--window 1000000` also raised Codex's skill
  budget to 20k tokens, so a Codex listing over its real budget showed as within it.

## 0.2.1 (2026-11-10)

- The check that Claude Code and Codex follow the same rules now also fires when one side has
  no project instructions at all, such as a large CLAUDE.md with no AGENTS.md. Before, it ran
  only when both files existed, so the worst case went unflagged.

## 0.2.0 (2026-11-10)

Fixes from two pre-release reviews. Every review finding has a test that failed before its fix.

- `report --svg` draws the report as one picture: the startup load per agent, the heaviest
  files, the skill listing against its budget, measured sessions and ranked fixes. It follows
  the viewer's light or dark mode (`--theme`), and `--open` opens it. Standard library only.
- Installable in one step: as a Claude Code plugin (`/plugin install context-weight --marketplace
  ohm41321/context-weight`), as a Codex plugin, or with `npx skills add ohm41321/context-weight`.
- `verify-split` no longer passes when content is lost. It counts repeated lines, keeps short
  lines that contain words, keeps indentation inside code blocks, and never counts the old file
  as its own copy. Unreadable inputs exit 2.
- `files --max-tokens` fails on a missing or non-directory path (exit 2), and fails when Codex
  would cut `AGENTS.md` off at `project_doc_max_bytes`, judging the whole file, not the part
  Codex keeps.
- Codex guardian reviewer and spawned threads (`source.subagent`) no longer count as sessions.
- Skill budgets: Claude Code's is in characters and honours `skillListingBudgetFraction`,
  `SLASH_COMMAND_TOOL_CHAR_BUDGET` and `skillListingMaxDescChars`. The Codex window comes from its
  logs. Assumed windows are printed as assumed.
- Gemini CLI: project files are read up to the git root, not the filesystem root.
- Claude Code rules: symlinked folders in `.claude/rules/` are followed, loops are skipped.
- Robustness: UTF-8 BOM before frontmatter, unreadable folders (Python 3.13 and older), log lines
  that are valid JSON but not objects, non-numeric usage fields, bad dates, broken pipes, and
  non-UTF-8 consoles on Windows.
- `verify-split` reads each real file once, so a symlinked `CLAUDE.md -> AGENTS.md` or a file
  passed together with its folder cannot hide a dropped copy.
- An instruction file that exists but cannot be read is reported, and `--max-tokens` exits 2
  instead of passing on a number it could not measure.
- New `install.py`: links or copies into every agent's skill folders, installs into one repo with
  `--project`, works on Windows. It only deletes symlinks and copies it marked as its own, never
  the source checkout, and reports a broken target folder without stopping.
- CI on Linux, macOS and Windows, Python 3.8 to 3.13.

## 0.1.0

First version: `files`, `skills`, `sessions`, `report` and `verify-split` for Claude Code,
Codex, Gemini CLI and OpenCode.
