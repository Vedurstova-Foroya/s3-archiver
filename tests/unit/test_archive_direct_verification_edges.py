"""Direct-copy destination verification regression tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from s3_archiver_core._archive_copy import copy_direct_entry
from s3_archiver_core.archive import ArchiveRoute
from s3_archiver_core.archive_manifest import ManifestEntry, build_archive_manifest
from s3_archiver_core.archive_transfer import archive_metadata
from s3_archiver_core.s3 import S3ListedObject

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed


@pytest.mark.unit()
def test_direct_copy_existing_destination_with_matching_metadata_refreshes_corrupt_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHIVER_DIRECT_CONTENT_VERIFY", "true")
    source, entry = _direct_manifest_objects()
    destination = FakeBucket(
        "archive",
        destination={
            entry.destination_key: replace(
                entry.object.properties, metadata=archive_metadata(entry)
            )
        },
        payloads={entry.destination_key: b"corrupt"},
    )

    failure, copied = copy_direct_entry(
        ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
        entry,
        None,
    )

    assert failure is None
    assert copied is True
    assert destination.copied == ["data/raw.txt"]
    assert destination.destination_payload(entry.destination_key) == b"payload:data/raw.txt"


@pytest.mark.unit()
def test_direct_copy_refreshes_stale_destination_for_overwritten_source() -> None:
    old_entry = _direct_manifest_objects(_listed("data/raw.txt", 2, None))[1]
    current = _listed("data/raw.txt", 1, None)
    current = replace(
        current,
        size=11,
        etag='"new-etag"',
        properties=replace(current.properties, size=11, etag='"new-etag"'),
    )
    source, entry = _direct_manifest_objects(current)
    destination = FakeBucket(
        "archive",
        destination={
            entry.destination_key: replace(
                old_entry.object.properties, metadata=archive_metadata(old_entry)
            )
        },
    )

    failure, copied = copy_direct_entry(
        ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
        entry,
        None,
    )

    refreshed = destination.head_object(entry.destination_key)
    assert failure is None
    assert copied is True
    assert destination.copied == ["data/raw.txt"]
    assert refreshed is not None
    assert refreshed.size == 11
    assert refreshed.etag == '"new-etag"'
    assert refreshed.metadata == archive_metadata(entry)


@pytest.mark.unit()
def test_direct_copy_existing_destination_skips_body_hashing_by_default() -> None:
    source, entry = _direct_manifest_objects()
    destination = FakeBucket(
        "archive",
        destination={
            entry.destination_key: replace(
                entry.object.properties, metadata=archive_metadata(entry)
            )
        },
        payloads={entry.destination_key: b"corrupt"},
    )

    failure, copied = copy_direct_entry(
        ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
        entry,
        None,
    )

    assert failure is None
    assert copied is True
    assert source.content_sha256_calls == []
    assert destination.content_sha256_calls == []


def _direct_manifest_objects(
    listed: S3ListedObject | None = None,
) -> tuple[FakeBucket, ManifestEntry]:
    source = FakeBucket("source", (_listed("data/raw.txt", 1, "v1") if listed is None else listed,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=datetime(2026, 4, 27, 12, tzinfo=UTC),
        versioning_state="Enabled",
        destination=FakeBucket("archive"),
        parser_kind="direct",
        copy_mode="direct",
    )
    return source, manifest.entries[0]
