UV ?= uv
PYTEST ?= $(UV) run pytest

.PHONY: bootstrap format-check lint typecheck type-coverage test-unit test-integration test-e2e test-full test build release

bootstrap:
	$(UV) python install 3.12
	$(UV) sync --all-packages --all-groups
	$(UV) run pre-commit install --install-hooks --hook-type commit-msg --hook-type pre-push

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
