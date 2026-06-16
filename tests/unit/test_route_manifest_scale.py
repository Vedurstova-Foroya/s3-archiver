"""Scale/load coverage for the always-sqlite route manifest builder.

Exercises the streaming insert path at a realistic object count without ever
materializing the full listing or the resulting manifest sequences in memory.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from s3_archiver_core.archive_manifest import (
    ArchiveManifest,
    ArchiveManifestRoute,
    build_route_archive_manifest,
)
from s3_archiver_core.s3 import S3ListedObject, VersioningState

from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)
_BASE_DAY = datetime(2026, 3, 1, tzinfo=UTC)
_GROUP_DAYS = 5
_VALID_OBJECT_COUNT = 48_000
_OVERSIZED_OBJECT_COUNT = 2_000
_OVERSIZED_SIZE = 2 * 1024 * 1024
_MAX_SOURCE_SIZE_MIB = "1"


class _GeneratorSource:
    """Source lister that streams synthetic objects from a generator."""

    bucket: str = "scale-source"
    temp_dir: Path

    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = temp_dir

    def versioning_state(self) -> VersioningState:
        return "Enabled"

    def list_source_objects(
        self, versioning_state: VersioningState, *, prefix: str = ""
    ) -> Iterator[S3ListedObject]:
        assert versioning_state == "Enabled"
        _ = prefix
        yield from _iter_valid_objects()
        yield from _iter_oversized_objects()


def _iter_valid_objects() -> Iterator[S3ListedObject]:
    for index in range(_VALID_OBJECT_COUNT):
        day = 1 + (index % _GROUP_DAYS)
        offset = index // _GROUP_DAYS
        hour, minute, second = offset // 3600, (offset // 60) % 60, offset % 60
        stamp = f"2026-03-{day:02d}T{hour:02d}-{minute:02d}-{second:02d}Z"
        key = f"data/fae/2026-03-{day:02d}/{stamp}.xml"
        yield _listed(key, version_id=f"v{index}", size=10)


def _iter_oversized_objects() -> Iterator[S3ListedObject]:
    for index in range(_OVERSIZED_OBJECT_COUNT):
        key = f"data/fae/big/oversized-{index}.bin"
        yield _listed(key, version_id=f"big{index}", size=_OVERSIZED_SIZE)


def _listed(key: str, *, version_id: str, size: int) -> S3ListedObject:
    last_modified = _BASE_DAY - timedelta(days=1)
    return S3ListedObject(
        key=key,
        size=size,
        last_modified=last_modified,
        etag='"etag"',
        version_id=version_id,
        properties=_properties(size=size, last_modified=last_modified),
    )


@pytest.mark.unit()
@pytest.mark.slow()
def test_route_manifest_streams_large_listing_into_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVER_MAX_SOURCE_OBJECT_SIZE_MIB", _MAX_SOURCE_SIZE_MIB)
    source = _GeneratorSource(tmp_path)

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "fae",
                source,
                _DestinationStub(),
                source_path="data/fae/",
                destination_path="archives/fae/",
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
            ),
        ),
        run_started_at_utc=STARTED,
        temp_dir=tmp_path,
    )
    try:
        assert manifest.manifest_storage == "sqlite"
        assert manifest.store is not None
        assert len(manifest.entries) == _VALID_OBJECT_COUNT
        assert len(manifest.skipped_objects) == _OVERSIZED_OBJECT_COUNT
        assert len(manifest.archive_groups) == _GROUP_DAYS
        assert _streamed_entry_count(manifest) == _VALID_OBJECT_COUNT
        assert _streamed_group_object_count(manifest) == _VALID_OBJECT_COUNT
        assert _streamed_skipped_count(manifest) == _OVERSIZED_OBJECT_COUNT
    finally:
        manifest.close()


def _streamed_entry_count(manifest: ArchiveManifest) -> int:
    return sum(1 for _ in manifest.entries)


def _streamed_skipped_count(manifest: ArchiveManifest) -> int:
    return sum(1 for _ in manifest.skipped_objects)


def _streamed_group_object_count(manifest: ArchiveManifest) -> int:
    total = 0
    for group in manifest.archive_groups:
        total += sum(1 for _ in group.entries)
    return total


class _DestinationStub:
    bucket: str = "scale-archive"
