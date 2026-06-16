"""Tests for the date-range filter applied during manifest building."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from s3_archiver_core.archive_date_range import ArchiveDateRange
from s3_archiver_core.archive_manifest import ArchiveManifest, build_archive_manifest

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

_RUN_STARTED = datetime(2026, 1, 1, tzinfo=UTC)


def _source() -> FakeBucket:
    return FakeBucket(
        "source",
        (
            _listed("data/fae/2018-12-31T00-00-00Z.xml", 1),
            _listed("data/fae/2019-06-15T00-00-00Z.xml", 1),
            _listed("data/fae/2020-12-31T00-00-00Z.xml", 1),
            _listed("data/fae/2021-01-01T00-00-00Z.xml", 1),
        ),
    )


def _manifest(date_range: ArchiveDateRange) -> ArchiveManifest:
    return build_archive_manifest(
        _source(),
        run_started_at_utc=_RUN_STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
        date_range=date_range,
    )


@pytest.mark.unit()
def test_open_range_keeps_every_eligible_object() -> None:
    manifest = _manifest(ArchiveDateRange())

    assert [entry.target_day for entry in manifest.entries] == [
        date(2018, 12, 31),
        date(2019, 6, 15),
        date(2020, 12, 31),
        date(2021, 1, 1),
    ]
    assert len(manifest.skipped_objects) == 0


@pytest.mark.unit()
def test_range_selects_only_in_window_objects() -> None:
    manifest = _manifest(ArchiveDateRange(date(2019, 1, 1), date(2020, 12, 31)))

    assert [entry.key for entry in manifest.entries] == [
        "data/fae/2019-06-15T00-00-00Z.xml",
        "data/fae/2020-12-31T00-00-00Z.xml",
    ]
    skipped = {obj.key: obj.reason for obj in manifest.skipped_objects}
    assert skipped == {
        "data/fae/2018-12-31T00-00-00Z.xml": "parser timestamp outside configured date range",
        "data/fae/2021-01-01T00-00-00Z.xml": "parser timestamp outside configured date range",
    }


@pytest.mark.unit()
def test_subday_bounds_round_to_the_object_day() -> None:
    source = FakeBucket("source", (_listed("data/fae/2019-01-01T00-00-00Z.xml", 1),))

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=_RUN_STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
        date_range=ArchiveDateRange(date(2019, 1, 1), date(2019, 1, 1)),
    )

    assert [entry.key for entry in manifest.entries] == ["data/fae/2019-01-01T00-00-00Z.xml"]


@pytest.mark.unit()
def test_incomplete_utc_day_guard_takes_precedence_over_range() -> None:
    source = FakeBucket("source", (_listed("data/fae/2026-01-01T00-00-00Z.xml", 1),))

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=_RUN_STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
        date_range=ArchiveDateRange(date(2026, 1, 1), date(2026, 1, 1)),
    )

    assert len(manifest.entries) == 0
    assert [obj.reason for obj in manifest.skipped_objects] == [
        "parser timestamp in incomplete UTC day"
    ]
