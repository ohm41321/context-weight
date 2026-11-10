# Changelog

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
