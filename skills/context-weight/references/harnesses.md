# What each harness loads at startup

Checked on 2026-11-13 against official docs and, where the docs say less, the
harnesses' own code: Claude Code 2.1.322 and Codex 0.166.0 (`rust-v0.166.0`).
Harnesses change. If a number here disagrees with the harness's own docs, trust
the docs and update this file and `scripts/cw.py`.

## Claude Code

Source: https://code.claude.com/docs/en/memory and https://code.claude.com/docs/en/skills,
and Claude Code 2.1.322's own code (its built-in agents-md plugin, the skill
listing), read on 2026-11-12.

**Loaded at launch**

- `~/.claude/CLAUDE.md`.
- `CLAUDE.md`, `.claude/CLAUDE.md` and `CLAUDE.local.md` in the working
  directory and in every directory above it, short of the filesystem root.
- `.claude/rules/*.md` files that have no `paths:` frontmatter, user-level
  `~/.claude/rules/` first, then the `.claude/rules/` of each of those same
  directories, from the top down. Symlinked files and folders inside `rules/`
  are followed; loops are detected. A symlink whose target is outside the
  working directory loads only after the user approves external imports.
- A file that holds only whitespace loads nothing.

**AGENTS.md** (Claude Code 2.1.277 and later, through its built-in agents-md
plugin). Its `instructionFiles` option decides:

- `claude-md-or-agents-md`, the default: `AGENTS.md` and `.claude/AGENTS.md`
  in the same directories load only when no CLAUDE.md there or above
  (`CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`; `~/.claude/CLAUDE.md`
  does not count) holds any text.
- `claude-md`: AGENTS.md never loads. `claude-md-and-agents-md`: it loads beside
  CLAUDE.md. `managed-only`: no user or project file loads at all.
- The option is read from user settings only:
  `pluginConfigs["agents-md@builtin"].options.instructionFiles`. Its older
  name, `projectInstructions`, took `none`, `claude`, `agents-fallback` and
  `both`.
- `enabledPlugins` can turn the plugin off in any settings file; the highest
  one that names it decides (project local, project, user). Off means no
  AGENTS.md.
- An AGENTS.md that is a file already loaded (the same file through a link, or
  one a CLAUDE.md imports), or holds the same text as a loaded project file,
  is not loaded twice.

**Git worktrees:** in a worktree inside its main checkout, as Claude Code makes
them (`.claude/worktrees/<name>`), the main checkout's own folders are skipped:
the worktree holds its own copy of their files. Folders above the main checkout
still count. Sessions in a worktree belong to its repository, and auto memory
is one per repository, shared by its worktrees.

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
- Where they come from: managed (policy) skills, `~/.claude/skills`,
  `.claude/skills` from the working directory up to the git root (in a
  worktree, the main checkout's too), commands in `commands/` folders, the
  enabled plugins in the order settings name them (their `skills/`, the skill
  paths in their manifest, their commands), and skills synced from the
  claude.ai account Claude Code is signed in with. A file reached twice is
  listed once.
- Frontmatter is YAML as Bun's parser reads it (Bun 1.4.4, inside Claude Code
  2.1.322). `cw.py` matches it on 341 of 344 cases; the other three
  (integer-like key order, `?` complex keys, `---` between documents) cannot
  occur in frontmatter.
- `description` plus `when_to_use` is cut at 1,536 characters
  (`skillListingMaxDescChars` changes it). Each line is cleaned as Claude Code
  does and measured in terminal columns: a wide character takes two.
- The whole listing has a budget of 1% of the model's context window, in
  columns: the window times the characters per token Claude Code assumes for
  the model (4 for older models, 3 for the rest and for today's default).
  `skillListingBudgetFraction` in settings (for example `0.02`) or the
  `SLASH_COMMAND_TOOL_CHAR_BUDGET` environment variable (a fixed count) changes
  it.
- The window comes from the model: 1M for models with a native 1M window or a
  `[1m]` suffix, 200k for the rest. `CLAUDE_CODE_DISABLE_1M_CONTEXT` caps it at
  200k. The model is not always in the logs; with none, `cw.py` assumes today's
  default model's 1M window and says so (`--window 200000` for a 200k model).
- Past the budget, lines set to `name-only` stay whole, every other line is cut
  to its name, and descriptions come back in order of use while they fit.
  Skills built into Claude Code keep theirs and are not on disk, so the number
  `cw.py` gives for skills losing descriptions is a lower bound.
- Levers: `skillOverrides` in settings (`{"name": "off"}` hides a skill,
  `"name-only"` keeps only its name), `disable-model-invocation: true` in a
  skill only run by hand, disabling unused plugins, shorter descriptions, or a
  larger `skillListingBudgetFraction`.

**Logs:** `~/.claude/projects/<slug>/<session>.jsonl` (or under
`$CLAUDE_CONFIG_DIR`).

- One API response is written once per content block. Deduplicate by
  `message.id`.
- A file that opens with `compact_boundary` continues an earlier file and
  repeats some of its requests, with their original timestamps. Deduplicate
  across files, and leave that file's first request out of the startup baseline.
  Claude Code 2.1.3xx writes compactions mid-file instead; message ids are still
  unique across files, so the cross-file dedup is a guard, not a correction.
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

**Skills** (Codex 0.166.0's code, `codex-rs` core skills and host, read on
2026-11-13; the docs give only the folders)

- Name and description load at startup. The full SKILL.md loads when the skill
  is used.
- Roots, in the order Codex reads them (a skill found twice keeps the first):
  `.codex/skills` from the working directory up to the project root,
  `$CODEX_HOME/skills`, `~/.agents/skills`, the bundled system skills
  (`$CODEX_HOME/skills/.system`, unless `[skills.bundled] enabled = false`),
  `/etc/codex/skills`, the plugins config.toml enables, then `.agents/skills`
  from the project root down to the working directory. The project root is
  the nearest folder with one of `project_root_markers` (default `.git`).
- Each root is searched breadth first, at most 6 folders deep, 2,000 folders
  and 20,000 names. Hidden folders are skipped.
- SKILL.md frontmatter is read with serde_yaml. A skill with no description is
  skipped; a name is cut at 64 characters, a description at 1,024. A plugin's
  skills are named `plugin:skill`.
- Left out of the listing: skills a `[[skills.config]]` entry turns off (by
  `path` or `name`, `enabled = false`, the last entry winning) and skills whose
  `agents/openai.yaml` sets `policy: allow_implicit_invocation: false`.
- Each line reads `- name: description (file: path)`. When it is cheaper, Codex
  names the roots once (`### Skill roots`, `` - `r0` = `root` ``) and writes
  `(file: r0/...)`.
- The budget is 2% of the full context window in tokens, at 4 bytes a token,
  or `[skills] max_context_tokens` (at most 10,000). Rollouts log
  `model_context_window` as 95% of the full window, so `cw.py` divides by 0.95.
  With no Codex logs it takes `--window`, or assumes 272k and says so.
- Past the budget, Codex cuts description characters evenly, one per line per
  round, then drops descriptions, then leaves lines out.
- Checked by replay: on the skill set of a real rollout, `cw.py` drew the same
  156 lines at the same cost (10,000 tokens).
- Not counted: cloud, remote and executor skills, and extra roots an app adds.

**Logs:** `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.

- `event_msg` / `token_count` carries `info.last_token_usage.input_tokens`,
  which is the context for that request and includes cached tokens.
- The same totals are re-emitted on rate-limit updates. Deduplicate them by
  `total_token_usage.total_tokens`.
- Subagent threads: `session_meta.payload.source` is an object with a
  `subagent` key (spawned agents: `thread_spawn`; guardian reviewers:
  `other: "guardian"`). `thread_source` is `"subagent"` or `"guardian_review"`
  for them, and `"user"`, `"chatgpt_handoff"` or absent for user threads.
  Counting guardian threads as sessions roughly doubles the session count.

**Live meter:** `/status`.

## Gemini CLI

Source: https://geminicli.com/docs/cli/gemini-md/ and https://geminicli.com/docs/cli/skills/

The docs say Gemini CLI was replaced by Antigravity CLI on 2026-06-18.
Antigravity's loading rules are not verified here.

- **Global:** `~/.gemini/GEMINI.md`.
- **Project:** `GEMINI.md` in the working directory and each parent up to the
  project root (the folder with `.git`). Subdirectory files load just in time,
  when a tool touches that directory. `cw.py` walks to the filesystem root when
  there is no `.git`.
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
- A large CLAUDE.md with a small or missing AGENTS.md (or the reverse) means
  Claude and Codex follow different rules in the same repo. `cw.py files`
  flags this when it measures both harnesses. It weighs only the repo's own
  files: a CLAUDE.md in a parent folder is listed, not compared.
