"""S3 archive temp storage guard tests."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest
from s3_archiver_core.archive_s3 import S3ArchiveBucket

from tests.unit.archive_s3_fakes import FakeArchiveClient, copy_object, properties


@pytest.mark.unit()
def test_s3_archive_bucket_temp_file_transfer_rejects_object_that_cannot_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_client = FakeArchiveClient()
    destination_client = FakeArchiveClient()
    source = S3ArchiveBucket(source_client, "source", tmp_path)
    bucket = S3ArchiveBucket(destination_client, "destination", tmp_path)
    monkeypatch.setattr(
        "s3_archiver_core.temp_files.shutil.disk_usage",
        _disk_usage(total=100, used=90, free=10),
    )

    with pytest.raises(RuntimeError, match=r"source_key=large\.bin"):
        copy_object(bucket, properties(11), "temp_file_backed", source)

    assert source_client.get_call == {}
    assert list(tmp_path.iterdir()) == []


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _disk_usage(*, total: int, used: int, free: int) -> object:
    def disk_usage(_path: object) -> _DiskUsage:
        return _DiskUsage(total=total, used=used, free=free)

    return disk_usage
