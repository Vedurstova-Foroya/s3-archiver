#!/usr/bin/env bash
set -euo pipefail

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "release must run from inside a git repository" >&2
  exit 1
fi

if [[ -n "$(git status --short)" ]]; then
  echo "release requires a clean working tree" >&2
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "release requires a configured git origin remote" >&2
  exit 1
fi

uv sync --all-packages --all-groups
uv run cz bump --changelog --check-consistency --yes
git push origin HEAD --follow-tags
