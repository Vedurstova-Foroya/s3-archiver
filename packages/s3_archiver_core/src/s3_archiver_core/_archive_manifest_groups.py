from __future__ import annotations

from dataclasses import replace
from typing import final

from s3_archiver_core._archive_env import positive_int_env
from s3_archiver_core._archive_manifest_models import ManifestEntry

DEFAULT_ARCHIVE_GROUP_MAX_BYTES = 100 * 1024 * 1024 * 1024
DEFAULT_ARCHIVE_GROUP_MAX_OBJECTS = 2_000_000


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
