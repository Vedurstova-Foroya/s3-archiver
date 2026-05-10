"""Archive staging temp-directory tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override

import pytest
from s3_archiver_core import archive as archive_module
from s3_archiver_core.archive_manifest import (
    ArchiveGroup,
    SourcePathFilter,
    build_archive_manifest,
)

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


@pytest.mark.unit()
def test_copy_group_stages_archive_in_destination_temp_dir(tmp_path: Path) -> None:
    source, group = _source_and_group()
    temp_dir = tmp_path / "runtime-temp"
    destination = RecordingUploadBucket("destination", temp_dir=temp_dir)
    copy_group = _copy_group_func()

    failure, verified = copy_group(source, destination, group, None)

    assert failure is None
    assert verified is True
    assert destination.upload_parent == temp_dir
    assert list(temp_dir.iterdir()) == []


class RecordingUploadBucket(FakeBucket):
    upload_parent: Path | None

    def __init__(self, bucket: str, *, temp_dir: Path) -> None:
        super().__init__(bucket, temp_dir=temp_dir)
        self.upload_parent = None

    @override
    def upload_archive_file(
        self, destination_key: str, archive_path: Path, metadata: Mapping[str, str]
    ) -> None:
        self.upload_parent = archive_path.parent
        super().upload_archive_file(destination_key, archive_path, metadata)


def _source_and_group() -> tuple[FakeBucket, ArchiveGroup]:
    listed = _listed("data/fae/2026-04-13T00-00-00Z.txt", 1)
    source = FakeBucket("source", (listed,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )
    return source, manifest.archive_groups[0]


def _copy_group_func() -> Callable[
    [FakeBucket, RecordingUploadBucket, ArchiveGroup, object | None],
    tuple[str | None, bool],
]:
    return cast(
        Callable[
            [FakeBucket, RecordingUploadBucket, ArchiveGroup, object | None],
            tuple[str | None, bool],
        ],
        _private_attr(archive_module, "_copy_group"),
    )


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))
