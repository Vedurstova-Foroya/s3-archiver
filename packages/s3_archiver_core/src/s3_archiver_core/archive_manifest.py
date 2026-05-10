from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol

from s3_archiver_core.archive_timestamp import (
    TimestampSource,
    archive_root_for_key,
    destination_archive_key,
    select_key_timestamp,
)
from s3_archiver_core.s3 import S3ListedObject, VersioningState

FilterMode = Literal["none", "whitelist", "blacklist"]

__all__ = (
    "ArchiveGroup",
    "ArchiveManifest",
    "ManifestEntry",
    "SkippedObject",
    "SourceLister",
    "SourcePathFilter",
    "TimestampSource",
    "archive_root_for_key",
    "build_archive_manifest",
    "destination_archive_key",
    "select_key_timestamp",
)


class SourceLister(Protocol):
    """Source bucket interface used to build archive manifests."""

    @property
    def bucket(self) -> str:
        """Return the source bucket name."""
        ...

    def list_source_objects(self, versioning_state: VersioningState) -> Iterable[S3ListedObject]:
        """List source objects for the given versioning state."""
        ...


@dataclass(frozen=True, slots=True)
class SourcePathFilter:
    """Prefix filter applied while selecting archive candidates."""

    mode: FilterMode = "none"
    prefixes: tuple[str, ...] = ()

    def includes(self, key: str) -> bool:
        """Return whether a key is included by this filter."""
        if self.mode == "none":
            return True
        matched = any(key.startswith(prefix) for prefix in self.prefixes)
        if self.mode == "whitelist":
            return matched
        return not matched


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One source object selected for a daily archive."""

    source_bucket: str
    key: str
    size: int
    last_modified: datetime
    etag: str | None
    version_id: str | None
    object: S3ListedObject
    selected_timestamp: datetime | None = None
    timestamp_source: TimestampSource | None = None
    target_day: date | None = None
    archive_root: str = ""
    destination_archive_key: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveGroup:
    """Source objects grouped into one destination archive."""

    target_day: date
    archive_root: str
    destination_archive_key: str
    entries: tuple[ManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class SkippedObject:
    """Source object skipped while building a manifest."""

    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """Complete archive plan for one run."""

    run_started_at_utc: datetime
    retention_cutoff_utc: datetime
    entries: tuple[ManifestEntry, ...]
    target_day: date | None = None
    archive_groups: tuple[ArchiveGroup, ...] = ()
    skipped_objects: tuple[SkippedObject, ...] = ()


def build_archive_manifest(
    source: SourceLister,
    *,
    run_started_at_utc: datetime,
    retention_days: int,
    versioning_state: VersioningState,
    source_filter: SourcePathFilter,
) -> ArchiveManifest:
    """Build a daily archive manifest from source object keys in the retention window."""

    run_started = _as_utc(run_started_at_utc)
    target_day = run_started.date() - timedelta(days=retention_days)
    cutoff = datetime.combine(target_day, time.min, UTC)
    entries: list[ManifestEntry] = []
    skipped: list[SkippedObject] = []
    for listed in source.list_source_objects(versioning_state):
        if not source_filter.includes(listed.key):
            continue
        selected = select_key_timestamp(listed.key, listed.last_modified)
        if selected is None:
            skipped.append(SkippedObject(listed.key, "no reliable key timestamp"))
            continue
        timestamp, timestamp_source = selected
        object_day = timestamp.date()
        if object_day > target_day:
            skipped.append(SkippedObject(listed.key, "outside retention window"))
            continue
        entries.append(_entry(source.bucket, listed, timestamp, timestamp_source, object_day))
    grouped = _archive_groups(tuple(entries))
    return ArchiveManifest(run_started, cutoff, tuple(entries), target_day, grouped, tuple(skipped))


def _entry(
    source_bucket: str,
    listed: S3ListedObject,
    selected_timestamp: datetime,
    timestamp_source: TimestampSource,
    target_day: date,
) -> ManifestEntry:
    root = archive_root_for_key(listed.key)
    destination_key = destination_archive_key(root, target_day)
    return ManifestEntry(
        source_bucket,
        listed.key,
        listed.size,
        listed.last_modified,
        listed.etag,
        listed.version_id,
        listed,
        selected_timestamp,
        timestamp_source,
        target_day,
        root,
        destination_key,
    )


def _archive_groups(entries: tuple[ManifestEntry, ...]) -> tuple[ArchiveGroup, ...]:
    group_keys = sorted(
        {
            (entry.archive_root, entry.target_day)
            for entry in entries
            if entry.target_day is not None
        }
    )
    groups: list[ArchiveGroup] = []
    for root, target_day in group_keys:
        grouped = tuple(
            sorted(_group_entries(entries, root, target_day), key=lambda item: item.key)
        )
        groups.append(
            ArchiveGroup(target_day, root, destination_archive_key(root, target_day), grouped)
        )
    return tuple(groups)


def _group_entries(
    entries: tuple[ManifestEntry, ...], root: str, target_day: date
) -> Iterable[ManifestEntry]:
    return (
        entry for entry in entries if entry.archive_root == root and entry.target_day == target_day
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
