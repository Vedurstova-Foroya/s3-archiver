"""Unit tests for archive run timestamp handling."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from s3_archiver_core._archive_protocols import ArchiveReadableBody
from s3_archiver_core.archive import run_archive
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.s3 import S3ListedObject, S3ObjectProperties, VersioningState
from s3_archiver_core.temp_files import default_temp_dir


class EmptyBucket:
    bucket: str = "bucket"
    temp_dir: Path = default_temp_dir()

    def versioning_state(self) -> VersioningState:
        return "Disabled"

    def list_source_objects(self, versioning_state: VersioningState) -> tuple[S3ListedObject, ...]:
        _ = versioning_state
        return ()

    def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        _ = (key, version_id)
        return None

    def content_sha256(self, key: str, version_id: str | None = None) -> str | None:
        _ = (key, version_id)
        return None

    def read_source_bytes(self, key: str, version_id: str | None = None) -> bytes:
        raise AssertionError(f"empty manifest must not read {key!r} {version_id!r}")

    def read_source_stream(self, key: str, version_id: str | None = None) -> ArchiveReadableBody:
        raise AssertionError(f"empty manifest must not read {key!r} {version_id!r}")

    def upload_archive_file(
        self, destination_key: str, archive_path: Path, metadata: Mapping[str, str]
    ) -> None:
        _ = (archive_path, metadata)
        raise AssertionError(f"empty manifest must not upload {destination_key!r}")

    def copy_from(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("empty manifest must not copy")

    def delete_source(self, key: str, version_id: str | None) -> None:
        raise AssertionError(f"empty manifest must not delete {key!r} {version_id!r}")


@pytest.mark.unit()
def test_run_archive_uses_fresh_clock_timestamp_per_run() -> None:
    first_started = datetime(2024, 4, 20, tzinfo=UTC)
    second_started = datetime(2024, 4, 21, tzinfo=UTC)
    options = ArchiveOptions(retention_days=60, cleanup_enabled=False, max_workers=1)

    first = run_archive(
        EmptyBucket(),
        EmptyBucket(),
        options,
        clock=lambda: first_started,
    )
    second = run_archive(
        EmptyBucket(),
        EmptyBucket(),
        options,
        clock=lambda: second_started,
    )

    assert first.manifest.run_started_at_utc == first_started
    assert first.manifest.retention_cutoff_utc == first_started - timedelta(days=60)
    assert second.manifest.run_started_at_utc == second_started
    assert second.manifest.retention_cutoff_utc == second_started - timedelta(days=60)
