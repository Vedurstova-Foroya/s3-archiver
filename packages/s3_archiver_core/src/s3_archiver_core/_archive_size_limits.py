"""Archive object and archive-size policy helpers."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from collections.abc import Iterable, Sequence

from s3_archiver_core._archive_manifest_models import ArchiveGroup, ManifestEntry, SkippedObject

_LOGGER = logging.getLogger("s3_archiver.archive")
_DEFAULT_MAX_SIZE_MIB = 102_400
_MIB = 1024 * 1024
_MAX_SOURCE_OBJECT_SIZE_MIB_ENV = "ARCHIVER_MAX_SOURCE_OBJECT_SIZE_MIB"
_MAX_DESTINATION_ARCHIVE_SIZE_MIB_ENV = "ARCHIVER_MAX_DESTINATION_ARCHIVE_SIZE_MIB"


def max_source_object_size_bytes() -> int:
    """Return the configured maximum source-object size in bytes."""

    return _mib_env(_MAX_SOURCE_OBJECT_SIZE_MIB_ENV) * _MIB


def max_destination_archive_size_bytes() -> int:
    """Return the configured maximum destination archive size in bytes."""

    return _mib_env(_MAX_DESTINATION_ARCHIVE_SIZE_MIB_ENV) * _MIB


def source_object_skip_reason(size: int) -> str | None:
    """Return the skip reason when a listed source object exceeds policy."""

    limit = max_source_object_size_bytes()
    if size <= limit:
        return None
    return f"source object size {size} exceeds max source object size {limit}"


def archive_group_skip_reason(estimated_size: int, limit: int) -> str:
    """Return the skip reason when an archive group exceeds the size policy."""

    return (
        f"estimated destination archive size {estimated_size} exceeds "
        f"max destination archive size {limit}"
    )


def estimated_archive_size_bytes(entries: Iterable[ManifestEntry]) -> int:
    """Return a conservative local tar staging-size estimate for archive entries."""

    tar_bytes = 1024
    for entry in entries:
        tar_bytes += 512
        tar_bytes += _tar_padded_size(entry.size)
    return tar_bytes + 1024 * 1024


def log_source_object_skip(skipped: SkippedObject, *, max_size_bytes: int) -> None:
    """Warn that a listed source object was skipped by size policy."""

    _LOGGER.warning(
        "archive source object skipped source_key=%s reason=%s",
        skipped.key,
        skipped.reason,
        extra={
            "event": "archive.object.skipped",
            "reason": skipped.reason,
            "route_name": skipped.route_name,
            "parser_kind": skipped.parser_kind,
            "copy_mode": skipped.copy_mode,
            "source_bucket": skipped.source_bucket,
            "source_key": skipped.key,
            "source_version_id": skipped.version_id,
            "size_bytes": skipped.size,
            "max_source_object_size_bytes": max_size_bytes,
        },
    )


def log_skipped_summary(skipped: Sequence[SkippedObject]) -> None:
    """Warn with a completion-time skipped-object summary when anything was skipped."""

    if len(skipped) == 0:
        return
    counts = Counter(item.reason for item in skipped)
    reason_counts = dict(sorted(counts.items()))
    _LOGGER.warning(
        "archive skipped object summary skipped_object_count=%d",
        len(skipped),
        extra={
            "event": "archive.skipped_objects.summary",
            "skipped_object_count": len(skipped),
            "skipped_reason_counts": reason_counts,
            "skipped_reason_counts_json": json.dumps(reason_counts, sort_keys=True),
        },
    )


def skipped_archive_entry(entry: ManifestEntry, reason: str) -> SkippedObject:
    return SkippedObject(
        key=entry.key,
        reason=reason,
        route_name=entry.route_name,
        parser_kind=entry.parser_kind,
        copy_mode=entry.copy_mode,
        size=entry.size,
        last_modified=entry.last_modified,
        etag=entry.etag,
        version_id=entry.version_id,
        selected_timestamp=entry.selected_timestamp,
        timestamp_source=entry.timestamp_source,
        target_day=entry.target_day,
        archive_root=entry.archive_root,
        source_bucket=entry.source_bucket,
        source_path=entry.source_path,
        destination_bucket=entry.destination_bucket,
        destination_path=entry.destination_path,
        source_identity=entry.source_identity,
        destination_identity=entry.destination_identity,
    )


def log_archive_group_skip(
    group: ArchiveGroup, reason: str, estimated_size: int, max_size: int
) -> None:
    _LOGGER.warning(
        "archive group skipped destination_key=%s reason=%s",
        group.destination_archive_key,
        reason,
        extra={
            "event": "archive.archive_group.skipped",
            "reason": reason,
            "route_name": group.route_name,
            "parser_kind": group.parser_kind,
            "copy_mode": group.copy_mode,
            "source_bucket": group.source_bucket,
            "destination_bucket": group.destination_bucket,
            "destination_key": group.destination_archive_key,
            "source_object_count": group.source_count or len(group.entries),
            "estimated_archive_size_bytes": estimated_size,
            "max_destination_archive_size_bytes": max_size,
        },
    )


def _tar_padded_size(size: int) -> int:
    return ((size + 511) // 512) * 512


def _mib_env(name: str) -> int:
    value = os.getenv(name)
    if value is None:
        return _DEFAULT_MAX_SIZE_MIB
    try:
        parsed = int(value)
    except ValueError:
        return _DEFAULT_MAX_SIZE_MIB
    return parsed if parsed > 0 else _DEFAULT_MAX_SIZE_MIB
