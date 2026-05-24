# Parser × Copy-Mode Matrix

This page documents every supported combination of `parser` and `copy_mode`,
shows the destination layout each combination produces, and gives a
"data-pattern → combination" guide for picking one.

For deeper per-parser and per-copy-mode behavior, see [`parsers.md`](parsers.md).
This page is the cross-reference: it walks all 4 × 3 = 12 combinations of the
parsers (`direct`, `filename_timestamp`, `folder_timestamp`,
`folder_timestamp_child`) and copy modes (`direct`, `daily_tar_gz`,
`timestamp_child_tar_gz`).

## How To Read The Path Examples

Routes carry four strings that together determine the destination key:

- `source.path` — listing prefix on the source bucket. Stripped from the
  archive root before the destination key is built, so the same prefix does
  not appear twice in the output.
- `destination.path` — prefix prepended to every destination key.
- `parser` — selects eligible objects, picks the data timestamp, and reports
  an archive root (a prefix used for grouping).
- `copy_mode` — turns the selected objects into destination keys. `direct`
  copies one source object to one destination object; the two `*_tar_gz`
  modes group selected objects into deterministic `.tar.gz` archives keyed by
  archive root and data day (and, for `timestamp_child_tar_gz`, hour and
  child folder).

All examples below assume `source.path` is set to the natural prefix of the
input data so that the archive root computed by the parser is *relative to
that prefix* by the time the destination key is built. Routes that leave
`source.path` empty will see the full archive root duplicated under
`destination.path`; see "Common pitfalls" at the bottom of this page.

## Quick Selector

If your source keys look like… → use this combination:

| Source key pattern | Parser | Copy mode |
| --- | --- | --- |
| `data/fae/2026/04/13/07/2026-04-13T07-00-00.xml` (timestamp in the filename) | `filename_timestamp` | `daily_tar_gz` |
| `data/harmonie/HARMONIE_DINI_SF_2026-04-24T000000Z_….bz2` (timestamp in the filename, no segmented folders) | `filename_timestamp` | `daily_tar_gz` |
| `data/wrf/gfs/2025/02/01/<file>` (timestamp only in parent folders) | `folder_timestamp` | `daily_tar_gz` |
| `data/wrf/ecmwf/2026/05/16/00/d01/<file>` (timestamp folders followed by a domain/run child folder, grouped per child) | `folder_timestamp_child` | `timestamp_child_tar_gz` |
| Anything where S3 `LastModified` is the only authoritative time and you want one destination object per source key | `direct` | `direct` |
| Anything where S3 `LastModified` is the only authoritative time and you want daily tarballs grouped by the source prefix | `direct` | `daily_tar_gz` |
| Timestamped filenames that should be mirrored 1-for-1 (no tarring) | `filename_timestamp` | `direct` |
| Folder-timestamped objects that should be mirrored 1-for-1 (no tarring) | `folder_timestamp` | `direct` |

## Full Matrix

Legend: ✅ allowed, ❌ rejected at route-config validation. The
`folder_timestamp_child` ↔ `timestamp_child_tar_gz` pairing is enforced
bidirectionally in `_route_config.py` — neither side is accepted without the
other.

| Parser ↓ \ Copy mode → | `direct` | `daily_tar_gz` | `timestamp_child_tar_gz` |
| --- | --- | --- | --- |
| `direct` | ✅ | ✅ | ❌ requires `folder_timestamp_child` |
| `filename_timestamp` | ✅ | ✅ | ❌ requires `folder_timestamp_child` |
| `folder_timestamp` | ✅ | ✅ | ❌ requires `folder_timestamp_child` |
| `folder_timestamp_child` | ❌ must be `timestamp_child_tar_gz` | ❌ must be `timestamp_child_tar_gz` | ✅ |

The seven allowed cells are described below.

## Allowed Combinations

For each combination this section shows: when to choose it, what the
selected timestamp and archive root come from, an example route, an example
source key, and the destination key the archiver writes.

### 1. `direct` + `direct`

**When to choose.** The key carries no authoritative data time and you want
each source object mirrored to one destination object. The only metadata
available for selection is S3 `LastModified`.

- **Selected timestamp:** S3 `LastModified` of the listed object.
- **Archive root (for manifests):** the parent prefix of the source key.
- **Grouping:** none — one source object yields one destination object.

```json
{
  "name": "raw-mirror",
  "parser": "direct",
  "copy_mode": "direct",
  "source": {"path": "raw/"},
  "destination": {"path": "copy/"}
}
```

```text
source:      raw/a.txt
destination: copy/raw/a.txt
```

### 2. `direct` + `daily_tar_gz`

**When to choose.** The key carries no authoritative data time but you want
daily tarballs grouped by the source prefix, using S3 `LastModified` as the
data day.

- **Selected timestamp:** S3 `LastModified` of the listed object.
- **Archive root:** the parent prefix of the source key (relative to
  `source.path`).
- **Grouping:** one `.tar.gz` per (route, archive root, `LastModified`
  date in UTC).

```json
{
  "name": "raw-daily",
  "parser": "direct",
  "copy_mode": "daily_tar_gz",
  "source": {"path": "raw/"},
  "destination": {"path": "archives/raw/"}
}
```

```text
source:      raw/current.txt           (LastModified 2024-04-19T…Z)
destination: archives/raw/2024-04-19.tar.gz
```

### 3. `filename_timestamp` + `direct`

**When to choose.** The basename is authoritative for the data time but the
destination layout should mirror keys 1-for-1 rather than collapsing them
into tarballs (e.g., the consumer expects individual files).

- **Selected timestamp:** the reliable basename timestamp (falls back to a
  path timestamp only when the basename has none and no malformed
  timestamp-looking text).
- **Archive root (for manifests):** parent prefix with trailing
  timestamp-only folders stripped.
- **Grouping:** none — one source object yields one destination object.

```json
{
  "name": "fae-mirror",
  "parser": "filename_timestamp",
  "copy_mode": "direct",
  "source": {"path": "data/fae/"},
  "destination": {"path": "copy/"}
}
```

```text
source:      data/fae/2026-04-13T03-00-00Z.xml
destination: copy/data/fae/2026-04-13T03-00-00Z.xml
```

### 4. `filename_timestamp` + `daily_tar_gz`

**When to choose.** The most common archive route. The basename carries the
authoritative data time (with or without supporting `YYYY/MM/DD[/HH]`
folders) and you want one tarball per archive root per data day.

- **Selected timestamp:** the reliable basename timestamp (with the path
  fallback described above).
- **Archive root:** parent prefix with trailing timestamp-only folders
  stripped, made relative to `source.path`.
- **Grouping:** one `.tar.gz` per (route, archive root, selected-timestamp
  date in UTC).

```json
{
  "name": "fae",
  "parser": "filename_timestamp",
  "copy_mode": "daily_tar_gz",
  "source": {"path": "data/fae/"},
  "destination": {"path": "archives/fae/"}
}
```

```text
source:      data/fae/2026/04/13/2026-04-13T03-00-00Z.xml
destination: archives/fae/2026-04-13.tar.gz
```

Another shape with a flat layout:

```text
source:      data/harmonie/HARMONIE_DINI_SF_2026-04-24T000000Z_2026-04-24T000000Z.bz2
destination: archives/harmonie/2026-04-24.tar.gz
```

### 5. `folder_timestamp` + `direct`

**When to choose.** The parent folders are authoritative for the data time
and the basename has no timestamp, but you want each source object mirrored
1-for-1 rather than tarred.

- **Selected timestamp:** the latest reliable folder timestamp
  (`YYYY/MM/DD` or `YYYY/MM/DD/HH`) — basename-only timestamps are ignored
  by this parser.
- **Archive root (for manifests):** parent prefix with trailing
  timestamp-only folders stripped; non-timestamp folders that follow the
  timestamp folders remain part of the archive root.
- **Grouping:** none — one source object yields one destination object.

```json
{
  "name": "fae-folder-mirror",
  "parser": "folder_timestamp",
  "copy_mode": "direct",
  "source": {"path": "data/fae/"},
  "destination": {"path": "copy/"}
}
```

```text
source:      data/fae/2026/04/13/07/no-stamp.xml
destination: copy/data/fae/2026/04/13/07/no-stamp.xml
```

### 6. `folder_timestamp` + `daily_tar_gz`

**When to choose.** Folders are authoritative for the data time (no
basename timestamp), and you want one tarball per archive root per data day.

- **Selected timestamp:** the latest reliable folder timestamp (segmented
  `YYYY/MM/DD[/HH]`).
- **Archive root:** parent prefix with trailing timestamp-only folders
  stripped (so non-timestamp folders following the timestamp survive into
  the archive root), made relative to `source.path`.
- **Grouping:** one `.tar.gz` per (route, archive root, folder-timestamp
  date in UTC).

```json
{
  "name": "wrf-gfs",
  "parser": "folder_timestamp",
  "copy_mode": "daily_tar_gz",
  "source": {"path": "data/wrf/gfs/"},
  "destination": {"path": "data/wrf/gfs/"}
}
```

```text
source:      data/wrf/gfs/2025/02/01/wrfout_d01.nc
destination: data/wrf/gfs/2025-02-01.tar.gz
```

### 7. `folder_timestamp_child` + `timestamp_child_tar_gz`

**When to choose.** WRF-style layouts where timestamp folders
(`YYYY/MM/DD` or `YYYY/MM/DD/HH`) are followed by a domain/run child
folder (`d01`, `d02`, …) and each child should land in its own tarball
keyed by hour. This is the only combination either side will accept —
route validation rejects every other pairing on both sides.

- **Selected timestamp:** the latest segmented folder timestamp; the
  `HH` segment is used when present, otherwise hour `00`.
- **Archive root:** the full prefix through the child folder
  (e.g., `data/wrf/ecmwf/2026/05/16/00/d01`), made relative to
  `source.path`. Objects nested deeper under the same child folder share
  the same archive root.
- **Grouping:** one `.tar.gz` per (route, archive root,
  destination key, data day). The destination key collapses to a flat
  filename `{YYYY-MM-DD-HH}-{child}.tar.gz` placed under
  `destination.path`; the archive-root intermediate folders are *not*
  reflected in the destination key, only in the filename suffix.

```json
{
  "name": "wrf-ecmwf",
  "parser": "folder_timestamp_child",
  "copy_mode": "timestamp_child_tar_gz",
  "source": {"path": "data/wrf/ecmwf/"},
  "destination": {"path": "data/wrf/ecmwf/"}
}
```

```text
source:      data/wrf/ecmwf/2026/05/16/00/d01/out.grib
source:      data/wrf/ecmwf/2026/05/16/00/d01/nested/aux.grib
destination: data/wrf/ecmwf/2026-05-16-00-d01.tar.gz   (both objects, same tarball)

source:      data/wrf/ecmwf/2026/05/16/00/d02/out.grib
destination: data/wrf/ecmwf/2026-05-16-00-d02.tar.gz

source:      data/wrf/ecmwf/2026/05/16/06/d01/out.grib
destination: data/wrf/ecmwf/2026-05-16-06-d01.tar.gz
```

If the archive root has no segments after `source.path` is stripped, the
child fallback is the literal string `archive`, yielding e.g.
`2026-05-16-00-archive.tar.gz`. This is an edge case for misconfigured
routes; in practice `source.path` should stop *before* the timestamp
folders so a real child name is preserved.

## Rejected Combinations

Route-config validation in
`packages/s3_archiver_core/src/s3_archiver_core/_route_config.py` rejects
the following five combinations at load time:

| Parser | Copy mode | Reason |
| --- | --- | --- |
| `direct` | `timestamp_child_tar_gz` | `copy_mode=timestamp_child_tar_gz` requires `parser=folder_timestamp_child`. |
| `filename_timestamp` | `timestamp_child_tar_gz` | Same — copy mode requires the child parser. |
| `folder_timestamp` | `timestamp_child_tar_gz` | Same — copy mode requires the child parser. |
| `folder_timestamp_child` | `direct` | `parser=folder_timestamp_child` requires `copy_mode=timestamp_child_tar_gz`. |
| `folder_timestamp_child` | `daily_tar_gz` | Same — parser requires the child copy mode. |

The pairing is exclusive because the child-folder grouping semantics live
on both sides: the parser produces an archive root that *includes* the
child folder, and the copy mode produces a destination filename that
*depends on* that child folder being the last archive-root segment. Mixing
either side with a different counterpart would silently produce wrong
groupings, so the validator refuses the route instead.

## Decision Flow

A short flow for picking a combination from a new source layout:

1. **Does the object key contain a timestamp you can trust?**
   - No, only S3 `LastModified` is meaningful → parser = `direct`.
   - Yes → continue.
2. **Where is the timestamp?**
   - In the basename (with or without supporting folders) → parser =
     `filename_timestamp`.
   - Only in parent folders, no basename timestamp, **and** there is a
     meaningful child folder (`d01`, `d02`, …) after the timestamp folders
     that should drive grouping → parser = `folder_timestamp_child` (and
     copy mode is forced to `timestamp_child_tar_gz`).
   - Only in parent folders, no basename timestamp, no special child
     folder semantics → parser = `folder_timestamp`.
3. **How should the destination be shaped?**
   - One destination object per source object → `copy_mode = direct`.
   - One tarball per archive root per UTC data day → `copy_mode =
     daily_tar_gz`.
   - WRF-style hour + child-folder tarballs → already chosen above
     (`copy_mode = timestamp_child_tar_gz`).

## Common Pitfalls

- **`source.path` not aligned with the data prefix.** The parser-reported
  archive root is made relative to `source.path` before the destination
  key is built. If `source.path` is empty or shorter than the prefix that
  appears in keys, the absolute archive root is preserved and will appear
  *under* `destination.path` in the output — yielding doubled-up prefixes
  like `archives/fae/data/fae/2026-04-13.tar.gz`. Set `source.path` to
  the common prefix of the inputs you actually intend to archive.
- **Choosing `folder_timestamp` for keys whose basenames also have
  timestamps.** `folder_timestamp` deliberately ignores basename
  timestamps. If the basename is the more reliable source, use
  `filename_timestamp` — it has its own fallback to path timestamps when
  the basename has none.
- **Choosing `folder_timestamp_child` for layouts without a child
  folder.** The parser requires at least one folder *after* the timestamp
  segments. Keys whose timestamp folders are the last folders before the
  basename will all be skipped with reason
  `"no reliable folder timestamp child"`.
- **Trying to opt out of the
  `folder_timestamp_child` ↔ `timestamp_child_tar_gz` pairing.** You
  cannot. Use a different parser if you need `direct` or `daily_tar_gz`
  output, or a different copy mode if you need a different parser.
