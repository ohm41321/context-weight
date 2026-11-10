# Working on context-weight

- Test: `python3 -m unittest discover -s tests -v`. Every bug fix starts with a test that fails.
- `cw.py` stays standard library only and runs on Python 3.8. Do not use 3.9+ APIs such as
  `str.removeprefix`, `Path.is_relative_to`, `zip(strict=)` or `match`.
- `cw.py` only reads. Session commands print numbers and paths, never transcript text.
- Exit codes: 0 ok, 1 a check failed, 2 bad input. A bad path must never exit 0 or 1.
- Loading rules live in `skills/context-weight/references/harnesses.md` and in `cw.py`.
  Change both together, with a source and the date you checked it.
- Use `_is_file` / `_is_dir` / `_exists`, not the `Path` methods: on Python 3.13 and older
  those raise `PermissionError` under an unreadable parent folder.
- Keep this file and SKILL.md small: `python3 skills/context-weight/scripts/cw.py files . --max-tokens 6000`.

## Lessons

- Codex guardian reviewer threads look like user sessions unless `session_meta.payload.source`
  is checked for a `subagent` key. Counting them nearly doubled the session count on a real machine.
- Claude Code budgets the skill listing in characters (1% of the window), not tokens. The window
  is not in Claude Code's logs, so it is assumed and printed as assumed.
- `verify-split` once passed while content was lost: it compared sets, skipped lines under 4
  characters and flattened code indentation. It now compares counts and keeps code shape.
