"""Archive staging temp-directory tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, override

import pytest
from s3_archiver_core._archive_copy import copy_group
from s3_archiver_core.archive_manifest import (
    ArchiveGroup,
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

    failure, verified = copy_group(source, destination, group, None)

    assert failure is None
    assert verified is True
    assert destination.upload_parent == temp_dir
    assert list(temp_dir.iterdir()) == []


@pytest.mark.unit()
def test_copy_group_rejects_archive_that_cannot_fit_in_temp_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, group = _source_and_group()
    temp_dir = tmp_path / "runtime-temp"
    destination = RecordingUploadBucket("destination", temp_dir=temp_dir)
    monkeypatch.setattr(
        "s3_archiver_core.temp_files.shutil.disk_usage",
        _disk_usage(total=100, used=90, free=10),
    )

    failure, verified = copy_group(source, destination, group, None)

    assert failure is not None
    assert "archive_group_staging requires" in failure
    assert "source_key=data/fae/2026-04-13T00-00-00Z.txt" in failure
    assert verified is False
    assert destination.upload_parent is None
    assert not temp_dir.exists() or list(temp_dir.iterdir()) == []


@pytest.mark.unit()
def test_copy_group_reports_empty_source_key_for_empty_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_dir = tmp_path / "runtime-temp"
    destination = RecordingUploadBucket("destination", temp_dir=temp_dir)
    group = ArchiveGroup(STARTED.date(), "", "empty.tar.gz", ())
    monkeypatch.setattr(
        "s3_archiver_core.temp_files.shutil.disk_usage",
        _disk_usage(total=100, used=90, free=10),
    )

    failure, verified = copy_group(FakeBucket("source"), destination, group, None)

    assert failure is not None
    assert "source_key=<empty archive group>" in failure
    assert verified is False


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
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
    )
    return source, manifest.archive_groups[0]


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _disk_usage(*, total: int, used: int, free: int) -> object:
    def disk_usage(_path: object) -> _DiskUsage:
        return _DiskUsage(total=total, used=used, free=free)

    return disk_usage
