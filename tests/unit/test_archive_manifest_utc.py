from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from s3_archiver_core.archive import run_archive
from s3_archiver_core.archive_manifest import build_archive_manifest

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes, daily_run_timeout
from tests.unit.archive_workflow_fakes import listed_object as _listed


@pytest.mark.unit()
def test_manifest_normalizes_non_utc_run_start_before_target_day_selection() -> None:
    source = FakeBucket(
        "source",
        (
            _listed("data/fae/2026-04-13T00-00-00Z.xml", 1),
            _listed("data/fae/2026-04-14T00-00-00Z.xml", 1),
        ),
    )

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=datetime(2026, 4, 28, 1, tzinfo=timezone(timedelta(hours=2))),
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
    )

    assert manifest.run_started_at_utc == datetime(2026, 4, 27, 23, tzinfo=UTC)
    assert manifest.target_day is None
    assert [entry.key for entry in manifest.entries] == [
        "data/fae/2026-04-13T00-00-00Z.xml",
        "data/fae/2026-04-14T00-00-00Z.xml",
    ]
    assert len(manifest.skipped_objects) == 0


@pytest.mark.unit()
def test_run_archive_manifest_normalizes_naive_run_start_as_utc() -> None:
    source = FakeBucket("source")
    destination = FakeBucket("destination")

    result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=datetime(2026, 4, 27, 23),
        clock=lambda: datetime(2026, 4, 27, 23, tzinfo=UTC),
    )

    assert result.manifest.run_started_at_utc == datetime(2026, 4, 27, 23, tzinfo=UTC)


@pytest.mark.unit()
def test_build_archive_manifest_keeps_utc_run_start() -> None:
    started = datetime(2026, 4, 27, 23, tzinfo=UTC)

    manifest = build_archive_manifest(
        FakeBucket("source"),
        run_started_at_utc=started,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
    )

    assert manifest.run_started_at_utc is started


@pytest.mark.unit()
def test_build_archive_manifest_treats_naive_run_start_as_utc() -> None:
    manifest = build_archive_manifest(
        FakeBucket("source"),
        run_started_at_utc=datetime(2026, 4, 27, 23),
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
    )

    assert manifest.run_started_at_utc == datetime(2026, 4, 27, 23, tzinfo=UTC)
