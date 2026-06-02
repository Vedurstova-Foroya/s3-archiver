# s3-archiver — Detailed Guide

This is the long-form companion to the top-level `README.md`. The top-level
README is intentionally minimal (install + run). This document holds
everything else: layout, configuration, archive routes, local development,
logging, tests, scheduling, releases, and deployment.

## Layout

- `packages/s3_archiver_core`: typed config, logging, S3 client creation, and health checks
- `packages/s3_archiver_cli`: Typer CLI and `s3-archiver` entrypoint
- `tests/unit`, `tests/integration`, `tests/e2e`: full test suite

## Compose-Backed Health Check Flow

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
APP_ENV_FILE=".local/e2e-${suffix}.env" docker compose --profile test run --rm app check
docker compose --profile test down -v
```

Running the app container without an explicit command prints CLI help and exits `0`.
Use `s3-archiver check` for startup validation, `s3-archiver archive` for one archive invocation, and `s3-archiver schedule` for the built-in once-per-day UTC scheduler loop.

The container runs rootless and writes JSON log files to the `app_logs` named volume mounted at `/var/log/s3-archiver` in the container.
The checked-in env files default `LOG_DIR` to `/var/log/s3-archiver` to match the runtime contract used by the container image and Compose stack.

Use `.env.example` for OCI-backed runs and `.env.e2e` as the template for the LocalStack compose flow shown above.
Archive route configuration is supplied through `ARCHIVER_CONFIG_JSON`, a JSON array of route objects that choose a parser, copy mode, source location, and destination location.
Each archive run selects source objects through the configured parser, then writes archive output according to the route copy mode.
Destination archive filenames use the data day from the source key, not the run date.
LocalStack readiness now only proves the S3 API is reachable. The pytest integration and e2e harnesses generate LocalStack-only env files with fresh UUID-suffixed source and destination buckets for each test, then create and tear down those buckets in fixtures.

## Archive Routes

`ARCHIVER_CONFIG_JSON` is the only archive routing configuration surface. It must be a JSON array, where each object has:

- `name`: unique route name used in logs, manifests, health output, and archive result payloads.
- `parser`: registered parser name, such as `filename_timestamp`, `folder_timestamp`,
  `folder_timestamp_child`, or `direct`.
- `copy_mode`: `daily_tar_gz`, `timestamp_child_tar_gz`, or `direct`.
- `source`: source S3 location object.
- `destination`: destination S3 location object.

Source and destination location objects use the same schema: optional `provider`, optional `region`, `bucket`, optional `namespace`, optional `iam_user_ocid`, optional `endpoint_url`, optional `access_key_id`, optional `secret_access_key`, optional `addressing_style`, and optional `path`. `provider` is `oci` or `localstack`; `addressing_style` is `path` or `virtual`. For OCI routes, omit `endpoint_url` to derive `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`. `path` scopes the route to a prefix on that side and may be empty.

Keep credentials and shared S3 connection settings in environment variables. Missing location fields are resolved from the explicit route value, then the side-specific environment variable, then the shared `S3_*` environment variable, then a built-in default where one is valid. For example, `source.region` falls back to `S3_SOURCE_REGION`, then `S3_REGION`, then `us-east-1`; `destination.access_key_id` falls back to `S3_DESTINATION_ACCESS_KEY_ID`, then `S3_ACCESS_KEY_ID`. Buckets intentionally do not have a shared fallback: source buckets use `S3_SOURCE_BUCKET`, and destination buckets use `S3_DESTINATION_BUCKET`. Common shared defaults are `S3_PROVIDER`, `S3_REGION`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_NAMESPACE`, `S3_IAM_USER_OCID`, and `S3_ADDRESSING_STYLE`.

Parser behavior:

- `filename_timestamp`: prefers reliable UTC timestamps in the source key basename. Path-only timestamps can be selected as a fallback only when the basename has no timestamp and no malformed basename timestamp. Objects without a usable timestamp are reported as skipped.
- `folder_timestamp`: selects objects whose parent folders contain a reliable UTC timestamp. Objects without a usable folder timestamp are reported as skipped.
- `folder_timestamp_child`: selects objects whose parent folders contain segmented timestamp folders followed by a child folder, and groups archives through that child folder. This fits layouts such as `data/wrf/ecmwf/2026/05/16/00/d01/<object>`.
- `direct`: selects objects using S3 `LastModified` as the parser timestamp and uses the parent prefix as the archive root. The listed object, hydrated S3 headers, metadata, tags, size, version id, and checksums are available for manifest, copy, and verification decisions.

Copy modes:

- `daily_tar_gz`: writes one deterministic `.tar.gz` archive per route, archive root, and data day.
- `timestamp_child_tar_gz`: for `folder_timestamp_child` routes, writes one deterministic `.tar.gz` archive per route, archive root, and selected timestamp hour plus child folder, such as `2026-05-16-00-d01.tar.gz`.
- `direct`: copies each selected source object directly to the destination path.

Size guardrails:

- `ARCHIVER_MAX_SOURCE_OBJECT_SIZE_MIB` defaults to `102400` MiB. Listed source objects larger than this are skipped before copy.
- `ARCHIVER_MAX_DESTINATION_ARCHIVE_SIZE_MIB` defaults to `102400` MiB. Archive groups whose estimated staged tar size is larger than this are skipped before local archive creation.

Parser and copy mode are independent: `parser: direct` means select by S3 `LastModified`, while `copy_mode: direct` means write one destination object per selected source key.
See `docs/parsers.md` for detailed parser and copy-mode behavior, and
`docs/parser-copy-mode-matrix.md` for every supported parser × copy_mode
combination with destination-path examples and a selection guide.

Minimal env example:

```env
S3_PROVIDER=oci
S3_REGION=eu-frankfurt-1
S3_NAMESPACE=replace-me
S3_IAM_USER_OCID=ocid1.user.oc1..replace-me
S3_ACCESS_KEY_ID=replace-me
S3_SECRET_ACCESS_KEY=replace-me
S3_ADDRESSING_STYLE=path
S3_SOURCE_BUCKET=source-bucket
S3_DESTINATION_BUCKET=archive-bucket
ARCHIVER_CONFIG_JSON=[{"name":"daily","parser":"filename_timestamp","copy_mode":"daily_tar_gz","source":{"path":"incoming/"},"destination":{}}]
```

The route only includes `path` when it needs prefix scoping or placement. Shared S3 auth and connection values come from `S3_*`; bucket names come from the side-specific bucket env vars.

Equivalent expanded route example:

```json
[
  {
    "name": "daily",
    "parser": "filename_timestamp",
    "copy_mode": "daily_tar_gz",
    "source": {"path": "incoming/"},
    "destination": {}
  }
]
```

Use explicit location fields only when a route differs from the env defaults. For example, set `source.path` to scope source selection to one prefix, or set `destination.path` for advanced placement of generated archives.

### Create A New Parser

Custom parsers live in `packages/s3_archiver_core/src/s3_archiver_core/parsers`.
The module filename is the parser name used in route config. Any non-template
module in that package that exposes a callable `Parser` is discovered at
startup. Parser discovery is cached for the running Python process, so restart
the app after adding a parser; tests that create parser modules at runtime can
call `clear_parser_registry_cache()`.

1. Copy `packages/s3_archiver_core/src/s3_archiver_core/parsers/template.py`.
2. Rename the copy to a snake_case parser name, for example `customer_timestamp.py`.
3. Edit the sections marked `CHANGE HERE` in the copied file.
4. Keep the parser class named `Parser`.
5. Do not edit `parsers/kinds.py`, `parsers/__init__.py`, `parsers/registry.py`, or route settings for registration.
6. Use `"parser": "customer_timestamp"` in `ARCHIVER_CONFIG_JSON`.
7. Run the targeted parser tests:

```bash
uv run pytest tests/unit/test_parsers.py tests/unit/test_route_config_settings.py -m unit
```

Removed environment variables are rejected when set. Migrate `ARCHIVER_RETENTION_DAYS`, `ARCHIVER_ENABLE_CLEANUP`, `ARCHIVER_MAX_WORKERS`, `S3_SOURCE_PATH_WHITELIST_ENABLED`, `S3_SOURCE_PATH_WHITELIST`, `S3_SOURCE_PATH_BLACKLIST_ENABLED`, and `S3_SOURCE_PATH_BLACKLIST` into explicit route JSON. Use route `path` values for source selection and destination placement, choose `parser` for object selection behavior, and choose `copy_mode` for archive-vs-direct output behavior.

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
  S3_ENDPOINT_URL=http://127.0.0.1:4566 \
  ./scripts/run.sh
ENV_FILE=".local/e2e-${suffix}.env" \
  S3_ENDPOINT_URL=http://127.0.0.1:4566 \
  make run
```

`./scripts/run.sh` is the canonical host-native smoke-test wrapper, and `make run` delegates to it. The CLI now loads `.env` itself, while the wrapper only selects the env file through `ENV_FILE` or `APP_ENV_FILE`. Inline overrides like `S3_ENDPOINT_URL=...` still win because process env takes precedence over file values. Docker Compose continues to set `/var/log/s3-archiver` inside the container so the named-volume behavior is unchanged.

Run the health check directly without the wrapper:

```bash
uv run s3-archiver check
ENV_FILE=".local/e2e-${suffix}.env" \
  S3_ENDPOINT_URL=http://127.0.0.1:4566 \
  uv run s3-archiver check
```

Run one archive invocation directly:

```bash
ENV_FILE=".local/e2e-${suffix}.env" \
  S3_ENDPOINT_URL=http://127.0.0.1:4566 \
  uv run s3-archiver archive
```

Run the manual compose-backed visual demo:

```bash
./scripts/run_visual_demo.sh
```

The manual demo lives in the separate `s3-archiver-visual-demo` package and is not part of
the regular e2e suite. It seeds 365 eligible data days across all parser and copy-mode combinations:
`direct`, `filename_timestamp`, and `folder_timestamp`, each with `daily_tar_gz` and
`direct` routes. The demo writes 2,190 tar.gz archives, 2,190 direct-copy destination
objects, and leaves the source bucket unchanged.

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
- The file logger keeps 30 daily files.
- Large source objects emit an `archive.object.large` log before transfer; `ARCHIVER_LARGE_OBJECT_LOG_BYTES` defaults to `1073741824`.
- Single-object copy/archive-member writes emit `archive.object.long_running` after `ARCHIVER_LONG_OBJECT_LOG_SECONDS`, which defaults to `300`.
- Oversized object/archive skips are logged as warnings, and completion logs repeat skipped-object counts by reason.
- Inspect the file logs with Docker:

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

Schedule exactly one local archive task on the production machine. The repo now ships a built-in scheduler loop that runs one `archive` invocation per UTC day and always computes the next future tick. Each invocation archives all eligible data days, so missed scheduler ticks do not need catch-up replay.

```bash
cd /opt/s3-archiver
ARCHIVER_SCHEDULE_UTC=02:00 ENV_FILE=/opt/s3-archiver/.env ./scripts/run_archive.sh schedule
```

The same loop is available in Docker Compose:

```bash
ARCHIVER_SCHEDULE_UTC=02:00 docker compose up -d scheduler
docker compose logs -f scheduler
```

If you prefer a host scheduler such as systemd, point it at one `archive` invocation and keep catch-up disabled. For a timer unit, set `Persistent=false` so missed runs are not replayed.

Do not schedule the same archive from GitHub Actions, host cron, systemd, and a container at the same time. Archive exclusivity is acquired before S3 preflight work starts, the lock lives in `LOG_DIR`, and stale-lock recovery is limited to timed-out runs, invalid lock metadata, or a dead owner process proven on the current host.

Timeout failures now surface explicitly with `field="ARCHIVER_RUN_TIMEOUT"`, `reason="archive_run_timeout"`, and `timed_out=true` in the archive JSON payload and structured error logs.

Archive result payloads are compact by default for production-scale runs: they include counts,
phase status, archive days, and route summaries, but omit per-object destination lists. Set
`ARCHIVER_PAYLOAD_DETAIL=full` only for small debugging runs or the visual demo.

Large manifests spill from memory into a temporary SQLite database after 100,000 manifest rows.
Daily `tar.gz` archive groups are capped by both source bytes and object count so temporary archive
files stay bounded. The defaults are 100 GiB and 2,000,000 source objects; override them with
`ARCHIVER_ARCHIVE_GROUP_MAX_BYTES` and `ARCHIVER_ARCHIVE_GROUP_MAX_OBJECTS` if a deployment needs
smaller or larger archive parts.

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
