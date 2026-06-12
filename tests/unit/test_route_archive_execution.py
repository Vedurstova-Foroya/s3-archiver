"""Route-aware archive execution tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import override

import pytest
from s3_archiver_core.archive import ArchiveRoute, run_archive
from s3_archiver_core.archive_manifest import ManifestEntry
from s3_archiver_core.archive_transfer import archive_metadata
from s3_archiver_core.s3 import S3ListedObject, S3TransferCapabilities, VersioningState

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


class FailingListBucket(FakeBucket):
    @override
    def list_source_objects(
        self, versioning_state: VersioningState, *, prefix: str = ""
    ) -> Iterable[S3ListedObject]:
        _ = versioning_state
        _ = prefix
        raise RuntimeError("list failed")


@pytest.mark.unit()
def test_run_archive_requires_explicit_route_options() -> None:
    with pytest.raises(ValueError, match="at least one route"):
        _ = run_archive(
            (),
            run_timeout=timedelta(days=7),
            run_started_at_utc=STARTED,
            clock=lambda: STARTED,
        )


@pytest.mark.unit()
def test_direct_copy_uses_route_transfer_capabilities() -> None:
    listed = _listed("data/raw.txt", 1, "v1")
    source = FakeBucket("source", (listed,))
    destination = FakeBucket("archive")

    result = run_archive(
        (
            ArchiveRoute(
                "direct",
                source,
                destination,
                parser_kind="direct",
                copy_mode="direct",
                transfer_capabilities=S3TransferCapabilities(
                    native_copy=False,
                    multipart_copy=False,
                    streaming_upload=True,
                    temp_file_backed=True,
                ),
            ),
        ),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.ok is True
    assert destination.copied == ["data/raw.txt"]
    assert destination.copy_strategies == ["multipart_streaming"]


@pytest.mark.unit()
def test_route_list_failure_uses_route_manifest_defaults() -> None:
    source = FailingListBucket("source")
    destination = FakeBucket("archive")

    result = run_archive(
        (
            ArchiveRoute(
                "broken",
                source,
                destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
            ),
        ),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.list.failures == ("list failed",)
    assert result.copy.skipped is True
    assert result.manifest.target_day is None


@pytest.mark.unit()
def test_run_archive_uses_one_worker_per_route() -> None:
    left = FakeBucket("left", (_listed("left/2026-04-13T00-00-00Z.txt", 1, None),))
    right = FakeBucket("right", (_listed("right.txt", 1, None),))
    destination = FakeBucket("archive")
    decisions: list[tuple[str, str]] = []

    result = run_archive(
        (
            ArchiveRoute(
                "daily",
                left,
                destination,
                source_path="left/",
                destination_path="archives/",
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
            ),
            ArchiveRoute(
                "direct",
                right,
                destination,
                destination_path="mirror/",
                parser_kind="direct",
                copy_mode="direct",
            ),
        ),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
        debug_logger=lambda entry, strategy: decisions.append((entry.route_name, strategy)),
    )

    assert result.ok is True
    assert sorted(decisions) == [
        ("daily", "deterministic_tar_gzip"),
        ("direct", "simple_native_copy"),
    ]
    assert destination.uploaded == ["archives/2026-04-13.tar.gz"]
    assert destination.copied == ["right.txt"]


@pytest.mark.unit()
def test_verified_daily_groups_are_tracked_by_destination_identity() -> None:
    listed = _listed("data/2026-04-13T00-00-00Z.txt", 1, None)
    failed_destination = FakeBucket("archive")
    failed_destination.fail_copy = True
    verified_destination = FakeBucket("archive")
    archive_key = "data/2026-04-13.tar.gz"

    result = run_archive(
        (
            ArchiveRoute(
                "failed",
                FakeBucket("source", (listed,)),
                failed_destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
                source_identity=("source", "failed"),
                destination_identity=("destination", "failed"),
            ),
            ArchiveRoute(
                "verified",
                FakeBucket("source", (listed,)),
                verified_destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
                source_identity=("source", "verified"),
                destination_identity=("destination", "verified"),
            ),
        ),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.copy.failures == (f"{archive_key}: copy failed",)


@pytest.mark.unit()
def test_direct_copy_existing_conflicting_destination_fails() -> None:
    listed = _listed("data/raw.txt", 1, "v1")
    source = FakeBucket("source", (listed,))
    destination = FakeBucket(
        "archive",
        destination={
            "data/raw.txt": replace(
                listed.properties,
                metadata=archive_metadata(
                    ManifestEntry(
                        "source",
                        "other.txt",
                        listed.size,
                        listed.last_modified,
                        listed.etag,
                        listed.version_id,
                        replace(listed, key="other.txt"),
                    )
                ),
            )
        },
    )

    result = run_archive(
        (
            ArchiveRoute(
                "default",
                source,
                destination,
                parser_kind="direct",
                copy_mode="direct",
            ),
        ),
        run_timeout=timedelta(days=7),
        run_started_at_utc=STARTED,
        clock=lambda: STARTED,
    )

    assert result.copy.failures == (
        "data/raw.txt: source fingerprint mismatch; remove conflicting destination",
    )
    assert result.verify.skipped is True
