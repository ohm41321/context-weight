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
- Keep this file small: `python3 skills/context-weight/scripts/cw.py files . --max-tokens 6000`.
  SKILL.md is held small by `ManifestTest.test_skill_stays_small` (description at most 1,024
  characters, the whole file at most 3,000 estimated tokens).
- Compare folders with `_same_dir`, never as strings: macOS and Windows disks ignore letter case,
  `Path.resolve` keeps the typed case, and git and the session logs record the true one. `report`
  spells its path as the disk does (`_true_case`) so repo paths match git's.

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
  A comparison must decide what an empty side means instead of skipping it. It also once
  counted a parent folder's CLAUDE.md and flagged every repo below it; both sides now count only
  files inside the repo.
- The picture's top row once took each tile's agent from a different maximum, so Claude's load
  sat beside Codex's measured startup. Numbers that read as one row must come from one agent.
- `--window` is Claude Code's window, which its logs lack. It once overrode the window Codex
  records, which hid a Codex listing over its real budget.
- `git log --since` with a date before 1970 returns no commits, and tests use `--days 40000`
  (1917). Leave `--since` out for windows that long. A window past year 1 overflows `date`:
  `_since` clamps it to 0001-01-01.
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
- `verify-split` finds code with `_blocks`, a port of cmark-gfm's block parser cut down to what
  decides code: block quotes and list items as containers, fences, indented code, paragraphs
  (with lazy lines), headings, rules, tables and HTML blocks. Each shortcut tried before it
  passed lost lines: a fence at any indentation, a list item ended by a lazy line, indented code
  read as prose, a `-` under text read as an empty item, a table read as a paragraph (a line four
  spaces in under a row is code), `str.splitlines` splitting at a form feed or U+2028, an HTML
  comment read as a paragraph (indented code under `<!-- generated -->` became its lazy text,
  and a fence inside a comment flipped every fence after it). Check a change against cmark-gfm
  (`pip install cmarkgfm` in a venv): random documents of quotes, markers, fences, rules, table
  rows, HTML lines and blank lines, comparing the code lines `_blocks` yields with the
  `<pre><code>` blocks of `cmarkgfm.github_flavored_markdown_to_html` (safe mode replaces raw
  HTML, so none of it looks like code). 0.3.2 matched it on 1,430,000 documents, 400,000 of them
  with tabs (compare the code text exactly: a check that normalised whitespace missed tabs turned
  into spaces, which expanding tabs first had let pass). cmark-gfm keeps tabs in code, writes the
  columns of a tab that a container or the code's indentation splits as spaces, and strips one
  column per character of a fence's indentation. So under a fence indented with a tab that a
  list item's text starts inside, the code moves in by the columns of that tab left past the
  item's text, minus one: one under `- `, none under `1. `, two under `100. ` at two tabs. Write tag names and list numbers as ASCII
  (`re.I | re.A`, `[0-9]`): plain `re.I` takes `ſ`, `ı` and the Kelvin sign for s, i and k, and
  `\d` takes any digit, while cmark-gfm reads `<ſcript>` and `１.` as text. cmark-gfm follows
  CommonMark 0.29 for HTML: `source` is a block tag and `search` is not, `<!a` starts nothing (only
  `<!A`), and its type 7 has no tag exceptions (`<pre/>` alone is one). Not modelled: footnotes
  (that function leaves them off, GitHub turns them on) and a paragraph made only of link
  reference definitions, under which cmark-gfm reads `---` or `===` as text, not as a heading.
  micromark and commonmark.js are no oracle: they disagree with cmark-gfm in corners (micromark
  starts no list numbered other than 1 right after indented code, and commonmark.js ends an
  empty item at a line of spaces that cmark-gfm lets it keep).
- A closed pipe (`cw.py files . | head -1`) raises `BrokenPipeError` at the last flush, which
  Python turns into exit 120. `main` returns the code decided before printing (`_RESULT`) and
  flushes stdout and stderr itself, pointing each that failed at devnull (with `2>&1` stderr is
  that pipe too). During the run they are `_Output` wrappers, which record each failed write
  and raise `_WriteFailed`, not an OSError, so no `except OSError` for a file read takes it. A
  failure other than a reader that left (EPIPE, or EINVAL on Windows) exits 2: a full disk must
  not pass a gate. With `2>&1` into the same full file the error message fails too; `main`
  settles stderr again after it, or the exit flush turns 2 into 120. `_Parser` swallows a failed
  write as Python 3.13 does, since older argparse let it escape before its exit code. Started
  with fd 1 closed (`>&-`, pythonw), `sys.stdout` is None: `main` swaps in devnull first. Probe
  a write failure with `ulimit -f 0`: Python ignores SIGXFSZ, so the write fails with EFBIG. Run
  it with `-B`: Apple's Python 3.9 writes bytecode under `~/Library/Caches` before it ignores the
  signal, and dies of it in a fresh home.
- Without `tomllib` (Python 3.8 to 3.10) Codex's config.toml is read by regex: only the part
  before the first `[table]`, integers with `_` and `0x`/`0o`/`0b`. `65_536` once read as 65.
- `json.loads` reads `1e999` as infinity and `int(inf)` raises `OverflowError`; `_int` catches it.
- Tests run cw.py in a fake home. setUp drops every `GIT_*` variable (a hook's `GIT_DIR` points
  git at another repo) and `XDG_CONFIG_HOME`; `mine()` drops the CLAUDE.md files a machine keeps
  above the temp folder. A log time's local day depends on `TZ`: build expected dates with
  `astimezone()`.
