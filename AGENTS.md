# Working on context-weight

- Test: `python3 -m unittest discover -s tests -v`. Every bug fix starts with a test that fails.
- `cw.py` stays standard library only and runs on Python 3.8. Do not use 3.9+ APIs such as
  `str.removeprefix`, `Path.is_relative_to`, `zip(strict=)` or `match`.
- `cw.py` never changes user files. Its one write is the picture from `report --svg`.
  Session commands print numbers and paths, never transcript text.
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
