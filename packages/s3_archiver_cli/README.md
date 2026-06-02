# s3-archiver-cli

Typer-based CLI package for the `s3-archiver` entrypoint.

## Commands

- `s3-archiver check`: load configuration, validate all routes, and verify source and destination bucket access.
- `s3-archiver archive`: run one archive invocation for every configured route.
- `s3-archiver schedule`: run the built-in once-per-day UTC scheduler loop.

## Runtime Environment

The CLI loads `.env` by default. `APP_ENV_FILE` or `ENV_FILE` can point at another env file, and process environment variables override file values. Set `APP_ENV_FILE=/dev/null` when the runtime should fail closed unless every required variable is supplied by Compose or the process environment.

Archive routes come from `ARCHIVER_CONFIG_JSON`. Each route chooses a registered parser, a copy mode (`daily_tar_gz` or `direct`), and source and destination S3 locations. Built-in parser names are `filename_timestamp`, `folder_timestamp`, and `direct`; custom parser modules that expose a `Parser` class are registered by filename. `daily_tar_gz` writes deterministic grouped archives by data day. `direct` copy mode writes one destination object per selected source key under the route destination path.

Object size guardrails default to 100 GiB, expressed as `102400` MiB. Set `ARCHIVER_MAX_SOURCE_OBJECT_SIZE_MIB` to skip listed source objects above that size, and `ARCHIVER_MAX_DESTINATION_ARCHIVE_SIZE_MIB` to skip archive groups whose estimated staged tar size is above that size. Skips are logged as warnings and summarized again when the run completes.

Runtime visibility logs include `archive.object.large` before source objects at or above `ARCHIVER_LARGE_OBJECT_LOG_BYTES` (`1073741824` by default), and `archive.object.long_running` when one direct copy or archive-member write exceeds `ARCHIVER_LONG_OBJECT_LOG_SECONDS` (`300` by default).

Use shared S3 environment variables for provider, auth, endpoint, region, addressing style, and OCI fields, then set only the buckets per side:

```shell
S3_PROVIDER=localstack
S3_REGION=us-east-1
S3_ENDPOINT_URL=http://localstack:4566
S3_ACCESS_KEY_ID=test
S3_SECRET_ACCESS_KEY=test
S3_ADDRESSING_STYLE=path
S3_SOURCE_BUCKET=source-bucket
S3_DESTINATION_BUCKET=archive-bucket
```

`ARCHIVER_CONFIG_JSON` route locations only need `path` when a route should be scoped or written below a prefix:

```json
[
  {
    "name": "fae-daily",
    "parser": "filename_timestamp",
    "copy_mode": "daily_tar_gz",
    "source": {
      "path": "data/fae/"
    },
    "destination": {
      "path": "archives/fae/"
    }
  }
]
```
