"""Unit tests for deterministic tar.gz archive creation."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from s3_archiver_core.archive import run_archive
from s3_archiver_core.archive_manifest import ArchiveGroup, ManifestEntry
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.archive_tar import ORIGINAL_KEY_PAX_HEADER, write_tar_gz_archive

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed


@pytest.mark.unit()
@pytest.mark.parametrize(
    "key",
    [
        "",
        "data/../outside.txt",
        "/tmp/outside.txt",
        r"data\outside.txt",
        "C:/tmp/outside.txt",
        "s3-archiver-safe/source-key.txt",
    ],
)
def test_tar_archive_rewrites_unsafe_member_names_with_original_key_pax_header(
    tmp_path: Path, key: str
) -> None:
    member = _single_member_archive(tmp_path, key)
    expected_name = f"s3-archiver-safe/{hashlib.sha256(key.encode()).hexdigest()}"

    assert member.name == expected_name
    assert ".." not in member.name.split("/")
    assert member.pax_headers[ORIGINAL_KEY_PAX_HEADER] == key


@pytest.mark.unit()
def test_tar_archive_keeps_safe_member_names(tmp_path: Path) -> None:
    key = "data/safe-source-key.txt"
    member = _single_member_archive(tmp_path, key)

    assert member.name == key
    assert member.pax_headers == {}


@pytest.mark.unit()
def test_run_archive_rewrites_unsafe_daily_tar_member_name_and_preserves_original_key() -> None:
    started = datetime(2026, 4, 27, 12, tzinfo=UTC)
    key = "C:/data/fae/2026-04-13T07-00-00Z.xml"
    listed = _listed(key, 1)
    source = FakeBucket("source", (listed,), payloads={key: b"x" * listed.size})
    destination = FakeBucket("destination")

    result = run_archive(
        source,
        destination,
        ArchiveOptions(retention_days=14, max_workers=1),
        run_started_at_utc=started,
        clock=lambda: started,
    )

    assert result.ok is True
    archive_key = result.manifest.archive_groups[0].destination_archive_key
    archive_payload = destination.destination_payload(archive_key)
    with (
        gzip.GzipFile(fileobj=io.BytesIO(archive_payload), mode="rb") as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="r:") as tar,
    ):
        member = tar.getmembers()[0]

    assert member.name == f"s3-archiver-safe/{hashlib.sha256(key.encode()).hexdigest()}"
    assert member.pax_headers[ORIGINAL_KEY_PAX_HEADER] == key


def _single_member_archive(tmp_path: Path, key: str) -> tarfile.TarInfo:
    archive_path = tmp_path / "archive.tar.gz"
    listed = _listed(key, 90)
    source = FakeBucket("source", (listed,), payloads={key: b"x" * listed.size})
    group = ArchiveGroup(
        date(2024, 1, 21),
        "",
        "2024-01-21.tar.gz",
        (
            ManifestEntry(
                source.bucket,
                key,
                listed.size,
                datetime(2024, 1, 1, tzinfo=UTC),
                listed.etag,
                listed.version_id,
                listed,
            ),
        ),
    )

    write_tar_gz_archive(source, group, archive_path)

    with (
        gzip.GzipFile(fileobj=io.BytesIO(archive_path.read_bytes()), mode="rb") as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="r:") as tar,
    ):
        members = tar.getmembers()

    assert len(members) == 1
    return members[0]
