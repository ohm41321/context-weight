# Changelog

## 0.4.0 (2026-11-13)

Loading rules and skill listings as Claude Code 2.1.322 and Codex 0.166.0 build them, and a
report that leads with what to do. Numbers change where 0.3.2 was wrong, and the report's
layout changes; exit codes do not.

**The report**

- `report` opens with a verdict: the one thing to fix first, or that nothing needs fixing, and
  the startup load behind it (`Verdict: ...` on the third line of the text, `verdict` in
  `--json` with `level`, `headline` and `detail`).
- The picture reads top to bottom: the verdict, the startup load, where it comes from, at most
  three things to do, before and after, then the details (heaviest files, skill listing,
  sessions). The startup bars add the skill listing beside this repo's files and those outside
  it, each part labelled under its bar, not only in a tooltip.
- Findings are ranked, the most urgent first, in the text, `--json` and the picture alike.
- A skill listing over budget says by how much, which skills lose their descriptions, and the
  setting that fixes it: for Claude Code `skillOverrides`, `disable-model-invocation: true`,
  plugins, shorter descriptions or `skillListingBudgetFraction`; for Codex a
  `[[skills.config]]` entry, `allow_implicit_invocation: false` in `agents/openai.yaml`,
  plugins, shorter descriptions or `[skills] max_context_tokens`. `skills` prints it as a
  `fix:` line.
- The skill tells the agent to run `report --open` every time and to give the user the verdict
  first.

**Claude Code**

- AGENTS.md loads as Claude Code's built-in agents-md plugin (2.1.277+) decides: by default only
  where no CLAUDE.md holds text, here or above. The `instructionFiles` option (`claude-md`,
  `claude-md-and-agents-md`, `managed-only`, and the older `projectInstructions`) and the plugin
  being turned off in settings are read, and the report says when they change what loads. An
  AGENTS.md that is already loaded, or holds the same text as a loaded file, counts once.
- `.claude/rules/` is read in every folder from the working directory up, not only the repo's.
- A file that holds only whitespace loads nothing and is no longer counted.
- In a git worktree inside its main checkout (`.claude/worktrees/<name>`), the main checkout's
  own files are no longer counted twice. A worktree's sessions count as its repository's.
- The skill listing is built as Claude Code builds it: managed, user and project skills, commands,
  the enabled plugins in settings order and skills synced from the signed-in account; each line
  cleaned, capped and measured in terminal columns; the budget from the model's window and
  characters per token. With no model in the logs, the window is today's default 1M (0.3.2
  assumed 200k). Over budget, the skills that lose descriptions follow Claude Code's usage order.
- Frontmatter YAML is read as Bun 1.4.4 reads it: a repeated key keeps the last value, `|+` keeps
  the right number of trailing breaks, flow collections take anchors and tags, and more (344
  cases checked against `Bun.YAML.parse`, 3 unreachable from frontmatter still differ).

**Codex**

- The skill listing is built as Codex 0.166.0 builds it: its roots in its order (project
  `.codex/skills`, `$CODEX_HOME/skills`, `~/.agents/skills`, bundled, `/etc/codex/skills`,
  enabled plugins, repo `.agents/skills`), searched 6 folders deep; SKILL.md read with
  serde_yaml's rules, a skill with no description skipped; `[[skills.config]]` and
  `agents/openai.yaml` policy honoured; plugin skills named `plugin:skill`; each line with its
  `(file: ...)` path, and root aliases when they are cheaper.
- The budget is 2% of the full window in Codex's own token count (4 bytes a token), or
  `[skills] max_context_tokens`. The logged window is 95% of the full one, so it is divided by
  0.95; with no Codex logs the window is `--window`, or 272k, printed as assumed (0.3.2 used
  8,000 characters). Over budget, the report says how many skills lose part of their
  descriptions and how many are left out, as Codex fits them.
- New `skills --json` fields include `loaded`, `loaded_tokens`, `omitted`, `aliases` and `hidden`.

**Fixes**

- A named pipe where a file is expected (an instruction file, a rule, a command, a SKILL.md,
  config.toml, a session log) hung `files`, `skills`, `report` and `sessions`, since opening it
  waits for a writer. `cw.py` now reads regular files only.
- A number of more than 4,300 digits in SKILL.md frontmatter crashed `skills` and `report` with
  exit 1 on Python 3.11 and later (and patched 3.8 to 3.10). It now reads as a float, as YAML
  does. Without `tomllib`, such a number in config.toml makes the file unreadable, as an integer
  past 64 bits does in TOML.

**`--json` changes for scripts**

- `skills`: `window_source` is `"model"`, `"assumed"`, `"--window"` or
  `"CLAUDE_CODE_MAX_CONTEXT_TOKENS"` for Claude Code (0.3.2 said `"default"` for an assumed
  window), and `"codex logs"`, `"--window"`, `"assumed"` or `"config"` for Codex (0.3.2 said
  `"unknown"`). Codex's `budget_unit` is always `"tokens"`; 0.3.2 fell back to `"chars"`.
  Codex's `count` includes skills left out of the listing for room (`omitted` says how many).
- `report`: new `verdict`; `findings` are ranked, the most urgent first.

**Not modelled**, and said so where it matters: Claude Code's built-in and bundled skills, MCP
skills, `--add-dir` and session plugins; Codex cloud, remote and executor skills, extra roots an
app adds, project `.codex/config.toml` skill settings and `-c` flags; a Codex skill under a
migrated-command root that a native skill of the same plugin shadows.

## 0.3.2 (2026-11-12)

Fixes from a review of 0.3.1. Output and exit codes change only where 0.3.1 was wrong.

- `verify-split` finds code as GitHub does (GitHub Flavored Markdown, checked against
  cmark-gfm), in list items and block quotes too, and keeps its indentation, tabs included:
  0.3.1 expanded tabs first, so a Makefile recipe whose tab became spaces passed. A line that opens
  with inline code in triple backticks is prose, not a fence; a block opened with four
  backticks closes only on four or more; a fence counts only within three spaces of the
  margin, or of where the text of its list item or block quote starts, so a line further in is
  code; a list item can open with a fence (`` 1. ```sh ``); a block left open ends with its
  list item or block quote; an indented code block (four spaces past where the text starts,
  not continuing a paragraph) is compared as code, not as prose; code in a block quote, such
  as a GitHub alert, is compared as code; a table is not a paragraph, so a line four spaces in
  under one of its rows is code and a list numbered 2 can start there; an HTML block holds no
  code, so a fence inside an HTML comment opens nothing, and indented code right under a
  one-line comment (`<!-- generated -->`) is code, not paragraph text; a fence counts at most
  255 of its characters, as cmark-gfm does; and a line ends only at `\n`, `\r\n` or `\r`, not
  at a form feed or a Unicode line separator. In each case 0.3.1 could pass a split that lost
  lines. Code may now move into or out of a list item or block quote without showing as
  missing. Not modelled: footnote definitions, which GitHub reads, and a paragraph made only of
  link reference definitions, under which cmark-gfm reads `---` or `===` as text.
- `verify-split`'s list of missing lines prints a tab as `\t` and any other control or format
  character as an escape (`\x1b`), so a tab that became spaces shows, and a line cannot send
  terminal escapes. `--json` keeps the lines as they are.
- `verify-split` compares each line on its own, so moving a line break shows both lines as
  missing. The README and `references/slimming.md` used to say re-wrapping prose was fine;
  they now say to keep the old line breaks.
- On macOS and Windows, a project typed in other letter case than git or the session logs
  record (`~/code/repo` for `~/Code/repo`) is one project. `report` used to miss its sessions
  and its Before and after, and on a subfolder typed so (`repo/SUB` for `repo/sub`) the
  Before and after as well.
- Without `tomllib` (Python 3.8 to 3.10), `project_doc_max_bytes = 65_536` in Codex's
  config.toml was read as 65 bytes, and a key under a `[profiles.x]` table was taken as the
  global limit. The fallback now reads only top-level keys, and integers with underscores or a
  `0x`, `0o` or `0b` prefix.
- The warning that Claude Code and Codex follow different rules weighs only files in the repo.
  A CLAUDE.md in a parent folder, which Claude Code loads in every repo below it, used to set it
  off in repos that hold no rules at all.
- `--days` longer than the calendar starts the window on 0001-01-01; it used to crash.
- When no finding is large enough but the repo instructions still pass `--target`, `report` and
  the picture say by how much (`No large fixes: Codex is ~1.9k over the ~5.0k target.`) instead
  of "none" and "within target". `--help` says findings start 2,000 tokens above the target.
- Bad input exits 2: an empty project path (an unset variable), an empty `verify-split` path,
  a negative `--target`. `--harness claude,claude` counts each session once, not twice. A
  Gemini `context.fileName` that is a number, `true` or an object falls back to `GEMINI.md`
  instead of crashing, and a `1e999` in a session log counts as 0 instead of crashing.
- A reader that closes the pipe early (`cw.py files . --max-tokens 6000 | head -1`, with or
  without `2>&1`) no longer changes the exit code: a failed gate still exits 1, a passing one
  0, bad input 2, and `--help` and `--version` 0. 0.3.1 exited 0 or 120, depending on when the
  pipe closed. Started with no stdout at all (`>&-`), `--json` exited 1 with a traceback; it
  now keeps its exit code too. Output that cannot be written for any other reason (a full
  disk, a file size limit) exits 2 with `cw.py: error: cannot write output: ...`, where 0.3.1
  exited 120.
- The slimming procedure backs up every instruction file it will edit (CLAUDE.md and AGENTS.md
  both), runs `verify-split` once per backup, adds `--allow dropped.txt` only when that file
  exists (a missing allow list exits 2), and removes the backups and `dropped.txt` before the
  commit. It no longer suggests writing `<skill-dir>`, a path on one machine, into a committed
  CI or pre-commit file.
- The README's pre-commit hook runs on every commit (`always_run: true`): rules, `@imports` and
  notes files count too. Its CI recipe uses `actions/checkout@v7` and suggests pinning `cw.py`
  to a release tag.
- CI runs on `actions/checkout@v7` and `actions/setup-python@v7`, and tests Python 3.14 on
  Linux, macOS and Windows; 3.8, 3.10 and 3.13 stay on Linux.
- The Claude Code plugin manifest names its icon, `.claude-plugin/icon.png`.
- The tests no longer depend on the machine they run on: they drop `GIT_*` and
  `XDG_CONFIG_HOME`, ignore CLAUDE.md files above the temp folder, read output as UTF-8,
  build expected dates in the local time zone and write exact bytes where they count them. New tests cover the Codex config fallback, Codex
  fallback file names and its 32 KiB cut, Claude Code's import depth limit and
  `.claude/CLAUDE.md`, project and plugin skills, exact session numbers, auto memory's 25 KB
  cap, the default 30-day window and the size of SKILL.md.
- Removed unused code (`fnmatch`, `_SKIP_MODES`).

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
