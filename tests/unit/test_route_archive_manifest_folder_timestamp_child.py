"""Route archive manifest tests for the folder timestamp child parser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from s3_archiver_core._archive_manifest_builder import timestamp_child_archive_key
from s3_archiver_core.archive_manifest import ArchiveManifestRoute, build_route_archive_manifest

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed


@pytest.mark.unit()
def test_route_manifest_groups_wrf_domains_by_timestamp_child_folder() -> None:
    source = FakeBucket(
        "folder-source",
        (
            _listed("data/wrf/ecmwf/2026/05/16/00/d01/out-a.grib", 1, "v1"),
            _listed("data/wrf/ecmwf/2026/05/16/00/d02/out-b.grib", 1, "v2"),
            _listed("data/wrf/ecmwf/2026/05/16/06/d01/nested/out-c.grib", 1, "v3"),
        ),
    )

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "wrf",
                source,
                FakeBucket("archive"),
                source_path="data/wrf/ecmwf/",
                destination_path="data/wrf/ecmwf/",
                parser_kind="folder_timestamp_child",
                copy_mode="timestamp_child_tar_gz",
            ),
        ),
        run_started_at_utc=datetime(2026, 5, 17, tzinfo=UTC),
    )

    assert [
        (group.archive_root, group.copy_mode, group.destination_archive_key)
        for group in manifest.archive_groups
    ] == [
        ("2026/05/16/00/d01", "timestamp_child_tar_gz", "data/wrf/ecmwf/2026-05-16-00-d01.tar.gz"),
        ("2026/05/16/00/d02", "timestamp_child_tar_gz", "data/wrf/ecmwf/2026-05-16-00-d02.tar.gz"),
        ("2026/05/16/06/d01", "timestamp_child_tar_gz", "data/wrf/ecmwf/2026-05-16-06-d01.tar.gz"),
    ]


@pytest.mark.unit()
def test_route_manifest_child_parser_uses_latest_segmented_folder_timestamp() -> None:
    source = FakeBucket(
        "folder-source",
        (_listed("data/model/2026/05/15/run/2026/05/16/00/d01/out.grib", 1, "v1"),),
    )

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "model",
                source,
                FakeBucket("archive"),
                source_path="data/model/",
                destination_path="data/model/",
                parser_kind="folder_timestamp_child",
                copy_mode="timestamp_child_tar_gz",
            ),
        ),
        run_started_at_utc=datetime(2026, 5, 17, tzinfo=UTC),
    )

    assert [
        (group.archive_root, group.destination_archive_key, group.entries[0].selected_timestamp)
        for group in manifest.archive_groups
    ] == [
        (
            "2026/05/15/run/2026/05/16/00/d01",
            "data/model/2026-05-16-00-d01.tar.gz",
            datetime(2026, 5, 16, tzinfo=UTC),
        )
    ]


@pytest.mark.unit()
def test_timestamp_child_archive_key_uses_fallback_child_for_empty_root() -> None:
    assert (
        timestamp_child_archive_key("", datetime(2026, 5, 16, tzinfo=UTC))
        == "2026-05-16-00-archive.tar.gz"
    )
