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

new_version="$(cat VERSION)"
version_files=(
  pyproject.toml
  packages/s3_archiver_core/pyproject.toml
  packages/s3_archiver_cli/pyproject.toml
  packages/s3_archiver_localstack_support/pyproject.toml
  packages/s3_archiver_visual_demo/pyproject.toml
)
for file in "${version_files[@]}"; do
  if ! grep -qE "^version = \"${new_version}\"" "$file"; then
    echo "release post-bump: ${file} not updated to ${new_version}" >&2
    exit 1
  fi
done

git push origin HEAD --follow-tags
