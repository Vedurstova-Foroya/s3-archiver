"""Daily archive verification and cleanup safety tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from s3_archiver_core.archive import (
    ARCHIVE_SHA256_METADATA_KEY,
    MANIFEST_SHA256_METADATA_KEY,
    group_metadata,
    run_archive,
)
from s3_archiver_core.archive_manifest import SourcePathFilter, build_archive_manifest
from s3_archiver_core.archive_options import ArchiveOptions

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


@pytest.mark.unit()
def test_existing_archive_requires_archive_hash_before_cleanup() -> None:
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
    missing_hash = FakeBucket(
        "destination",
        destination={archive_key: _properties(metadata=group_metadata(manifest.archive_groups[0]))},
    )

    result = run_archive(
        source,
        missing_hash,
        ArchiveOptions(retention_days=14, cleanup_enabled=True, max_workers=1),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    assert result.skipped_archive_keys == (archive_key,)
    assert source.deleted == []


@pytest.mark.unit()
def test_mismatched_existing_archive_skips_only_that_group_cleanup() -> None:
    good = _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1, "v-good")
    skipped = _listed("data/harmonie/2026-04-13T07-00-00.xml", 1, "v-skip")
    source = FakeBucket("source", (good, skipped))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )
    good_group = next(
        group for group in manifest.archive_groups if group.archive_root == "data/fae"
    )
    skipped_group = next(
        group for group in manifest.archive_groups if group.archive_root == "data/harmonie"
    )
    payload = b"verified"
    good_metadata = dict(group_metadata(good_group)) | {
        ARCHIVE_SHA256_METADATA_KEY: hashlib.sha256(payload).hexdigest()
    }
    destination = FakeBucket(
        "destination",
        destination={
            good_group.destination_archive_key: _properties(metadata=good_metadata),
            skipped_group.destination_archive_key: _properties(
                metadata={MANIFEST_SHA256_METADATA_KEY: "different"}
            ),
        },
        payloads={good_group.destination_archive_key: payload},
    )

    result = run_archive(
        source,
        destination,
        ArchiveOptions(retention_days=14, cleanup_enabled=True, max_workers=1),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    assert result.verified_archive_keys == (good_group.destination_archive_key,)
    assert result.skipped_archive_keys == (skipped_group.destination_archive_key,)
    assert source.deleted == [(good.key, "v-good")]
