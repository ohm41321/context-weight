#!/usr/bin/env bash
# Thin wrapper so `./install.sh` keeps working. All logic lives in install.py.
#   ./install.sh                  link into ~/.agents/skills and ~/.claude/skills
#   ./install.sh --project DIR    copy into one repo
#   ./install.sh --uninstall      remove what was installed
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$(command -v python3 || command -v python || true)"
if [ -z "$py" ]; then
  echo "install.sh: python3 not found (context-weight needs Python 3.8+)" >&2
  exit 2
fi
exec "$py" "$here/install.py" "$@"
