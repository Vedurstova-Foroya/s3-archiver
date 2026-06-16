"""Route manifest spill edge coverage."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from s3_archiver_core._archive_manifest_builder import iter_archive_manifest_items
from s3_archiver_core._archive_manifest_models import ArchiveManifestRoute, SourceLister
from s3_archiver_core.archive_manifest import build_route_archive_manifest

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


class _LegacySource:
    bucket: str = "source"

    def versioning_state(self) -> str:
        return "Enabled"

    def list_source_objects(self, versioning_state: str) -> Iterable[object]:
        assert versioning_state == "Enabled"
        return ()


class _PrefixIgnoringSource:
    bucket: str = "source"

    def versioning_state(self) -> str:
        return "Enabled"

    def list_source_objects(self, versioning_state: str, *, prefix: str = "") -> Iterable[object]:
        assert versioning_state == "Enabled"
        assert prefix == "data/"
        return (_listed("outside.txt", 1, "v1"),)


@pytest.mark.unit()
def test_legacy_source_list_requires_no_source_path() -> None:
    source = cast(SourceLister, cast(object, _LegacySource()))

    assert (
        list(
            iter_archive_manifest_items(
                source,
                run_started_at_utc=STARTED,
                versioning_state="Enabled",
                parser_kind="direct",
                copy_mode="direct",
            )
        )
        == []
    )
    with pytest.raises(TypeError):
        _ = list(
            iter_archive_manifest_items(
                source,
                run_started_at_utc=STARTED,
                versioning_state="Enabled",
                parser_kind="direct",
                copy_mode="direct",
                source_path="data/",
            )
        )


@pytest.mark.unit()
def test_route_manifest_stores_entries_and_skips_in_sqlite() -> None:
    future = datetime(2026, 4, 28, tzinfo=UTC)
    source = FakeBucket(
        "source",
        (
            _listed("2026-04-13T00-00-00Z-old-a.txt", 1, "v1"),
            replace(_listed("2026-04-28T00-00-00Z-future-a.txt", 1, "v2"), last_modified=future),
            _listed("2026-04-13T00-00-00Z-old-b.txt", 1, "v3"),
            replace(_listed("2026-04-28T00-00-00Z-future-b.txt", 1, "v4"), last_modified=future),
        ),
    )

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "raw",
                source,
                FakeBucket("archive"),
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
            ),
        ),
        run_started_at_utc=STARTED,
    )

    assert manifest.manifest_storage == "sqlite"
    assert len(manifest.entries) == 2
    assert len(manifest.skipped_objects) == 2


@pytest.mark.unit()
def test_manifest_builder_skips_lister_items_outside_requested_prefix() -> None:
    source = cast(SourceLister, cast(object, _PrefixIgnoringSource()))

    assert (
        list(
            iter_archive_manifest_items(
                source,
                run_started_at_utc=STARTED,
                versioning_state="Enabled",
                parser_kind="direct",
                copy_mode="direct",
                source_path="data/",
            )
        )
        == []
    )
