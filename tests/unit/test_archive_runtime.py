"""Runtime-oriented archive workflow unit tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import override

import pytest
from s3_archiver_core.archive import group_metadata, run_archive
from s3_archiver_core.archive_manifest import build_archive_manifest
from s3_archiver_core.s3 import S3ObjectProperties, VersioningState

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes, daily_run_timeout
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties

STARTED = datetime(2024, 4, 20, tzinfo=UTC)


def _clock() -> datetime:
    return STARTED


def _target_key(name: str = "2024-02-20T00-00-00.txt") -> str:
    return f"data/fae/2024/02/20/{name}"


@pytest.mark.unit()
def test_rerun_rejects_existing_archive_with_stale_manifest_metadata() -> None:
    listed = _listed(_target_key(), 90, None)
    source = FakeBucket("source", (listed,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
    )
    archive_key = manifest.archive_groups[0].destination_archive_key
    destination = FakeBucket(
        "destination",
        destination={
            archive_key: replace(
                listed.properties,
                metadata=dict(group_metadata(manifest.archive_groups[0]))
                | {"s3-archiver-manifest-sha256": "stale"},
            )
        },
    )

    result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert result.copy.failures == (f"{archive_key}: archive verification failed",)
    assert result.verify.skipped is True


@pytest.mark.unit()
def test_run_archive_orders_phases_and_reuses_verified_archive() -> None:
    key = _target_key()
    source = FakeBucket("source", (_listed(key, 90, "v1"),))
    destination = FakeBucket("destination")
    decisions: list[tuple[str, str]] = []
    result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
        debug_logger=lambda entry, strategy: decisions.append((entry.key, strategy)),
    )
    archive_key = "data/fae/2024-02-20.tar.gz"
    assert result.ok is True
    assert destination.uploaded == [archive_key]
    assert destination.copied == []
    assert decisions == [(key, "deterministic_tar_gzip")]
    reuse_result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert reuse_result.ok is True


@pytest.mark.unit()
def test_copy_or_verify_failure_blocks_later_phases() -> None:
    key = _target_key()
    archive_key = "data/fae/2024-02-20.tar.gz"
    source = FakeBucket("source", (_listed(key, 90),))
    failing_destination = FakeBucket("destination")
    failing_destination.fail_copy = True

    copy_failed = run_archive(
        archive_routes(source, failing_destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert copy_failed.copy.failures == (f"{archive_key}: copy failed",)
    assert copy_failed.verify.skipped is True

    bad_destination = FakeBucket("destination", destination={archive_key: _properties(size=10)})
    verify_failed = run_archive(
        archive_routes(source, bad_destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert verify_failed.copy.failures == (f"{archive_key}: archive verification failed",)
    assert verify_failed.verify.skipped is True

    class MissingDuringVerify(FakeBucket):
        head_calls: int

        def __init__(self) -> None:
            super().__init__("destination")
            self.head_calls = 0

        @override
        def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
            self.head_calls += 1
            if self.head_calls >= 3:
                return None
            return super().head_object(key, version_id)

    verify_missing = run_archive(
        archive_routes(source, MissingDuringVerify()),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert verify_missing.copy.ok is True
    assert verify_missing.verify.failures == (f"{archive_key}: destination missing",)


@pytest.mark.unit()
def test_archive_upload_failure_keeps_archive_key_for_reporting() -> None:
    listed = _listed(_target_key(), 90)
    source = FakeBucket("source", (listed,))
    destination = FakeBucket("destination")
    destination.fail_copy = True

    result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert result.copy.failures == ("data/fae/2024-02-20.tar.gz: copy failed",)
    assert result.verify.skipped is True


@pytest.mark.unit()
def test_run_archive_timeout_blocks_later_phases() -> None:
    started = datetime(2024, 4, 20, tzinfo=UTC)
    source = FakeBucket("source", (_listed(_target_key(), 90),))
    destination = FakeBucket("destination")

    timed_out = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=started,
        clock=lambda: started + timedelta(days=8),
    )

    assert timed_out.copy.failures == ("archive run timed out",)
    assert timed_out.verify.skipped is True


@pytest.mark.unit()
def test_list_failure_blocks_archive_phases() -> None:
    class BrokenListBucket(FakeBucket):
        @override
        def versioning_state(self) -> VersioningState:
            raise RuntimeError("source.txt: list failed")

    result = run_archive(
        archive_routes(BrokenListBucket("source"), FakeBucket("destination")),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert result.list.failures == ("source.txt: list failed",)
    assert result.copy.skipped is True
    assert result.verify.skipped is True


@pytest.mark.unit()
def test_archive_accepts_source_last_modified_changes_after_listing() -> None:
    listed = replace(
        _listed(_target_key(), 90, None),
        properties=_properties(last_modified=datetime(2024, 2, 21, tzinfo=UTC)),
    )
    source = FakeBucket("source", (listed,))

    result = run_archive(
        archive_routes(source, FakeBucket("destination")),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert result.ok is True


@pytest.mark.unit()
def test_key_only_archive_succeeds_when_current_source_still_matches() -> None:
    listed = _listed(_target_key("2024-02-20T01-00-00.txt"), 90, None)
    source = FakeBucket("source", (listed,))
    destination = FakeBucket("destination")

    result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert result.ok is True

