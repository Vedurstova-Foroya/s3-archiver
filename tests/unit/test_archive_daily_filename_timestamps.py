from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from s3_archiver_core.archive import (
    ARCHIVE_SHA256_METADATA_KEY,
    MANIFEST_SHA256_METADATA_KEY,
    group_metadata,
    run_archive,
)
from s3_archiver_core.archive_manifest import (
    SourcePathFilter,
    archive_root_for_key,
    build_archive_manifest,
    select_key_timestamp,
)
from s3_archiver_core.archive_options import ArchiveOptions

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)
TARGET_DAY = datetime(2026, 4, 13, tzinfo=UTC).date()


@pytest.mark.unit()
def test_timestamp_selection_prefers_basename_and_uses_path_fallback() -> None:
    basename = select_key_timestamp(
        "data/2026/04/12/2026-04-13T07-00-00Z.xml",
        datetime(2026, 4, 12, 7, tzinfo=UTC),
    )
    path_only = select_key_timestamp("data/fae/2026/04/13/07/no-stamp.xml")
    tied = select_key_timestamp(
        "a/2026/04/13/file_2026-04-13T010000Z_2026-04-13T020000Z.txt",
        datetime(2026, 4, 13, 2, 1, tzinfo=UTC),
    )

    assert basename == (datetime(2026, 4, 13, 7, tzinfo=UTC), "basename")
    assert path_only == (datetime(2026, 4, 13, 7, tzinfo=UTC), "path")
    assert tied == (datetime(2026, 4, 13, 2, tzinfo=UTC), "basename")
    assert select_key_timestamp("data/no-stamp.txt") is None


@pytest.mark.unit()
def test_malformed_filename_time_is_not_used_as_date_only_fallback() -> None:
    assert select_key_timestamp("data/fae/2026-04-13T99-00-00.xml") is None
    assert select_key_timestamp("data/fae/2026/04/13/2026-04-13T99-00-00.xml") is None

    source = FakeBucket(
        "source",
        (
            _listed("data/fae/2026-04-13T99-00-00.xml", 1),
            _listed("data/fae/2026-04-13T00-00-00Z.xml", 1),
        ),
    )

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )

    assert [(skip.key, skip.reason) for skip in manifest.skipped_objects] == [
        ("data/fae/2026-04-13T99-00-00.xml", "no reliable key timestamp")
    ]


@pytest.mark.unit()
def test_filename_timestamp_offset_is_converted_to_utc_target_day() -> None:
    selected = select_key_timestamp("data/fae/2026-04-14T00:30:00+01:00.xml")

    assert selected == (datetime(2026, 4, 13, 23, 30, tzinfo=UTC), "basename")

    source = FakeBucket(
        "source",
        (
            _listed("data/fae/2026-04-14T00:30:00+01:00.xml", 1),
            _listed("data/fae/2026-04-14T00:30:00+0100.xml", 1),
        ),
    )

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )

    assert manifest.target_day == TARGET_DAY
    assert [entry.key for entry in manifest.entries] == [
        "data/fae/2026-04-14T00:30:00+01:00.xml",
        "data/fae/2026-04-14T00:30:00+0100.xml",
    ]


@pytest.mark.unit()
def test_filename_timestamp_offset_must_be_valid() -> None:
    assert select_key_timestamp("data/fae/2026-04-13T12:00:00+99:99.xml") is None
    assert select_key_timestamp("data/fae/2026-04-13T12:00:00+2400.xml") is None
    assert select_key_timestamp("data/fae/2026-04-13T12:00:00+.xml") is None
    assert select_key_timestamp("data/fae/2026-04-13T12:00:00-02:30.xml") == (
        datetime(2026, 4, 13, 14, 30, tzinfo=UTC),
        "basename",
    )


@pytest.mark.unit()
def test_manifest_selects_retained_utc_days_and_records_skips() -> None:
    source = FakeBucket(
        "source",
        (
            _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1),
            _listed("data/fae/2026/04/12/23/2026-04-12T23-59-59.xml", 1),
            _listed("data/fae/2026/04/14/00/2026-04-14T00-00-00.xml", 1),
            _listed("data/fae/no-stamp.xml", 1),
        ),
    )

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )

    assert manifest.target_day == TARGET_DAY
    assert [entry.key for entry in manifest.entries] == [
        "data/fae/2026/04/13/07/2026-04-13T07-00-00.xml",
        "data/fae/2026/04/12/23/2026-04-12T23-59-59.xml",
    ]
    assert [(skip.key, skip.reason) for skip in manifest.skipped_objects] == [
        ("data/fae/2026/04/14/00/2026-04-14T00-00-00.xml", "outside retention window"),
        ("data/fae/no-stamp.xml", "no reliable key timestamp"),
    ]


@pytest.mark.unit()
def test_invalid_date_like_timestamp_is_skipped_instead_of_failing_manifest() -> None:
    source = FakeBucket(
        "source",
        (
            _listed("data/fae/2026-02-31T00-00-00Z.xml", 1),
            _listed("data/fae/2026/02/31/file.xml", 1),
            _listed("data/fae/2026-04-13T00-00-00Z.xml", 1),
        ),
    )

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )

    assert [entry.key for entry in manifest.entries] == ["data/fae/2026-04-13T00-00-00Z.xml"]
    assert [(skip.key, skip.reason) for skip in manifest.skipped_objects] == [
        ("data/fae/2026-02-31T00-00-00Z.xml", "no reliable key timestamp"),
        ("data/fae/2026/02/31/file.xml", "no reliable key timestamp"),
    ]


@pytest.mark.unit()
def test_archive_root_flattening_examples() -> None:
    assert archive_root_for_key("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml") == "data/fae"
    assert (
        archive_root_for_key(
            "data/harmonie/HARMONIE_DINI_SF_2026-04-24T000000Z_2026-04-24T000000Z.bz2"
        )
        == "data/harmonie"
    )


@pytest.mark.unit()
def test_run_archive_uploads_deterministic_tar_with_manifest_metadata() -> None:
    first = _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1)
    second = _listed("data/fae/2026/04/13/08/2026-04-13T08-00-00.xml", 1)
    source = FakeBucket(
        "source",
        (second, first),
        payloads={first.key: b"first-0000", second.key: b"second-000"},
    )
    destination = FakeBucket("destination")

    result = run_archive(
        source,
        destination,
        ArchiveOptions(retention_days=14, max_workers=1),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    archive_key = "data/fae/2026-04-13.tar.gz"
    assert result.ok is True
    assert destination.uploaded == [archive_key]
    head = destination.head_object(archive_key)
    assert head is not None
    metadata = head.metadata
    payload = destination.destination_payload(archive_key)
    assert metadata[ARCHIVE_SHA256_METADATA_KEY] == hashlib.sha256(payload).hexdigest()
    assert (
        metadata[MANIFEST_SHA256_METADATA_KEY]
        == group_metadata(result.manifest.archive_groups[0])[MANIFEST_SHA256_METADATA_KEY]
    )
    with (
        gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="r:") as tar,
    ):
        members = tar.getmembers()
        names = [member.name for member in members]
        mtimes = [member.mtime for member in members]
    assert names == [first.key, second.key]
    assert mtimes == [0, 0]


@pytest.mark.unit()
def test_existing_archive_matching_manifest_allows_cleanup_and_mismatch_blocks_it() -> None:
    listed = _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1, "v1")
    source = FakeBucket("source", (listed,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )
    archive_key = manifest.archive_groups[0].destination_archive_key
    existing_payload = b"archive"
    existing_metadata = dict(group_metadata(manifest.archive_groups[0])) | {
        ARCHIVE_SHA256_METADATA_KEY: hashlib.sha256(existing_payload).hexdigest()
    }
    matching = FakeBucket(
        "destination",
        destination={archive_key: _properties(metadata=existing_metadata)},
        payloads={archive_key: existing_payload},
    )

    result = run_archive(
        source,
        matching,
        ArchiveOptions(retention_days=14, cleanup_enabled=True, max_workers=1),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    assert matching.uploaded == []
    assert source.deleted == [(listed.key, "v1")]

    source.deleted.clear()
    mismatched = FakeBucket(
        "destination",
        destination={
            archive_key: _properties(metadata={MANIFEST_SHA256_METADATA_KEY: "different"})
        },
    )
    failed = run_archive(
        source,
        mismatched,
        ArchiveOptions(retention_days=14, cleanup_enabled=True, max_workers=1),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert failed.ok is True
    assert failed.skipped_archive_keys == (archive_key,)
    assert failed.cleanup.skipped is False
    assert source.deleted == []


@pytest.mark.unit()
def test_cleanup_deletes_manifest_versions_without_source_last_modified_recheck() -> None:
    listed = replace(
        _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1, None),
        properties=_properties(last_modified=datetime(2026, 4, 14, tzinfo=UTC)),
    )
    source = FakeBucket("source", (listed,))
    destination = FakeBucket("destination")

    result = run_archive(
        source,
        destination,
        ArchiveOptions(retention_days=14, cleanup_enabled=True, max_workers=1),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    assert source.deleted == [(listed.key, None)]
