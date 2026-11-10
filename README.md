# context-weight

Measure and cut what an AI coding agent loads before it does any work, across
**Claude Code, Codex, Gemini CLI and OpenCode**. It then proves the cut using the
session logs on your own machine.

Everything that loads at startup (CLAUDE.md, AGENTS.md, GEMINI.md, rules,
`@imports`, the skill listing) is re-sent with **every request** in a session.
A 150k-token CLAUDE.md costs 150k tokens per request before the agent reads a
single line of your code.

```text
$ cw.py report ~/repo --days 120 --harness claude,codex
claude   ~157.0k tokens at startup (5 files, ~154.4k from this repo)
  project                       154.4k  ~/repo/CLAUDE.md
  ! ~/repo/CLAUDE.md: 222,696 chars > 40,000 (Claude Code shows 'Large CLAUDE.md will impact performance')
...
harness  project   sess   req  start  median ... start%   input
claude   ~/repo       9  2261 175.7k  262.0k ...    65%  613.4M
...
1. [claude] repo instructions ~154.4k tokens are re-sent on every request. Slimming to ~5.0k would
   have saved ~337.9M of 613.4M input tokens in this window (55%, estimated at today's file size).
```

## What makes it different

| | context-weight | Others found on skills.sh (2026-11-10) |
| --- | --- | --- |
| Harnesses | Claude Code, Codex, Gemini CLI, OpenCode, each with its real loading rules | Mostly one harness |
| Evidence | Measured from local session logs: startup load, growth, before/after a change | Static estimates, or routing to `/context` / `/status` |
| Cross-harness check | Flags a repo where Claude and Codex follow different instructions, and AGENTS.md text that Codex cuts off past 32 KiB | none found |
| Safe slimming | `verify-split` fails if any line of the old file is missing from the new set | none found |
| Regrowth guard | `files --max-tokens N` exits 1, for CI or pre-commit | none found |

Related skills: [`affaan-m/ecc@context-budget`](https://skills.sh/affaan-m/everything-claude-code/context-budget)
(live meter routing plus a static component audit),
[`anthropics/claude-plugins-official@claude-md-improver`](https://skills.sh/anthropics/claude-plugins-official/claude-md-improver),
[`daymade/claude-code-skills@claude-md-progressive-disclosurer`](https://skills.sh/daymade/claude-code-skills/claude-md-progressive-disclosurer),
[`huajiexiewenfeng/codex-token-usage-skill`](https://skills.sh/huajiexiewenfeng/codex-token-usage-skill/codex-token-usage)
(Codex usage dashboard).

## Install

```bash
git clone <this repo> ~/context-weight && ~/context-weight/install.sh
```

`install.sh` symlinks `skills/context-weight` into:

- `~/.agents/skills`, read by Codex, Gemini CLI and OpenCode;
- `~/.claude/skills`, read by Claude Code and OpenCode.

`./install.sh --uninstall` removes the links. For a single repo, copy the folder
to `.agents/skills/` or `.claude/skills/` instead.

## Use

Ask the agent to "weigh this repo's context". You can also run the script
yourself; it needs Python 3.8+ and nothing else:

```bash
CW=skills/context-weight/scripts/cw.py
python3 $CW report .                    # everything for this repo + findings
python3 $CW files .                     # startup instruction files per harness
python3 $CW skills . --window 1000000   # skill listing vs budget
python3 $CW sessions --top 20           # all projects, from local logs
python3 $CW sessions --project myrepo --compare-at 2026-11-10
python3 $CW verify-split CLAUDE.md.bak AGENTS.md CLAUDE.md docs/agent-notes --allow dropped.txt
python3 $CW files . --max-tokens 6000   # CI gate
```

All subcommands take `--json`. The script only reads. It prints numbers and
paths, never transcript content.

Accuracy:

- File and skill token counts are estimates: about 3 ASCII characters per
  token, about 1 token per Thai or CJK character.
- Session numbers are what the provider reported.
- Session logs are read from Claude Code and Codex only. Gemini CLI and
  OpenCode get the file and skill checks.

## Test

```bash
python3 -m unittest discover -s tests -v
```
