# s3-archiver-core

Typed configuration, S3 clients, route manifest building, archive execution, logging, and health checks for `s3-archiver`.

## Route Model

`AppSettings.from_env` reads `ARCHIVER_CONFIG_JSON`, a JSON array of route objects. Each route has a unique `name`, a `parser`, a `copy_mode`, a `source` S3 location, and a `destination` S3 location.

Shared S3 connection settings come from environment variables such as `S3_PROVIDER`, `S3_REGION`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_ADDRESSING_STYLE`, `S3_NAMESPACE`, and `S3_IAM_USER_OCID`. Buckets are side-specific with `S3_SOURCE_BUCKET` and `S3_DESTINATION_BUCKET`. Route location objects should only carry route-local path information when needed:

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
  },
  {
    "name": "raw-direct",
    "parser": "direct",
    "copy_mode": "direct",
    "source": {},
    "destination": {}
  }
]
```

Built-in parsers:

- `filename_timestamp`: selects objects with reliable UTC timestamps in the source key basename.
- `folder_timestamp`: selects objects with reliable UTC timestamps in parent folders.
- `folder_timestamp_child`: selects segmented folder timestamps followed by a child folder, for layouts such as `data/wrf/ecmwf/2026/05/16/00/d01/<object>`.
- `direct`: selects objects from S3 `LastModified`.

The repository-level `docs/parsers.md` file documents parser behavior, copy modes, and example route choices in more detail.

Custom parsers can be added by copying `s3_archiver_core/parsers/template.py` to a new
snake_case module in the parser package. Modules that expose a `Parser` class are
registered automatically by filename, excluding the template itself. The copied
filename is the configured parser name, so `customer_timestamp.py` is selected
with `"parser": "customer_timestamp"`. Do not edit `parsers/kinds.py`,
`parsers/__init__.py`, `parsers/registry.py`, or settings to register a new parser.
Discovery is cached for the running Python process, so restart the app after adding
a parser; tests that create parser modules at runtime can call
`clear_parser_registry_cache()`.

Supported copy modes:

- `daily_tar_gz`: groups selected objects by route, archive root, and data day, then writes deterministic `.tar.gz` archives.
- `timestamp_child_tar_gz`: for `folder_timestamp_child` routes, groups selected objects by route, archive root, and data day, then writes archive names from the selected timestamp hour plus child folder, such as `2026-05-16-00-d01.tar.gz`.
- `direct`: copies each selected object to the destination path.

Route names must be unique, source and destination storage identities must differ, and OCI locations require `namespace` and `iam_user_ocid`. Source paths are validated per storage location with directory-boundary prefix semantics, so sibling prefixes such as `data` and `database` are separate routes while `data` and `data/fae` overlap.
