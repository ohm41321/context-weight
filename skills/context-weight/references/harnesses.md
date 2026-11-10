# What each harness loads at startup

Checked against official docs on 2026-11-10. Harnesses change. If a number here
disagrees with the harness's own docs, trust the docs and update this file and
`scripts/cw.py`.

## Claude Code

Source: https://code.claude.com/docs/en/memory and https://code.claude.com/docs/en/skills

**Loaded at launch**

- `~/.claude/CLAUDE.md`.
- `CLAUDE.md`, `.claude/CLAUDE.md` and `CLAUDE.local.md` in the working
  directory and in every directory above it.
- `.claude/rules/*.md` files that have no `paths:` frontmatter.

**Loaded on demand**

- A subdirectory's `CLAUDE.md` loads the first time Claude reads, writes or
  edits a file in that subdirectory.
- A rule with `paths:` frontmatter loads when Claude touches a file that
  matches.

**Auto memory:** the start of
`~/.claude/projects/<project path, non-alphanumerics replaced by ->/memory/MEMORY.md`
(its first 200 lines or 25 KB) also loads at launch.

**`@path` imports** "are expanded and loaded into context alongside the
CLAUDE.md that references them". They can recurse up to four hops. They give
structure; they save no context.

- Claude Code 2.1.x matches them with `(?:^|\s)@((?:[^\s\\]|\\ )+)`. `\ ` stands
  for a space in a path.
- Imports are not read inside code spans or code blocks.
- Non-text files are skipped.
- Not verified: whether imports inside HTML comments count. `cw.py` counts them.

**Size**

- The docs target under 200 lines per CLAUDE.md.
- Startup warns "Large CLAUDE.md will impact performance" above 40,000
  characters.

**Skills**

- Only the name and description are listed at startup. The body loads when the
  skill is used.
- `description` plus `when_to_use` is cut at 1,536 characters.
- The whole listing gets about 1% of the context window. Past that, the
  least-used skills lose their descriptions; their names stay.
- Skill folders: `~/.claude/skills`, `.claude/skills` from the working
  directory up to the repo root, and `skills/` inside each plugin.

**Logs:** `~/.claude/projects/<slug>/<session>.jsonl` (or under
`$CLAUDE_CONFIG_DIR`).

- One API response is written once per content block. Deduplicate by
  `message.id`.
- A file that opens with `compact_boundary` continues an earlier file and
  repeats some of its requests, with their original timestamps. Deduplicate
  across files, and leave that file's first request out of the startup baseline.
- Context for one request = `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.

**Live meter:** `/context`.

## Codex

Source: https://developers.openai.com/codex/guides/agents-md and https://developers.openai.com/codex/skills

**Global:** `~/.codex/AGENTS.override.md` if it exists, otherwise
`~/.codex/AGENTS.md`. Only the first non-empty one is used. Set `CODEX_HOME`
to move the folder.

**Project**

- Codex walks from the project root (usually the git root) down to the working
  directory.
- In each directory it takes at most one file: `AGENTS.override.md`, then
  `AGENTS.md`, then any names listed in `project_doc_fallback_filenames`.
- The files are joined root first.

**Size:** `project_doc_max_bytes` defaults to 32 KiB. Once that is reached,
Codex adds nothing more. Raise it in `config.toml`, or split the instructions
across nested directories. `cw.py` applies the cap to the project chain.

**`@` imports:** not documented, so they are treated as plain text.

**Skills**

- Name and description load at startup. The full SKILL.md loads when the skill
  is used.
- Folders: `.agents/skills` from the working directory up to the repo root,
  `~/.agents/skills`, `/etc/codex/skills`, and bundled system skills. Older
  installs also used `~/.codex/skills`.
- The listing budget is 2% of the context window, or 8,000 characters when the
  window is unknown.

**Logs:** `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.

- `event_msg` / `token_count` carries `info.last_token_usage.input_tokens`,
  which is the context for that request and includes cached tokens.
- The same totals are re-emitted on rate-limit updates. Deduplicate them by
  `total_token_usage.total_tokens`.
- Subagent threads have `session_meta.payload.thread_source == "subagent"`.

**Live meter:** `/status`.

## Gemini CLI

Source: https://geminicli.com/docs/cli/gemini-md/ and https://geminicli.com/docs/cli/skills/

The docs say Gemini CLI was replaced by Antigravity CLI on 2026-06-18.
Antigravity's loading rules are not verified here.

- **Global:** `~/.gemini/GEMINI.md`.
- **Project:** `GEMINI.md` in the workspace directories and their parents.
  Subdirectory files load just in time, when a tool touches that directory.
- **File names:** `context.fileName` in `~/.gemini/settings.json` can list
  other names, for example `["AGENTS.md", "GEMINI.md"]`.
- **Skills:** `~/.gemini/skills`, `~/.agents/skills`, `.gemini/skills`,
  `.agents/skills`. Only metadata loads at startup.
- **Logs:** no token-usage log format is supported yet.

## OpenCode

Source: https://opencode.ai/docs/rules/ and https://opencode.ai/docs/skills/

- **Project:** `AGENTS.md`. If there is none, it falls back to `CLAUDE.md`.
- **Global:** `~/.config/opencode/AGENTS.md`. If there is none, it falls back
  to `~/.claude/CLAUDE.md`.
- The first match wins in each category. `OPENCODE_DISABLE_CLAUDE_CODE=1`
  turns off the Claude fallbacks.
- **Extra files:** the `instructions` array in `opencode.json`.
- **Skills:** `~/.config/opencode/skills`, `~/.claude/skills`,
  `~/.agents/skills`, `.opencode/skills`, `.claude/skills`, `.agents/skills`.
  Only the ID, name and description load at startup.
- **Logs:** no token-usage log format is supported yet.

## Consequences for one repo shared by several harnesses

- Put the shared rules in AGENTS.md. Codex, OpenCode and Cursor read it, and
  Gemini does when `context.fileName` includes it.
- CLAUDE.md = `@AGENTS.md` plus Claude-only lines. Every harness then follows
  the same rules, and Claude pays for them only once.
- Keep the whole AGENTS.md chain under 32 KiB, or Codex silently loses the tail.
- A large CLAUDE.md with a small AGENTS.md means Claude and Codex follow
  different rules in the same repo. `cw.py files` flags this.
