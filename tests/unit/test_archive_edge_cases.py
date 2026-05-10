"""Unit tests for archive workflow edge cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import override

import pytest
from s3_archiver_core.archive import MANIFEST_SHA256_METADATA_KEY, group_metadata, run_archive
from s3_archiver_core.archive_manifest import (
    ManifestEntry,
    SourcePathFilter,
    build_archive_manifest,
)
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.s3 import S3ObjectProperties, VersioningState

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2024, 4, 20, tzinfo=UTC)


class FakeRunLock:
    """Archive run lock test double."""

    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired: bool = acquired
        self.released: list[str] = []

    def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: timedelta) -> bool:
        _ = (run_id, run_started_at_utc, timeout)
        return self.acquired

    def release(self, *, run_id: str) -> None:
        self.released.append(run_id)


class SequenceClock:
    """Return the start time for a while, then an expired time."""

    def __init__(self, expire_after_calls: int) -> None:
        self.expire_after_calls: int = expire_after_calls
        self.calls: int = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if self.calls > self.expire_after_calls:
            return STARTED + timedelta(days=8)
        return STARTED


def _target_key(name: str = "2024-02-20T00-00-00.txt") -> str:
    return f"data/fae/2024/02/20/{name}"


@pytest.mark.unit()
def test_run_archive_rejects_held_lock_and_releases_acquired_lock() -> None:
    with pytest.raises(RuntimeError, match="lock is already held"):
        _ = run_archive(
            FakeBucket("source"),
            FakeBucket("destination"),
            ArchiveOptions(retention_days=60),
            run_started_at_utc=STARTED,
            run_lock=FakeRunLock(acquired=False),
            clock=lambda: STARTED,
        )
    lock = FakeRunLock()
    result = run_archive(
        FakeBucket("source"),
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60),
        run_started_at_utc=STARTED,
        run_lock=lock,
        clock=lambda: STARTED,
    )
    assert result.ok is True
    assert lock.released == [result.run_id]


@pytest.mark.unit()
def test_run_archive_reports_timeout_after_copy_and_verify_phases() -> None:
    source = FakeBucket("source", (_listed(_target_key(), 90),))
    batch_timeout = run_archive(
        source,
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60),
        run_started_at_utc=STARTED,
        clock=SequenceClock(expire_after_calls=1),
    )
    assert batch_timeout.copy.failures == ("archive run timed out",)
    assert batch_timeout.verify.skipped is True
    copy_timeout = run_archive(
        source,
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60),
        run_started_at_utc=STARTED,
        clock=SequenceClock(expire_after_calls=2),
    )
    assert copy_timeout.copy.failures == ("archive run timed out",)
    assert copy_timeout.verify.skipped is True
    verify_timeout = run_archive(
        source,
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60),
        run_started_at_utc=STARTED,
        clock=SequenceClock(expire_after_calls=4),
    )
    assert verify_timeout.copy.ok is True
    assert verify_timeout.verify.failures == ("archive run timed out",)
    assert verify_timeout.cleanup.skipped is True


@pytest.mark.unit()
def test_run_archive_reports_timeout_after_cleanup_phase() -> None:
    source = FakeBucket("source", (_listed(_target_key(), 90),))
    result = run_archive(
        source,
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60, cleanup_enabled=True),
        run_started_at_utc=STARTED,
        clock=SequenceClock(expire_after_calls=7),
    )
    assert result.copy.ok is True
    assert result.verify.ok is True
    assert result.cleanup.failures == ("archive run timed out",)


@pytest.mark.unit()
def test_verify_failure_after_copy_blocks_cleanup() -> None:
    class VanishingDestination(FakeBucket):
        head_calls: int

        def __init__(self, bucket: str) -> None:
            super().__init__(bucket)
            self.head_calls = 0

        @override
        def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
            self.head_calls += 1
            if self.head_calls >= 3:
                return None
            return super().head_object(key, version_id)

    source = FakeBucket("source", (_listed(_target_key(), 90),))
    result = run_archive(
        source,
        VanishingDestination("destination"),
        ArchiveOptions(retention_days=60, cleanup_enabled=True),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )
    assert result.copy.ok is True
    assert result.verify.failures == ("data/fae/2024-02-20.tar.gz: destination missing",)
    assert result.cleanup.skipped is True


@pytest.mark.unit()
def test_cleanup_worker_future_exception_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_call_worker(
        worker: Callable[[ManifestEntry], str | None], entry: ManifestEntry
    ) -> str | None:
        _ = entry
        if "_cleanup_phase" in worker.__qualname__:
            raise RuntimeError("executor failed")
        return worker(entry)

    monkeypatch.setattr("s3_archiver_core.archive_workers._call_worker", broken_call_worker)
    result = run_archive(
        FakeBucket("source", (_listed(_target_key(), 90),)),
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60, cleanup_enabled=True),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )
    assert result.copy.ok is True
    assert result.verify.ok is True
    assert result.cleanup.failures == ("worker failure: executor failed",)


@pytest.mark.unit()
def test_cleanup_uses_manifest_version_for_current_archive_group() -> None:
    current = _listed(_target_key(), 90, "v2")
    source = FakeBucket(
        "source",
        (current,),
        version_payloads={(current.key, "v2"): b"current000"},
    )
    result = run_archive(
        source,
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60, cleanup_enabled=True),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )
    assert result.manifest.entries[0].version_id == "v2"
    assert result.ok is True
    assert source.deleted == [(current.key, "v2")]


@pytest.mark.unit()
def test_existing_archive_with_different_manifest_metadata_blocks_cleanup() -> None:
    listed = _listed(_target_key(), 90, "v1")
    source = FakeBucket("source", (listed,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=60,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )
    archive_key = manifest.archive_groups[0].destination_archive_key
    destination = FakeBucket(
        "destination",
        destination={
            archive_key: replace(
                listed.properties,
                metadata=dict(group_metadata(manifest.archive_groups[0]))
                | {MANIFEST_SHA256_METADATA_KEY: "different"},
            )
        },
    )

    result = run_archive(
        source,
        destination,
        ArchiveOptions(retention_days=60, cleanup_enabled=True),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.copy.failures == ()
    assert result.skipped_archive_keys == (archive_key,)
    assert result.cleanup.skipped is False
    assert source.deleted == []


@pytest.mark.unit()
def test_list_failure_with_lock_still_releases_lock() -> None:
    class BrokenListBucket(FakeBucket):
        @override
        def versioning_state(self) -> VersioningState:
            raise RuntimeError("list failed")

    lock = FakeRunLock()
    result = run_archive(
        BrokenListBucket("source"),
        FakeBucket("destination"),
        ArchiveOptions(retention_days=60),
        run_started_at_utc=STARTED,
        run_lock=lock,
    )
    assert result.list.failures == ("list failed",)
    assert lock.released == [result.run_id]
