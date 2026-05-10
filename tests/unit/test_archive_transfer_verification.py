"""Archive transfer verification helper tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from s3_archiver_core.archive_manifest import ManifestEntry
from s3_archiver_core.archive_transfer import (
    FINGERPRINT_METADATA_KEY,
    archive_metadata,
    fingerprint_from_metadata,
    verify_destination,
    verify_source_unchanged,
)

from tests.unit.archive_workflow_fakes import listed_object as _listed


def _entry(key: str = "old.txt") -> ManifestEntry:
    listed = _listed(key, 90, None)
    return ManifestEntry("source", key, 10, listed.last_modified, '"etag"', None, listed)


@pytest.mark.unit()
def test_verify_destination_reports_each_mismatch_detail() -> None:
    entry = _entry()
    destination = replace(entry.object.properties, metadata=archive_metadata(entry))
    assert verify_destination(entry, None).detail == "destination missing"
    assert (
        verify_destination(entry, replace(destination, content_type="application/json")).detail
        == "object property mismatch"
    )
    assert (
        verify_destination(
            entry,
            replace(destination, metadata=dict(destination.metadata) | {"owner": "other"}),
        ).detail
        == "metadata mismatch"
    )
    assert (
        verify_destination(entry, replace(destination, tags={"kind": "other"})).detail
        == "tag mismatch"
    )


@pytest.mark.unit()
def test_verify_source_unchanged_reports_missing_and_property_changes() -> None:
    entry = _entry()
    current = entry.object.properties
    assert verify_source_unchanged(entry, None).detail == "source missing before cleanup"
    assert verify_source_unchanged(entry, replace(current, size=11)).ok is False
    assert verify_source_unchanged(entry, replace(current, content_type=None)).ok is False
    assert verify_source_unchanged(entry, replace(current, metadata={"owner": "other"})).ok is False
    assert verify_source_unchanged(entry, replace(current, tags={"kind": "other"})).ok is False
    assert verify_source_unchanged(entry, current).ok is True


@pytest.mark.unit()
def test_fingerprint_metadata_rejects_invalid_shapes() -> None:
    metadata_key = FINGERPRINT_METADATA_KEY
    assert fingerprint_from_metadata({metadata_key: "not-json"}) is None
    assert fingerprint_from_metadata({metadata_key: "[]"}) is None
    assert fingerprint_from_metadata({metadata_key: '{"source_bucket": 3}'}) is None
    assert (
        fingerprint_from_metadata(
            {
                metadata_key: (
                    '{"source_bucket":"bucket","source_key":"key","source_size":1,'
                    '"source_last_modified":"time","source_version_id":3,"source_etag":4}'
                )
            }
        )
        is not None
    )


@pytest.mark.unit()
def test_verify_source_unchanged_rejects_last_modified_changes() -> None:
    entry = _entry()
    current = replace(entry.object.properties, last_modified=datetime(2024, 1, 22, tzinfo=UTC))
    assert verify_source_unchanged(entry, current).detail == "source changed before cleanup"
