"""Daily archive verification safety tests."""

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
from s3_archiver_core.archive_manifest import build_archive_manifest

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes, daily_run_timeout
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


@pytest.mark.unit()
def test_existing_archive_requires_archive_hash_before_reuse() -> None:
    listed = _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1, "v1")
    source = FakeBucket("source", (listed,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
    )
    archive_key = manifest.archive_groups[0].destination_archive_key
    missing_hash = FakeBucket(
        "destination",
        destination={archive_key: _properties(metadata=group_metadata(manifest.archive_groups[0]))},
    )

    result = run_archive(
        archive_routes(source, missing_hash),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.copy.failures == (f"{archive_key}: archive verification failed",)
    assert result.verify.skipped is True


@pytest.mark.unit()
def test_existing_archive_rejects_mismatched_source_identity() -> None:
    listed = _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1, "v1")
    source = FakeBucket("source", (listed,))
    destination_bucket = FakeBucket("destination")
    other_manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
        destination=destination_bucket,
        source_identity=("other", "source"),
    )
    archive_key = other_manifest.archive_groups[0].destination_archive_key
    payload = b"archive"
    existing_metadata = dict(group_metadata(other_manifest.archive_groups[0])) | {
        ARCHIVE_SHA256_METADATA_KEY: hashlib.sha256(payload).hexdigest()
    }
    destination = FakeBucket(
        "destination",
        destination={archive_key: _properties(metadata=existing_metadata)},
        payloads={archive_key: payload},
    )

    result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.copy.failures == (f"{archive_key}: archive verification failed",)
    assert result.verify.skipped is True


@pytest.mark.unit()
def test_mismatched_existing_archive_fails_only_that_group() -> None:
    good = _listed("data/fae/2026/04/13/07/2026-04-13T07-00-00.xml", 1, "v-good")
    skipped = _listed("data/harmonie/2026-04-13T07-00-00.xml", 1, "v-skip")
    source = FakeBucket("source", (good, skipped))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
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
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is False
    assert result.copy.failures == (
        f"{skipped_group.destination_archive_key}: archive verification failed",
    )


@pytest.mark.unit()
def test_stale_verified_archive_is_refreshed_for_late_arriving_prior_day_objects() -> None:
    started = datetime(2026, 5, 22, 7, tzinfo=UTC)
    original = _listed("data/harmonie/processor/2026-05-21T00-00-00Z.grib", 1, "v1")
    late = _listed("data/harmonie/processor/2026-05-21T06-00-00Z.grib", 1, "v2")
    current_day = _listed("data/harmonie/processor/2026-05-22T00-00-00Z.grib", 1, "v3")
    first_source = FakeBucket("source", (original,))
    destination = FakeBucket("destination")

    first = run_archive(
        archive_routes(first_source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=started,
        clock=lambda: started,
    )
    archive_key = "data/harmonie/processor/2026-05-21.tar.gz"
    first_payload = destination.destination_payload(archive_key)
    assert first.ok is True
    assert first.manifest.skipped_objects == ()

    second_source = FakeBucket("source", (original, late, current_day))
    second = run_archive(
        archive_routes(second_source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=started,
        clock=lambda: started,
    )

    assert second.ok is True
    assert destination.uploaded == [archive_key, archive_key]
    assert destination.destination_payload(archive_key) != first_payload
    assert [entry.key for entry in second.manifest.entries] == [original.key, late.key]
    assert [(skip.key, skip.reason) for skip in second.manifest.skipped_objects] == [
        (current_day.key, "parser timestamp in incomplete UTC day")
    ]
