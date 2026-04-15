#!/usr/bin/env bash
set -euo pipefail

docker compose --profile test build app
uv run pytest \
  --cov \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=100
