# s3-archiver

Strictly typed Python `uv` monorepo for an S3 archiver bootstrap, with OCI S3-compatible auth, LocalStack-backed integration and e2e tests, rootless Docker runtime, conventional commits, and local semver release automation.

## Layout

- `packages/s3_archiver_core`: typed config, logging, S3 client creation, and health checks
- `packages/s3_archiver_cli`: Typer CLI and `s3-archiver` entrypoint
- `tests/unit`, `tests/integration`, `tests/e2e`: full test suite

## Quickstart

Copy and paste this on a Docker Compose host after cloning the repo:

```bash
cp .env.example .env
$EDITOR .env
docker compose build app
docker compose run --rm app
```

The container runs rootless and writes retained JSON logs to the `app_logs` named volume mounted at `/var/log/s3-archiver` in the container.

## Local Development

Install `uv` once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Bootstrap the workspace:

```bash
uv python install 3.12
uv sync --all-packages --all-groups
uv run pre-commit install --install-hooks --hook-type commit-msg --hook-type pre-push
```

Run checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run python scripts/check_type_coverage.py
uv run pytest tests/unit -m unit
./scripts/run_integration.sh
./scripts/run_e2e.sh
uv build --package s3-archiver-core
uv build --package s3-archiver-cli
```

## Logging

- Console logs are JSON lines filtered by `LOG_LEVEL`.
- The same records are written to a timed rotating file handler.
- Retention is 30 daily files.
- Inspect the retained logs with Docker:

```bash
docker run --rm -v s3-archiver_app_logs:/logs alpine:3.22 ls -lah /logs
docker run --rm -v s3-archiver_app_logs:/logs alpine:3.22 cat /logs/s3-archiver.log
```

## Tests

- Unit tests cover config validation, logging setup, health checks, CLI behavior, and repo policy guards.
- Integration tests run against LocalStack S3 and verify bucket access plus object round-trips.
- E2E tests build and run the compose stack and assert the rootless container can complete `s3-archiver check` and persist logs.

## Conventional Commits And Releases

Commit messages must follow Conventional Commits. The commit-msg hook enforces this after `pre-commit` is installed.

Create a release locally:

```bash
git status --short
./scripts/release.sh
```

The release flow bumps the semver version from commit history, updates `VERSION`, updates `CHANGELOG.md`, creates the git tag, and pushes `HEAD` plus tags to `origin`.

## amd64 Deployment Builds

Local development is host-native on Apple Silicon. For deployment images targeting Ubuntu 22 on amd64:

```bash
docker buildx build --platform linux/amd64 -t s3-archiver:latest .
```
