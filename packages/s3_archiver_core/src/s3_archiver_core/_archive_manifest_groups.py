from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import date
from typing import final

from s3_archiver_core._archive_env import positive_int_env
from s3_archiver_core._archive_identity import stable_identity_value
from s3_archiver_core._archive_manifest_models import ArchiveGroup, CopyMode, ManifestEntry

DEFAULT_ARCHIVE_GROUP_MAX_BYTES = 100 * 1024 * 1024 * 1024
DEFAULT_ARCHIVE_GROUP_MAX_OBJECTS = 2_000_000

_ARCHIVE_COPY_MODES = frozenset({"daily_tar_gz", "timestamp_child_tar_gz"})

type _ArchiveGroupKey = tuple[str, CopyMode, str, date, str, str, str]


def archive_groups(entries: Iterable[ManifestEntry]) -> tuple[ArchiveGroup, ...]:
    """Group daily tar entries by route, root, target day, key, and destination."""

    grouped_entries: dict[_ArchiveGroupKey, list[ManifestEntry]] = {}
    for entry in entries:
        if entry.copy_mode not in _ARCHIVE_COPY_MODES or entry.target_day is None:
            continue
        key = (
            entry.route_name,
            entry.copy_mode,
            entry.archive_root,
            entry.target_day,
            entry.destination_bucket,
            entry.destination_archive_key,
            _stable_sort_value(entry.destination_identity),
        )
        grouped_entries.setdefault(key, []).append(entry)

    groups: list[ArchiveGroup] = []
    for key in sorted(grouped_entries):
        grouped = tuple(sorted(grouped_entries[key], key=lambda item: item.key))
        for chunk_index, chunk in enumerate(archive_group_chunks(grouped), start=1):
            groups.append(archive_group_for_chunk(chunk, chunk_index))
    return tuple(groups)


def archive_group_chunks(entries: Iterable[ManifestEntry]) -> Iterator[tuple[ManifestEntry, ...]]:
    """Yield bounded archive member chunks from already-sorted manifest entries."""

    entry_tuple = tuple(entries)
    for _chunk_index, offset, length in archive_chunk_ranges(entry.size for entry in entry_tuple):
        yield entry_tuple[offset : offset + length]


def archive_chunk_ranges(sizes: Iterable[int]) -> Iterator[tuple[int, int, int]]:
    """Yield ``(chunk_index, offset, length)`` for bounded archive chunks."""

    chunker = ArchiveChunkSizer()
    chunk_index = 1
    offset = 0
    length = 0
    for size in sizes:
        if chunker.would_overflow(size):
            yield chunk_index, offset, length
            chunk_index += 1
            offset += length
            length = 0
            chunker.reset()
        chunker.add(size)
        length += 1
    if length:
        yield chunk_index, offset, length


def archive_group_for_chunk(
    entries: tuple[ManifestEntry, ...],
    chunk_index: int,
) -> ArchiveGroup:
    """Build one archive group from an already-bounded chunk."""

    chunk_entries = archive_chunk_entries(entries, chunk_index)
    first = chunk_entries[0]
    if first.target_day is None:  # pragma: no cover - guarded before grouping
        raise RuntimeError("daily archive group missing target day")
    return ArchiveGroup(
        target_day=first.target_day,
        archive_root=first.archive_root,
        destination_archive_key=first.destination_archive_key,
        entries=chunk_entries,
        route_name=first.route_name,
        parser_kind=first.parser_kind,
        copy_mode=first.copy_mode,
        source_bucket=first.source_bucket,
        source_identity=first.source_identity,
        destination_bucket=first.destination_bucket,
        destination_identity=first.destination_identity,
    )


def archive_chunk_entries(
    entries: tuple[ManifestEntry, ...],
    chunk_index: int,
) -> tuple[ManifestEntry, ...]:
    return tuple(archive_chunk_entry(entry, chunk_index) for entry in entries)


def archive_chunk_entry(entry: ManifestEntry, chunk_index: int) -> ManifestEntry:
    if chunk_index == 1:
        return entry
    destination_key = archive_chunk_key(entry.destination_archive_key, chunk_index)
    return replace(
        entry,
        destination_archive_key=destination_key,
        destination_key=destination_key,
    )


def archive_chunk_limits() -> tuple[int, int]:
    return (
        positive_int_env("ARCHIVER_ARCHIVE_GROUP_MAX_BYTES", DEFAULT_ARCHIVE_GROUP_MAX_BYTES),
        positive_int_env("ARCHIVER_ARCHIVE_GROUP_MAX_OBJECTS", DEFAULT_ARCHIVE_GROUP_MAX_OBJECTS),
    )


@final
class ArchiveChunkSizer:
    """Track archive chunk size limits for manifest grouping queries."""

    def __init__(self) -> None:
        self._max_bytes, self._max_objects = archive_chunk_limits()
        self._object_count = 0
        self._byte_count = 0

    @property
    def has_items(self) -> bool:
        return self._object_count > 0

    def would_overflow(self, size: int) -> bool:
        next_bytes = self._byte_count + max(size, 0)
        return self.has_items and (
            self._object_count >= self._max_objects or next_bytes > self._max_bytes
        )

    def add(self, size: int) -> None:
        self._object_count += 1
        self._byte_count += max(size, 0)

    def reset(self) -> None:
        self._object_count = 0
        self._byte_count = 0


def archive_chunk_key(key: str, chunk_index: int) -> str:
    if chunk_index == 1:
        return key
    suffix = f".part-{chunk_index:05d}.tar.gz"
    if key.endswith(".tar.gz"):
        return f"{key[:-7]}{suffix}"
    return f"{key}.part-{chunk_index:05d}"


def _stable_sort_value(value: object) -> str:
    return repr(stable_identity_value(value))
