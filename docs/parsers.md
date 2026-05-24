# Parsers And Copy Modes

Archive routes use two string fields:

- `parser`: selects which source objects are eligible and which timestamp/archive root they use.
- `copy_mode`: selects how eligible objects are written to the destination.

Both fields are plain strings. `copy_mode` is not an object and does not carry nested options.
When a layout needs different parsing or grouping semantics, add a focused parser name rather
than overloading an existing parser or copy mode.

Most parser and copy-mode values combine freely, but `folder_timestamp_child` and
`timestamp_child_tar_gz` are paired: route configuration rejects either one without the other.

## Parser Contract

A parser receives one listed S3 object and returns either:

- a selected object with a UTC timestamp, timestamp source, and archive root; or
- a skipped object with a reason.

For archive-producing copy modes, archive groups are keyed by route, archive root, destination
archive key, and data day. For `direct`, the archive root is still recorded in manifests, but
each selected source key is copied as its own destination object.

Parser modules are discovered by filename from `s3_archiver_core.parsers`. A module named
`customer_timestamp.py` with a callable `Parser` class is configured as
`"parser": "customer_timestamp"`.

## Built-In Parsers

### `filename_timestamp`

Selects timestamps embedded in object keys, preferring the basename.

Use this for keys where the file name is authoritative, such as:

```text
data/fae/2026/04/13/07/2026-04-13T07-00-00.xml
data/harmonie/HARMONIE_DINI_SF_2026-04-24T000000Z_2026-04-24T000000Z.bz2
```

Behavior:

- Uses a reliable basename timestamp when one exists.
- Falls back to a path timestamp only when the basename has no timestamp and no malformed
  timestamp-looking text.
- Reports objects without a usable key timestamp as skipped.
- Uses `archive_root_for_key`, which removes trailing timestamp-only folders from the parent
  path when forming the archive root.

### `folder_timestamp`

Selects timestamps from parent folders and ignores basename-only timestamps.

Use this for keys where folder structure is authoritative, such as:

```text
data/wrf/gfs/2025/02/01/<object>
data/fae/2026/04/13/07/no-stamp.xml
```

Behavior:

- Uses timestamps found in parent folders.
- Supports segmented folders such as `2026/04/13` and `2026/04/13/07`.
- Reports objects without a usable folder timestamp as skipped.
- Uses the standard archive root logic. If the timestamp folders are the trailing parent
  folders, they are removed from the archive root. If non-timestamp folders follow them,
  those folders remain part of the archive root.

### `folder_timestamp_child`

Selects a segmented folder timestamp and groups by the first child folder after that timestamp.

Use this for WRF-style layouts where the timestamp folders are followed by a domain/run folder:

```text
data/wrf/ecmwf/2026/05/16/00/d01/<object>
data/wrf/ecmwf/2026/05/16/00/d01/nested/<object>
data/wrf/ecmwf/2026/05/16/00/d02/<object>
```

Behavior:

- Uses segmented parent folder timestamps: `YYYY/MM/DD` or `YYYY/MM/DD/HH`.
- Requires one child folder after the selected timestamp, such as `d01`.
- Uses the latest segmented folder timestamp when a path contains more than one.
- Sets the archive root through the child folder, so nested objects under `d01` stay in the
  same archive group.
- Reports objects without a timestamp plus child folder as skipped.

Example route:

```json
{
  "name": "wrf-ecmwf",
  "parser": "folder_timestamp_child",
  "copy_mode": "timestamp_child_tar_gz",
  "source": {"path": "data/wrf/ecmwf/"},
  "destination": {"path": "data/wrf/ecmwf/"}
}
```

An object at:

```text
data/wrf/ecmwf/2026/05/16/00/d01/out.grib
```

is archived under:

```text
data/wrf/ecmwf/2026-05-16-00-d01.tar.gz
```

### `direct`

Selects every listed object using S3 `LastModified` as the parser timestamp.

Use this when the object key does not contain authoritative data time and S3 metadata is the
selection source.

Behavior:

- Uses `LastModified` as the selected timestamp.
- Uses the parent key prefix as the archive root.
- Leaves listed object properties available to manifest, copy, and verification code.

## Copy Modes

### `daily_tar_gz`

Writes deterministic `.tar.gz` archives grouped by route, archive root, and data day.

The data day comes from the parser-selected timestamp, not the run date.

### `timestamp_child_tar_gz`

Writes deterministic `.tar.gz` archives grouped by route, archive root, destination key, and
data day, with flat archive names derived from the parser-selected UTC timestamp hour and the
last archive-root segment.

Use this with `folder_timestamp_child` for WRF-style domain folders. For archive root
`2026/05/16/00/d01`, selected timestamp `2026-05-16T00:00:00Z`, and destination path
`data/wrf/ecmwf/`, the destination archive key is:

```text
data/wrf/ecmwf/2026-05-16-00-d01.tar.gz
```

Route configuration rejects this copy mode unless the parser is `folder_timestamp_child`, and
also rejects `folder_timestamp_child` unless this copy mode is selected.

### `direct`

Copies each selected object directly to the destination path.

This is independent from the `direct` parser. For example, `parser: "filename_timestamp"` with
`copy_mode: "direct"` selects by key timestamp but writes one destination object per source key.

## Example Choices

| Source key pattern | Parser | Copy mode |
| --- | --- | --- |
| `data/fae/2026/04/13/07/2026-04-13T07-00-00.xml` | `filename_timestamp` | `daily_tar_gz` |
| `data/harmonie/HARMONIE_DINI_SF_2026-04-24T000000Z_2026-04-24T000000Z.bz2` | `filename_timestamp` | `daily_tar_gz` |
| `data/wrf/gfs/2025/02/01/<object>` | `folder_timestamp` | `daily_tar_gz` |
| `data/wrf/ecmwf/2026/05/16/00/d01/<object>` | `folder_timestamp_child` | `timestamp_child_tar_gz` |

S3 stores object keys, not directories. A prefix such as `data/wrf/gfs/2025/02/01/` only becomes
archivable when objects exist under that prefix.

For every supported combination of `parser` and `copy_mode` — including the
rejected pairings, full destination-path examples, and a decision flow for
picking one — see [`parser-copy-mode-matrix.md`](parser-copy-mode-matrix.md).
