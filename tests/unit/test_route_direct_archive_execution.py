"""Direct-copy route archive execution tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import override

import pytest
from s3_archiver_core.archive import ArchiveRoute, run_archive
from s3_archiver_core.archive_transfer import (
    FINGERPRINT_METADATA_KEY,
    fingerprint_from_metadata,
    verify_destination,
    verify_destination_checksum,
)

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


class CorruptAfterCopyVerificationBucket(FakeBucket):
    _destination_hash_count: int

    def __init__(self, bucket: str) -> None:
        super().__init__(bucket)
        self._destination_hash_count = 0

    @override
    def content_sha256(self, key: str, version_id: str | None = None) -> str | None:
        digest = super().content_sha256(key, version_id)
        if key == "data/raw.txt" and version_id is None:
            self._destination_hash_count += 1
            if self._destination_hash_count == 1:
                self._destination_payloads[key] = b"changed after copy verification"
        return digest


@pytest.mark.unit()
def test_run_archive_direct_copy_mode_copies_and_verifies() -> None:
    listed = _listed("data/raw.txt", 1, "v1")
    source = FakeBucket("source", (listed,))
    destination = FakeBucket("archive")

    result = run_archive(
        _direct_routes(source, destination, destination_path="mirror/"),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    assert destination.copied == ["data/raw.txt"]
    assert destination.head_object("mirror/data/raw.txt") is not None
    assert destination.head_object("data/raw.txt") is None


@pytest.mark.unit()
def test_run_archive_direct_copy_mode_rehashes_content_without_checksums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHIVER_DIRECT_CONTENT_VERIFY", "true")
    listed = _listed("data/raw.txt", 1, "v1")
    source = FakeBucket("source", (listed,))
    destination = CorruptAfterCopyVerificationBucket("archive")

    result = run_archive(
        _direct_routes(source, destination),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.copy.ok is True
    assert result.verify.ok is False
    assert result.verify.failures == ("data/raw.txt: content mismatch",)
    assert source.content_sha256_calls == [("data/raw.txt", "v1"), ("data/raw.txt", "v1")]
    assert destination.content_sha256_calls == [("data/raw.txt", None), ("data/raw.txt", None)]


@pytest.mark.unit()
def test_run_archive_direct_copy_mode_skips_future_last_modified_before_copy() -> None:
    future = datetime(2026, 4, 28, tzinfo=UTC)
    listed = replace(
        _listed("data/future.txt", 1, "v1"),
        last_modified=future,
        properties=_properties(last_modified=future),
    )
    source = FakeBucket("source", (listed,))
    destination = FakeBucket("archive")

    result = run_archive(
        _direct_routes(source, destination),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    assert result.manifest.entries == ()
    assert [(item.key, item.reason) for item in result.manifest.skipped_objects] == [
        ("data/future.txt", "parser timestamp in incomplete UTC day")
    ]
    assert destination.copied == []
    assert destination.uploaded == []
    assert destination.head_object("data/future.txt") is None
    assert source.content_sha256_calls == []
    assert destination.content_sha256_calls == []
    assert result.copy.failures == ()
    assert result.copy.skipped is False
    assert result.verify.failures == ()
    assert result.verify.skipped is False


@pytest.mark.unit()
def test_direct_copy_preserves_object_properties_for_manifest_and_verification() -> None:
    last_modified = datetime(2026, 4, 26, 9, 30, tzinfo=UTC)
    properties = _properties(
        size=17,
        metadata={"owner": "archive", "dataset": "raw"},
        tags={"kind": "source", "route": "direct"},
        last_modified=last_modified,
        checksums={"sha256": "payload-sha256", "crc32c": "payload-crc32c"},
        checksum_type="FULL_OBJECT",
    )
    listed = replace(
        _listed("data/raw.txt", 1, "v-direct"),
        size=properties.size,
        last_modified=last_modified,
        etag=properties.etag,
        properties=properties,
    )
    source = FakeBucket("source", (listed,), payloads={"data/raw.txt": b"direct test body"})
    destination = FakeBucket("archive")

    result = run_archive(
        _direct_routes(source, destination, destination_path="mirror/"),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    [entry] = result.manifest.entries
    assert entry.object.properties == properties
    assert entry.version_id == "v-direct"
    assert entry.selected_timestamp == last_modified
    assert entry.timestamp_source == "last_modified"

    destination_properties = destination.head_object("mirror/data/raw.txt")
    assert destination_properties is not None
    assert destination_properties.tags == properties.tags
    assert destination_properties.content_type == properties.content_type
    assert destination_properties.content_encoding == properties.content_encoding
    assert destination_properties.content_language == properties.content_language
    assert destination_properties.content_disposition == properties.content_disposition
    assert destination_properties.cache_control == properties.cache_control
    assert destination_properties.expires == properties.expires
    assert destination_properties.checksums == properties.checksums
    assert destination_properties.checksum_type == properties.checksum_type
    assert verify_destination(entry, destination_properties).ok is True
    checksum_result = verify_destination_checksum(properties, destination_properties)
    assert checksum_result is not None
    assert checksum_result.ok is True

    fingerprint = fingerprint_from_metadata(destination_properties.metadata)
    assert fingerprint is not None
    assert fingerprint.source_version_id == "v-direct"
    assert fingerprint.source_checksums == properties.checksums
    assert fingerprint.source_checksum_type == "FULL_OBJECT"
    assert {
        key: value
        for key, value in destination_properties.metadata.items()
        if key != FINGERPRINT_METADATA_KEY
    } == properties.metadata


def _direct_routes(
    source: FakeBucket, destination: FakeBucket, *, destination_path: str = ""
) -> tuple[ArchiveRoute, ...]:
    return archive_routes(
        source,
        destination,
        destination_path=destination_path,
        parser_kind="direct",
        copy_mode="direct",
    )
