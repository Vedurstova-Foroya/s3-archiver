"""Route archive manifest review edge coverage tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from s3_archiver_core.archive_manifest import (
    ArchiveManifestRoute,
    ParserKind,
    build_route_archive_manifest,
)

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


@pytest.mark.unit()
def test_route_manifest_does_not_convert_invalid_parser_kind_to_skip() -> None:
    source = FakeBucket("source", (_listed("data/bad.txt", 1, None),))

    with pytest.raises(ValueError, match="unsupported"):
        _ = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "custom",
                    source,
                    FakeBucket("archive"),
                    parser_kind=cast(ParserKind, cast(object, "unsupported")),
                    copy_mode="direct",
                ),
            ),
            run_started_at_utc=STARTED,
        )


@pytest.mark.unit()
@pytest.mark.parametrize(("left_path", "right_path"), (("data/", "data/fae/"), ("", "data/")))
def test_route_manifest_rejects_overlapping_source_paths(left_path: str, right_path: str) -> None:
    source, destination = FakeBucket("source"), FakeBucket("archive")

    with pytest.raises(ValueError, match="overlapping source paths"):
        _ = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "left",
                    source,
                    destination,
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                    source_path=left_path,
                ),
                ArchiveManifestRoute(
                    "right",
                    source,
                    destination,
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                    source_path=right_path,
                ),
            ),
            run_started_at_utc=STARTED,
        )


@pytest.mark.unit()
def test_route_manifest_allows_non_overlapping_source_paths_on_same_storage() -> None:
    source = FakeBucket("source")
    destination = FakeBucket("archive")

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "left",
                source,
                destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
                source_path="left/",
            ),
            ArchiveManifestRoute(
                "right",
                source,
                destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
                source_path="right/",
            ),
        ),
        run_started_at_utc=STARTED,
    )

    assert len(manifest.entries) == 0


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("left_path", "right_path"),
    (("data", "database"), ("data/", "database/")),
)
def test_route_manifest_allows_sibling_source_paths_on_same_storage(
    left_path: str, right_path: str
) -> None:
    source, destination = FakeBucket("source"), FakeBucket("archive")

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "data",
                source,
                destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
                source_path=left_path,
            ),
            ArchiveManifestRoute(
                "database",
                source,
                destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
                source_path=right_path,
            ),
        ),
        run_started_at_utc=STARTED,
    )

    assert len(manifest.entries) == 0
