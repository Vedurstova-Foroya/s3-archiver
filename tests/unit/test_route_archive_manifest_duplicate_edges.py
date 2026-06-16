"""Route archive manifest duplicate identity edge coverage tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import override

import pytest
from s3_archiver_core.archive_manifest import ArchiveManifestRoute, build_route_archive_manifest
from s3_archiver_core.s3 import S3ListedObject, VersioningState

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


def _large_listed(key: str, *, size: int) -> S3ListedObject:
    listed = _listed(key, 1, "v1")
    return replace(listed, size=size, properties=replace(listed.properties, size=size))


class DuplicateListingBucket(FakeBucket):
    @override
    def list_source_objects(
        self, versioning_state: VersioningState, *, prefix: str = ""
    ) -> Iterable[S3ListedObject]:
        listed = tuple(super().list_source_objects(versioning_state, prefix=prefix))
        return (*listed, *listed)


@pytest.mark.unit()
def test_route_manifest_rejects_duplicate_destinations_across_routes() -> None:
    destination = FakeBucket("archive")

    with pytest.raises(ValueError, match="duplicate destination object identity"):
        _ = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "left",
                    FakeBucket("left", (_listed("same.txt", 1, None),)),
                    destination,
                    parser_kind="direct",
                    copy_mode="direct",
                ),
                ArchiveManifestRoute(
                    "right",
                    FakeBucket("right", (_listed("same.txt", 1, None),)),
                    destination,
                    parser_kind="direct",
                    copy_mode="direct",
                ),
            ),
            run_started_at_utc=STARTED,
        )


@pytest.mark.unit()
def test_route_manifest_rejects_duplicate_daily_archive_destinations_across_routes() -> None:
    destination = FakeBucket("archive")

    with pytest.raises(ValueError, match="duplicate destination object identity"):
        _ = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "left",
                    FakeBucket("left", (_listed("left/2026-04-13T01-00-00Z.xml", 1, None),)),
                    destination,
                    source_path="left/",
                    destination_path="archives/common/",
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                ),
                ArchiveManifestRoute(
                    "right",
                    FakeBucket("right", (_listed("right/2026-04-13T02-00-00Z.xml", 1, None),)),
                    destination,
                    source_path="right/",
                    destination_path="archives/common/",
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                ),
            ),
            run_started_at_utc=STARTED,
        )


@pytest.mark.unit()
def test_route_manifest_rejects_duplicate_destinations_after_size_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHIVER_MAX_DESTINATION_ARCHIVE_SIZE_MIB", "2")
    destination = FakeBucket("archive")

    with pytest.raises(ValueError, match="duplicate destination object identity"):
        _ = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "left",
                    FakeBucket("left", (_listed("left/2026-04-13T01-00-00Z.xml", 1, None),)),
                    destination,
                    source_path="left/",
                    destination_path="archives/common/",
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                ),
                ArchiveManifestRoute(
                    "right",
                    FakeBucket("right", (_listed("right/2026-04-13T02-00-00Z.xml", 1, None),)),
                    destination,
                    source_path="right/",
                    destination_path="archives/common/",
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                ),
                ArchiveManifestRoute(
                    "big",
                    FakeBucket(
                        "big",
                        (_large_listed("big/2026-04-13T03-00-00Z.xml", size=4 * 1024 * 1024),),
                    ),
                    destination,
                    source_path="big/",
                    destination_path="archives/big/",
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                ),
            ),
            run_started_at_utc=STARTED,
        )


@pytest.mark.unit()
def test_route_manifest_rejects_duplicate_source_identities() -> None:
    source = DuplicateListingBucket("source", (_listed("same.txt", 1, "v1"),))

    with pytest.raises(ValueError, match="duplicate source object identity"):
        _ = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "duplicates",
                    source,
                    FakeBucket("destination"),
                    parser_kind="direct",
                    copy_mode="direct",
                ),
            ),
            run_started_at_utc=STARTED,
        )


@pytest.mark.unit()
def test_sqlite_route_manifest_rejects_direct_collision_with_first_archive_chunk() -> None:
    destination = FakeBucket("archive")

    with pytest.raises(ValueError, match="duplicate destination object identity"):
        _ = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "direct",
                    FakeBucket("direct", (_listed("archives/fae/2026-04-13.tar.gz", 1, None),)),
                    destination,
                    parser_kind="direct",
                    copy_mode="direct",
                ),
                ArchiveManifestRoute(
                    "daily",
                    FakeBucket("daily", (_listed("data/fae/2026-04-13T00-00-00Z.xml", 1, None),)),
                    destination,
                    source_path="data/fae/",
                    destination_path="archives/fae/",
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                ),
            ),
            run_started_at_utc=STARTED,
        )
