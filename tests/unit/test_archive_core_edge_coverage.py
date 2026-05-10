"""Focused coverage tests for archive core edge paths."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast, override

import pytest
from s3_archiver_core import archive as archive_module
from s3_archiver_core.archive_fingerprint import (
    archive_metadata,
    fingerprint_from_metadata,
    fingerprint_matches_entry,
    recover_fingerprinted_entry,
)
from s3_archiver_core.archive_manifest import (
    ArchiveGroup,
    ManifestEntry,
    SourcePathFilter,
    archive_root_for_key,
    build_archive_manifest,
    select_key_timestamp,
)
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.archive_transfer import (
    verify_destination,
    verify_destination_checksum,
    verify_destination_content,
)
from s3_archiver_core.archive_workers import run_archive_group_workers, run_archive_workers
from s3_archiver_core.s3 import S3ObjectProperties

from tests.unit.archive_s3_fakes import FakeArchiveClient
from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


@pytest.mark.unit()
def test_copy_group_reports_missing_and_unverified_uploaded_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, group = _source_and_group()
    copy_group = _copy_group_func()

    def fail_named_tempfile(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("temp unavailable")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fail_named_tempfile)
    failure, verified = copy_group(source, FakeBucket("destination"), group, None)
    assert failure == "data/fae/2026-04-13.tar.gz: temp unavailable"
    assert verified is False
    monkeypatch.undo()

    failure, verified = copy_group(source, MissingAfterUploadBucket("destination"), group, None)
    assert failure == "data/fae/2026-04-13.tar.gz: destination missing"
    assert verified is False

    failure, verified = copy_group(source, BadArchiveHashBucket("destination"), group, None)
    assert failure == "data/fae/2026-04-13.tar.gz: archive verification failed"
    assert verified is False


@pytest.mark.unit()
def test_fingerprint_recovery_covers_absent_success_and_property_mismatch_paths() -> None:
    versioned = _entry(version_id="v1")
    versioned_destination = replace(
        versioned.object.properties,
        metadata=archive_metadata(versioned),
    )

    assert (
        recover_fingerprinted_entry(versioned, replace(versioned_destination, metadata={}), _props)
        is None
    )
    recovered = recover_fingerprinted_entry(versioned, versioned_destination, _props)
    assert recovered is not None
    assert recovered.version_id == "v1"

    assert (
        recover_fingerprinted_entry(
            versioned,
            versioned_destination,
            lambda _version_id: _properties(
                last_modified=versioned.last_modified.replace(hour=versioned.last_modified.hour + 1)
            ),
        )
        is None
    )
    checksum_typed = _entry_with_properties(
        checksums={"sha256": "same"},
        checksum_type="FULL_OBJECT",
    )
    checksum_destination = replace(
        checksum_typed.object.properties,
        metadata=archive_metadata(checksum_typed),
    )
    assert (
        recover_fingerprinted_entry(
            checksum_typed,
            checksum_destination,
            lambda _version_id: _properties(
                checksums={"sha256": "same"},
                checksum_type="COMPOSITE",
            ),
        )
        is None
    )
    fingerprint = fingerprint_from_metadata(archive_metadata(versioned))
    assert fingerprint is not None
    assert fingerprint_matches_entry(fingerprint, _entry(key="other.txt")) is False

    unversioned = _entry(version_id=None)
    unversioned_destination = replace(
        unversioned.object.properties,
        metadata=archive_metadata(unversioned),
    )
    assert (
        recover_fingerprinted_entry(
            unversioned,
            unversioned_destination,
            lambda _version_id: _properties(size=11),
            require_current_source_match=True,
        )
        is None
    )
    assert (
        recover_fingerprinted_entry(unversioned, unversioned_destination, lambda _version_id: None)
        is not None
    )


@pytest.mark.unit()
def test_transfer_verification_covers_fingerprint_content_and_checksum_edges() -> None:
    entry = _entry(version_id=None)
    destination = replace(entry.object.properties, metadata=archive_metadata(entry))

    assert verify_destination(entry, replace(destination, metadata={})).detail == (
        "source fingerprint mismatch"
    )
    assert verify_destination(_entry(key="other.txt", version_id=None), destination).detail == (
        "source fingerprint mismatch"
    )
    assert verify_destination_content("source", "destination").detail == "content mismatch"
    assert (
        verify_destination_checksum(
            _properties(checksums={"sha256": "same"}, checksum_type="COMPOSITE"),
            _properties(checksums={"sha256": "same"}, checksum_type="FULL_OBJECT"),
        )
        is None
    )

    matching = verify_destination_checksum(
        _properties(checksums={"sha256": "same", "crc32": "also"}, checksum_type="FULL_OBJECT"),
        _properties(checksums={"sha256": "same", "crc32": "also"}, checksum_type="FULL_OBJECT"),
    )
    mismatched_second = verify_destination_checksum(
        _properties(checksums={"sha256": "same", "crc32": "expected"}, checksum_type="FULL_OBJECT"),
        _properties(checksums={"sha256": "same", "crc32": "other"}, checksum_type="FULL_OBJECT"),
    )

    assert matching is not None
    assert matching.ok is True
    assert mismatched_second is not None
    assert mismatched_second.detail == "content mismatch"


@pytest.mark.unit()
def test_timestamp_parser_covers_path_stripping_and_time_separator_edges() -> None:
    assert select_key_timestamp("data/20260413120000Z") == (
        datetime(2026, 4, 13, 12, tzinfo=UTC),
        "basename",
    )
    assert select_key_timestamp("data/2026-04-13-120000Z") == (
        datetime(2026, 4, 13, 12, tzinfo=UTC),
        "basename",
    )
    assert select_key_timestamp("data/2026-04-13T120000Z+.txt") is None
    assert select_key_timestamp(
        "data/2026-04-13T00-00-00Z.txt",
        datetime(2026, 4, 13),
    ) == (datetime(2026, 4, 13, tzinfo=UTC), "basename")
    assert archive_root_for_key("data/fae/2026-04-13/file.txt") == "data/fae"
    assert archive_root_for_key("file.txt") == ""


@pytest.mark.unit()
def test_s3_archive_bucket_read_source_bytes_reads_and_closes_stream() -> None:
    client = FakeArchiveClient()
    client.source_body = b"abc"

    assert S3ArchiveBucket(client, "source").read_source_bytes("key", "v1") == b"abc"
    assert client.get_call == {"Bucket": "source", "Key": "key", "VersionId": "v1"}


@pytest.mark.unit()
def test_archive_workers_report_group_and_entry_worker_exceptions() -> None:
    _source, group = _source_and_group()

    group_failures = run_archive_group_workers(
        (group,),
        1,
        lambda _group: _raise("group boom"),
        lambda: False,
        lambda: 1.0,
    )
    entry_failures = run_archive_workers(
        group.entries,
        1,
        lambda _entry: _raise("entry boom"),
        lambda: False,
        lambda: 1.0,
    )

    assert group_failures == ("data/fae/2026-04-13.tar.gz: group boom",)
    assert entry_failures == ("data/fae/2026-04-13T00-00-00Z.txt: entry boom",)


class MissingAfterUploadBucket(FakeBucket):
    @override
    def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        _ = (key, version_id)
        return None


class BadArchiveHashBucket(FakeBucket):
    @override
    def content_sha256(self, key: str, version_id: str | None = None) -> str | None:
        _ = (key, version_id)
        return "wrong"


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


def _entry(*, key: str = "old.txt", version_id: str | None = "v1") -> ManifestEntry:
    listed = _listed(key, 90, version_id)
    return ManifestEntry("source", key, 10, listed.last_modified, '"etag"', version_id, listed)


def _entry_with_properties(
    *,
    checksums: Mapping[str, str],
    checksum_type: str,
) -> ManifestEntry:
    listed = replace(
        _listed("old.txt", 90, "v1"),
        properties=_properties(checksums=checksums, checksum_type=checksum_type),
    )
    return ManifestEntry("source", "old.txt", 10, listed.last_modified, '"etag"', "v1", listed)


def _props(_version_id: str | None) -> S3ObjectProperties:
    _ = _version_id
    return _properties(checksum_type="FULL_OBJECT")


def _copy_group_func() -> Callable[
    [FakeBucket, FakeBucket, ArchiveGroup, object | None],
    tuple[str | None, bool],
]:
    return cast(
        Callable[
            [FakeBucket, FakeBucket, ArchiveGroup, object | None],
            tuple[str | None, bool],
        ],
        _private_attr(archive_module, "_copy_group"),
    )


def _raise(message: str) -> str:
    raise RuntimeError(message)


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))
