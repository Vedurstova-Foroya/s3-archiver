"""Unit tests for archive workflow primitives."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from s3_archiver_core.archive_manifest import (
    ManifestEntry,
    SourcePathFilter,
    build_archive_manifest,
)
from s3_archiver_core.archive_transfer import (
    FINGERPRINT_METADATA_KEY,
    archive_metadata,
    verify_destination,
    verify_destination_checksum,
    verify_destination_content,
    verify_source_unchanged,
)

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2024, 4, 20, tzinfo=UTC)


def _target_key(name: str, *, prefix: str = "keep") -> str:
    return f"{prefix}/2024/02/20/{name}"


@pytest.mark.unit()
def test_manifest_uses_frozen_cutoff_filters_and_preserves_versions() -> None:
    source = FakeBucket(
        "source",
        (
            _listed(_target_key("2024-02-20T00-00-00.txt"), 61, "v-target"),
            _listed("keep/2024/02/19/2024-02-19T23-59-59.txt", 60, "v-previous"),
            _listed(_target_key("2024-02-20T01-00-00.txt", prefix="skip"), 90, "v-skip"),
        ),
    )

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=60,
        versioning_state="Enabled",
        source_filter=SourcePathFilter("whitelist", ("keep/",)),
    )

    assert manifest.retention_cutoff_utc == datetime(2024, 2, 20, tzinfo=UTC)
    assert [(entry.key, entry.version_id) for entry in manifest.entries] == [
        (_target_key("2024-02-20T00-00-00.txt"), "v-target"),
        ("keep/2024/02/19/2024-02-19T23-59-59.txt", "v-previous"),
    ]


@pytest.mark.unit()
def test_transfer_fingerprint_helpers_reject_stale_destination_metadata() -> None:
    listed = _listed(_target_key("2024-02-20T00-00-00.txt"), 70)
    entry = ManifestEntry("source", listed.key, 10, listed.last_modified, '"etag"', "v1", listed)
    metadata = archive_metadata(entry)
    destination = replace(entry.object.properties, metadata=metadata)

    assert verify_destination(entry, destination).ok is True
    assert verify_destination_checksum(entry.object.properties, destination) is None
    assert verify_destination(entry, replace(destination, size=11)).detail == "size mismatch"
    assert verify_destination_content("digest", "digest").ok is True
    assert verify_destination_content(None, "digest").detail == "source missing during verification"
    assert verify_destination_content("digest", None).detail == "destination missing"
    reserved = replace(listed, properties=_properties(metadata={FINGERPRINT_METADATA_KEY: "user"}))
    reserved_entry = ManifestEntry(
        "source", listed.key, 10, listed.last_modified, None, "v1", reserved
    )
    with pytest.raises(ValueError, match="reserved key"):
        _ = archive_metadata(reserved_entry)


@pytest.mark.unit()
def test_key_only_cleanup_verification_rejects_etag_changes() -> None:
    listed = _listed(_target_key("2024-02-20T00-00-00.txt"), 70, None)
    entry = ManifestEntry("source", listed.key, 10, listed.last_modified, '"etag"', None, listed)

    result = verify_source_unchanged(
        entry,
        replace(entry.object.properties, etag='"changed"'),
    )

    assert result.ok is False
    assert result.detail == "source changed before cleanup"


@pytest.mark.unit()
def test_key_only_cleanup_verification_rejects_last_modified_changes() -> None:
    listed = _listed(_target_key("2024-02-20T00-00-00.txt"), 70, None)
    entry = ManifestEntry("source", listed.key, 10, listed.last_modified, '"etag"', None, listed)

    result = verify_source_unchanged(
        entry,
        replace(
            entry.object.properties,
            last_modified=entry.last_modified + timedelta(seconds=1),
        ),
    )

    assert result.ok is False
    assert result.detail == "source changed before cleanup"


@pytest.mark.unit()
def test_verify_destination_checksum_reports_checksum_mismatch() -> None:
    source = _properties(checksums={"sha256": "expected"}, checksum_type="FULL_OBJECT")
    destination = _properties(checksums={"sha256": "other"}, checksum_type="FULL_OBJECT")

    result = verify_destination_checksum(source, destination)

    assert result is not None
    assert result.ok is False
