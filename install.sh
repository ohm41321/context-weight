#!/usr/bin/env bash
# Link context-weight into the personal skill folders that agent harnesses read.
#   ~/.agents/skills   Codex, Gemini CLI, OpenCode
#   ~/.claude/skills   Claude Code (and OpenCode)
# Usage: ./install.sh            install (idempotent)
#        ./install.sh --uninstall
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/context-weight"
TARGETS=("$HOME/.agents/skills" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills")

if [ "${1:-}" = "--uninstall" ]; then
  for t in "${TARGETS[@]}"; do
    link="$t/context-weight"
    if [ -L "$link" ]; then rm "$link"; echo "removed $link"; fi
  done
  exit 0
fi

for t in "${TARGETS[@]}"; do
  mkdir -p "$t"
  link="$t/context-weight"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "skip $link: exists and is not a symlink" >&2
    continue
  fi
  ln -sfn "$SRC" "$link"
  echo "linked $link -> $SRC"
done
echo "Restart the agent (or reload skills) to pick it up."
