# Slimming an instruction file without losing knowledge

Goal: keep only what changes the agent's behaviour on most tasks, so that is
what gets re-sent every request. Everything else moves to files the agent reads
when a task needs them.

## 0. Before

```bash
CW=<skill-dir>/scripts/cw.py
python3 $CW files .                      # record the startup tokens per harness
for f in CLAUDE.md AGENTS.md GEMINI.md; do
  if [ -f "$f" ]; then cp "$f" "$f.bak-$(date +%Y%m%d)"; fi
done
```

Back up every instruction file you will edit, not only the largest: when
CLAUDE.md and AGENTS.md both hold rules and you merge them, each backup is
checked in step 5. If the repo is in git, also work on a branch. Never edit a
backup.

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
# every line of each old file must exist in the new set (core + notes),
# or be listed on purpose in dropped.txt: run it once per backup from step 0
python3 $CW verify-split CLAUDE.md.bak-YYYYMMDD AGENTS.md CLAUDE.md docs/agent-notes
python3 $CW verify-split AGENTS.md.bak-YYYYMMDD AGENTS.md CLAUDE.md docs/agent-notes
python3 $CW files .                      # compare with step 0
python3 $CW files . --max-tokens 6000    # exit 0
```

Add `--allow dropped.txt` to each `verify-split` line only if you made that
file: a missing allow list is an input that cannot be read, so it exits 2.

How `verify-split` compares:

- Every line with a letter or digit counts, however short (`No.` counts).
  Horizontal rules, table separators and code fences are structure and are
  skipped.
- Each line is compared on its own. In prose, indentation and spacing inside
  a line may change. Moving a line break changes both lines, so they show up
  as missing: keep the old line breaks when you move a paragraph.
- In code, indentation relative to its block is kept, tabs included:
  flattening YAML or Python, or turning a Makefile's tab indentation into
  spaces, shows up as missing. Where a list item's or quote's indentation
  takes part of a tab, the rest counts as the spaces GitHub shows. Spacing
  inside a line may change. The list of missing lines prints a tab as `\t`.
  Code is found as GitHub finds it (GitHub Flavored Markdown):
  fenced blocks, in list items and block quotes too, and indented blocks
  (four spaces past where the text starts, not continuing a paragraph). A
  fence counts within three spaces of where the text of its list item or
  quote starts; the block ends on a bare fence of its own character
  (backticks or tildes), at least as long as the one that opened it, or where
  its item or quote ends. A line that starts with inline code in triple
  backticks is prose, and so is HTML: a fence inside an HTML comment opens
  nothing, nor does one right under a `<div>` line (after a blank line it is
  code), and the lines of a `<pre>` block are compared as prose, so keep
  their indentation yourself. Code may move into or out of a list item or a
  quote. Two rare cases are not modelled: footnote definitions (`[^1]: ...`),
  and a `---` or `===` right under a paragraph made only of link
  definitions (`[docs]: https://...`), which GitHub reads as text, not as a
  heading underline.
- A line that appears twice in the old file must appear twice in the new set.
  Removing a duplicate is a drop: list it in `dropped.txt`.
- The old file never counts as its own copy, even inside a folder you pass.

When you rewrite a rule tersely, the old wording shows up as missing. Either
keep the original line in a notes file, or add it to `dropped.txt` with the
user's agreement. Exit 0 is the gate; exit 1 means lines are missing; exit 2
means an input could not be read. Do not hand back work while it fails.

## 6. Before you commit

The backups hold the whole old file, and `dropped.txt` the lines you chose to
drop. Neither belongs in the commit that slims the repo.

- In git, delete them once every `verify-split` exits 0: the history keeps the
  old file.
- Outside git, the backup is the only copy: move it out of the project folder
  instead of deleting it.

## 7. Keep it slim

- Add the `--max-tokens` check to pre-commit or CI. Never put `<skill-dir>`
  in a committed file: it is a path on this machine. In CI, download `cw.py`
  pinned to a release tag or commit, as the README shows. For pre-commit, keep
  a copy in the repo with `install.py --project .` and point the hook at
  `.agents/skills/context-weight/scripts/cw.py`.
- New lessons go in as one rule line plus a note file, not as a pasted log.
- After a few days of use, measure the real effect with `python3 $CW report .`.
  Its Before and after finds the slimming commit in git by itself; pass
  `--compare-at <commit id>` (or a time just before the change) to set the split yourself.
