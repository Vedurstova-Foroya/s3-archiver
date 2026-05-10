# s3-archiver

Strictly typed Python `uv` monorepo for an S3 archiver bootstrap, with OCI S3-compatible auth, LocalStack-backed integration and e2e tests, rootless Docker runtime, conventional commits, and local semver release automation.

## Layout

- `packages/s3_archiver_core`: typed config, logging, S3 client creation, and health checks
- `packages/s3_archiver_cli`: Typer CLI and `s3-archiver` entrypoint
- `tests/unit`, `tests/integration`, `tests/e2e`: full test suite

## Quickstart

Install `uv` once and sync the workspace after cloning the repo:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv sync --all-packages --all-groups
uv run pre-commit install --install-hooks --hook-type commit-msg --hook-type pre-push
```

Run the canonical compose-backed health check flow:

```bash
docker compose build app
suffix="$(uuidgen | tr '[:upper:]' '[:lower:]')"
source_bucket="s3-archiver-source-${suffix}"
destination_bucket="s3-archiver-destination-${suffix}"
mkdir -p .local
sed \
  -e "s/s3-archiver-source-replace-with-uuid/${source_bucket}/" \
  -e "s/s3-archiver-destination-replace-with-uuid/${destination_bucket}/" \
  .env.e2e >".local/e2e-${suffix}.env"
docker compose --profile test up -d localstack
docker compose --profile test exec -T localstack \
  awslocal s3api create-bucket --bucket "${source_bucket}"
docker compose --profile test exec -T localstack \
  awslocal s3api create-bucket --bucket "${destination_bucket}"
APP_ENV_FILE=".local/e2e-${suffix}.env" docker compose --profile test run --rm app s3-archiver check
docker compose --profile test down -v
```

Running the app container without an explicit command prints CLI help and exits `0`.
Use `s3-archiver check` for startup validation, `s3-archiver archive` for one archive invocation, and `s3-archiver schedule` for the built-in once-per-day UTC scheduler loop.

The container runs rootless and writes retained JSON logs to the `app_logs` named volume mounted at `/var/log/s3-archiver` in the container.
The checked-in env files default `LOG_DIR` to `/var/log/s3-archiver` to match the runtime contract used by the container image and Compose stack.

Use `.env.example` for OCI-backed runs and `.env.e2e` as the template for the LocalStack compose flow shown above.
Archive defaults are explicit in those files: `ARCHIVER_RETENTION_DAYS=60`, `ARCHIVER_ENABLE_CLEANUP=false`, `ARCHIVER_MAX_WORKERS=16`, `ARCHIVER_RUN_TIMEOUT=7d`, `ARCHIVER_TEMP_DIR=/tmp/s3-archiver`, and disabled source whitelist/blacklist filters.
Each archive run selects timestamped source objects whose key-derived UTC data day is at or
before the retention cutoff day, then writes one `.tar.gz` per archive root per data day.
Destination archive filenames use the data day from the source key, not the run date.
LocalStack readiness now only proves the S3 API is reachable. The pytest integration and e2e harnesses generate LocalStack-only env files with fresh UUID-suffixed source and destination buckets for each test, then create and tear down those buckets in fixtures.

## Local Development

For host-native OCI smoke checks, create a local env file first:

```bash
cp .env.example .env
$EDITOR .env
```

Run the host-native smoke check:

```bash
./scripts/run.sh
make run
```

If your local user cannot write `/var/log/s3-archiver`, override `LOG_DIR` to a writable path before running the host-native smoke check.

If you want to run against LocalStack instead of OCI credentials:

```bash
suffix="$(uuidgen | tr '[:upper:]' '[:lower:]')"
source_bucket="s3-archiver-source-${suffix}"
destination_bucket="s3-archiver-destination-${suffix}"
mkdir -p .local
sed \
  -e "s/s3-archiver-source-replace-with-uuid/${source_bucket}/" \
  -e "s/s3-archiver-destination-replace-with-uuid/${destination_bucket}/" \
  .env.e2e >".local/e2e-${suffix}.env"
docker compose --profile test up -d localstack
docker compose --profile test exec -T localstack \
  awslocal s3api create-bucket --bucket "${source_bucket}"
docker compose --profile test exec -T localstack \
  awslocal s3api create-bucket --bucket "${destination_bucket}"
ENV_FILE=".local/e2e-${suffix}.env" \
  S3_SOURCE_ENDPOINT_URL=http://127.0.0.1:4566 \
  S3_DESTINATION_ENDPOINT_URL=http://127.0.0.1:4566 \
  ./scripts/run.sh
ENV_FILE=".local/e2e-${suffix}.env" \
  S3_SOURCE_ENDPOINT_URL=http://127.0.0.1:4566 \
  S3_DESTINATION_ENDPOINT_URL=http://127.0.0.1:4566 \
  make run
```

`./scripts/run.sh` is the canonical host-native smoke-test wrapper, and `make run` delegates to it. The CLI now loads `.env` itself, while the wrapper only selects the env file through `ENV_FILE` or `APP_ENV_FILE`. Inline overrides like `S3_SOURCE_ENDPOINT_URL=...` and `S3_DESTINATION_ENDPOINT_URL=...` still win because process env takes precedence over file values. Docker Compose continues to set `/var/log/s3-archiver` inside the container so the named-volume behavior is unchanged.

Run the health check directly without the wrapper:

```bash
uv run s3-archiver check
ENV_FILE=".local/e2e-${suffix}.env" \
  S3_SOURCE_ENDPOINT_URL=http://127.0.0.1:4566 \
  S3_DESTINATION_ENDPOINT_URL=http://127.0.0.1:4566 \
  uv run s3-archiver check
```

Run one archive invocation directly:

```bash
ENV_FILE=".local/e2e-${suffix}.env" \
  S3_SOURCE_ENDPOINT_URL=http://127.0.0.1:4566 \
  S3_DESTINATION_ENDPOINT_URL=http://127.0.0.1:4566 \
  uv run s3-archiver archive
```

Run the visual demos directly:

```bash
ENV_FILE=".local/e2e-${suffix}.env" uv run s3-archiver demo
ENV_FILE=".local/e2e-${suffix}.env" uv run s3-archiver demo-cleanup
```

`demo` forces cleanup off and ends with a cleanup preview. `demo-cleanup` forces cleanup on
for that invocation and shows the post-cleanup bucket state.

Run the compose-backed visual demo scripts:

```bash
./scripts/run_visual_demo.sh
./scripts/run_visual_cleanup_demo.sh
```

These scripts seed 365 eligible data days across 12 archive roots with 2 source files per
root/day, plus retained and invalid-key examples. The non-cleanup script archives 4,380
daily destination objects and previews deletion. The cleanup script archives the same
shape, then deletes the 8,760 verified source objects while leaving the 4 skipped objects.

Run the production-style local wrapper:

```bash
ENV_FILE=.env ./scripts/run_archive.sh
make archive
ARCHIVER_SCHEDULE_UTC=02:00 ENV_FILE=.env ./scripts/run_archive.sh schedule
ARCHIVER_SCHEDULE_UTC=02:00 make archive-schedule
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
./scripts/run_full_suite.sh
uv build --package s3-archiver-core
uv build --package s3-archiver-cli
```

Run all suites with the canonical coverage-gated command:

```bash
./scripts/run_full_suite.sh
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

- Back up the named volume contents to a local archive:

```bash
mkdir -p .local/log-backups
docker run --rm \
  -v s3-archiver_app_logs:/logs \
  -v "$PWD/.local/log-backups:/backup" \
  alpine:3.22 \
  sh -lc 'tar -czf /backup/app_logs.tgz -C /logs .'
```

## Tests

- Unit tests cover config validation, logging setup, health checks, CLI behavior, and repo policy guards.
- Integration tests run against LocalStack S3 with fixture-managed per-test source and destination buckets, LocalStack endpoint guard rails, and object round-trips.
- E2E tests build and run the compose stack, assert the rootless container can complete `s3-archiver check` and persist logs, and verify the runtime image excludes repo tests and LocalStack test support.
- CI is currently intended to run locally through the documented scripts and Make targets.

LocalStack test-only helpers live under `docker/localstack/test-support` and are mounted only into the LocalStack service by the `test` compose profile. They are not copied into the application runtime image.
Built source distributions and wheels also carry explicit exclusions for test and LocalStack-only assets.

## Local Scheduling

Schedule exactly one local archive task on the production machine. The repo now ships a built-in scheduler loop that runs one `archive` invocation per UTC day and always computes the next future tick. Each invocation archives all eligible retained data days, so missed scheduler ticks do not need catch-up replay.

```bash
cd /opt/s3-archiver
ARCHIVER_SCHEDULE_UTC=02:00 ENV_FILE=/opt/s3-archiver/.env ./scripts/run_archive.sh schedule
```

The same loop is available in Docker Compose:

```bash
ARCHIVER_SCHEDULE_UTC=02:00 docker compose --profile schedule up -d scheduler
docker compose --profile schedule logs -f scheduler
```

If you prefer a host scheduler such as systemd, point it at one `archive` invocation and keep catch-up disabled. For a timer unit, set `Persistent=false` so missed runs are not replayed.

Do not schedule the same archive from GitHub Actions, host cron, systemd, and a container at the same time. Archive exclusivity is acquired before S3 preflight work starts, the lock lives in `LOG_DIR`, and stale-lock recovery is limited to timed-out runs, invalid lock metadata, or a dead owner process proven on the current host.

Timeout failures now surface explicitly with `field="ARCHIVER_RUN_TIMEOUT"`, `reason="archive_run_timeout"`, and `timed_out=true` in the archive JSON payload and structured error logs.

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
