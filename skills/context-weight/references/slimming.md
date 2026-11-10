# Slimming an instruction file without losing knowledge

Goal: keep only what changes the agent's behaviour on most tasks, so that is
what gets re-sent every request. Everything else moves to files the agent reads
when a task needs them.

## 0. Before

```bash
CW=<skill-dir>/scripts/cw.py
python3 $CW files .                      # record the startup tokens per harness
cp CLAUDE.md CLAUDE.md.bak-$(date +%Y%m%d)
cp AGENTS.md AGENTS.md.bak-$(date +%Y%m%d) 2>/dev/null || true
```

If the repo is in git, also work on a branch. Never edit the backup.

## 1. Sort every section

Read the whole file. Put each section into one of these kinds:

| Kind | Test | Where it goes |
| --- | --- | --- |
| **Rule** | Would the agent do something wrong on an ordinary task without it? This covers commands to run, test and lint, conventions, never-do lists, safety gates. | Stays in the core, rewritten tersely |
| **Pointer** | Needed only for some tasks: deploy, a subsystem, a data layout. | Body moves to `docs/agent-notes/<topic>.md`. The core keeps one line |
| **Reference** | Long facts: API tables, schemas, hosts, file maps. | `docs/agent-notes/<topic>.md` |
| **Log** | Dated history, experiment results, changelogs, incident write-ups, "today we…". | `docs/agent-notes/history/<yyyy-mm>.md` or the existing CHANGELOG. Keep the lasting lesson as one rule line |
| **Duplicate / obsolete** | Says the same as another line, or is contradicted by the current code. | Delete, but only after you check. List it in `dropped.txt` |

Session logs pasted into CLAUDE.md are usually the bulk. They are logs.

## 2. Write pointers the agent will follow

Use a plain path. Never use `@path`: Claude Code loads `@` imports at launch, so
an import saves nothing.

```markdown
## Where notes live (read before working in that area)
- Deploy and rollback: docs/agent-notes/deploy.md
- GPU host roles and ssh: docs/agent-notes/hosts.md
- Past incidents and lessons: docs/agent-notes/history/
```

Make each pointer name the task that should trigger the read.

## 3. One source for every harness

- Put the core in `AGENTS.md`.
- `CLAUDE.md` becomes:

  ```markdown
  @AGENTS.md

  <Claude Code-only lines, if any>
  ```

- If you need the opposite direction, a symlink `CLAUDE.md -> AGENTS.md` also
  works.

## 4. Size targets

- Core: under 200 lines and about 5k tokens.
- The whole AGENTS.md chain (global plus root down to the working directory):
  under 32 KiB, Codex's default limit.
- Rules that apply to one subdirectory go into that subdirectory's
  `AGENTS.md`/`CLAUDE.md`, or into a Claude Code rule with `paths:`
  frontmatter. Both load only when work happens there.

## 5. Verify: nothing lost, size down

```bash
# every line of the old file must exist in the new set (core + notes),
# or be listed on purpose in dropped.txt
python3 $CW verify-split CLAUDE.md.bak-YYYYMMDD AGENTS.md CLAUDE.md docs/agent-notes --allow dropped.txt
python3 $CW files .                      # compare with step 0
python3 $CW files . --max-tokens 6000    # exit 0
```

`verify-split` compares lines after collapsing whitespace. When you rewrite a
rule tersely, the old wording shows up as missing. Either keep the original
line in a notes file, or add it to `dropped.txt` with the user's agreement.
Exit 0 is the gate. Do not hand back work while it fails.

## 6. Keep it slim

- Add the `--max-tokens` check to pre-commit or CI.
- New lessons go in as one rule line plus a note file, not as a pasted log.
- After a few days of use, measure the real effect:
  `python3 $CW sessions --project <name> --compare-at <date of the change>`.
