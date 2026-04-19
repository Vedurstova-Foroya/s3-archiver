UV ?= uv
PYTEST ?= $(UV) run pytest
ENV_FILE ?= .env

.PHONY: bootstrap run format-check lint typecheck type-coverage test-unit test-integration test-e2e test-full test build release

bootstrap:
	$(UV) python install 3.12
	$(UV) sync --all-packages --all-groups
	$(UV) run pre-commit install --install-hooks --hook-type commit-msg --hook-type pre-push

run:
	@test -f "$(ENV_FILE)" || { echo "missing env file: $(ENV_FILE)" >&2; exit 1; }
	@bash -lc 'set -a; source "$(ENV_FILE)"; set +a; if [ -n "$(S3_ENDPOINT_URL)" ]; then export S3_ENDPOINT_URL="$(S3_ENDPOINT_URL)"; fi; $(UV) run s3-archiver check'

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run basedpyright

type-coverage:
	$(UV) run python scripts/check_type_coverage.py

test-unit:
	$(PYTEST) tests/unit -m unit

test-integration:
	./scripts/run_integration.sh

test-e2e:
	./scripts/run_e2e.sh

test-full:
	./scripts/run_full_suite.sh

test: test-unit test-integration test-e2e

build:
	$(UV) build --package s3-archiver-core
	$(UV) build --package s3-archiver-cli

release:
	./scripts/release.sh
