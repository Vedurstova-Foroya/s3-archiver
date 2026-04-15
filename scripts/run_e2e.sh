#!/usr/bin/env bash
set -euo pipefail

docker compose --profile test build app
uv run pytest tests/e2e -m e2e
