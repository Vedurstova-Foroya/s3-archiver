#!/usr/bin/env bash
set -euo pipefail

uv run pytest -q tests/e2e/test_compose_visual_demo_cleanup.py -m e2e -s --tb=short
