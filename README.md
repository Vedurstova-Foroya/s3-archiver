# s3-archiver

S3-to-S3 archiver that groups source objects into deterministic `.tar.gz`
archives (or mirrors them as-is). OCI S3-compatible, LocalStack-tested,
unprivileged runtime, `uv`-managed Python monorepo.

## Deploy (Docker)

```bash
docker compose up -d
```

That builds the image and starts the once-per-day UTC scheduler. By
default it reads `.env` from the repo root — start from `.env.example`
(OCI) or `.env.e2e` (LocalStack) and fill in S3 credentials and
`ARCHIVER_CONFIG_JSON`. Override the path with `APP_ENV_FILE=...` if you
keep it elsewhere.

## Run an archive pass

Run one archive pass in the background (it is owned by the Docker daemon, so it
survives a shell logout) and watch its output:

```bash
docker compose up -d --build archive   # run one archive pass in the background
docker compose logs archive            # show the output so far
docker compose logs -f archive         # follow the live output
```

The `archive` container runs once and exits (it does not restart). Re-run
`docker compose up -d --build archive` to trigger another pass; logs from earlier
passes stay available until you `docker compose rm archive`. Each pass writes a
cleanup manifest of the source objects it archived.

```bash
docker compose run --rm app check      # validate config + S3
docker compose run --rm app cleanup    # delete the archived source objects
docker compose logs -f scheduler       # tail the scheduler loop
```

Set `CLEANUP=true` to delete archived source objects automatically after each
scheduled archive run; the manual `cleanup` command always cleans up regardless.

Logs persist to the `app_logs` named volume at `/var/log/s3-archiver`.

## Dev (host)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv sync --all-packages --all-groups
uv run pre-commit install --install-hooks --hook-type commit-msg --hook-type pre-push
cp .env.example .env && $EDITOR .env
uv run s3-archiver check
```

## Docs

- [`docs/readme-detailed.md`](docs/readme-detailed.md) — full guide: layout, compose flows, local dev, logging, tests, scheduling, releases, amd64 builds.
- [`docs/parsers.md`](docs/parsers.md) — parser and copy-mode behavior.
- [`docs/parser-copy-mode-matrix.md`](docs/parser-copy-mode-matrix.md) — every `parser` × `copy_mode` combination with destination-path examples.
