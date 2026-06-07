"""Unit coverage for canonical timestamp archive dataset splits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from s3_archiver_core.archive import run_archive

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes, daily_run_timeout
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2024, 4, 20, tzinfo=UTC)
CANONICAL_DAYS = tuple(range(366))


def _clock() -> datetime:
    return STARTED


def _canonical_source(prefix: str) -> FakeBucket:
    return FakeBucket(
        "source",
        tuple(
            _listed(
                f"{prefix}/{(STARTED.date() - timedelta(days=day)).isoformat()}T00-00-00.txt",
                day,
            )
            for day in CANONICAL_DAYS
        ),
    )


@pytest.mark.unit()
def test_canonical_timestamp_dataset_archives_each_selected_day() -> None:
    prefix = "timestamp-canonical"
    source = _canonical_source(prefix)
    destination = FakeBucket("destination")

    result = run_archive(
        archive_routes(source, destination),
        run_timeout=daily_run_timeout(),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    expected_days = tuple(day for day in CANONICAL_DAYS if day > 0)
    expected_keys = [
        f"{prefix}/{(STARTED.date() - timedelta(days=day)).isoformat()}T00-00-00.txt"
        for day in expected_days
    ]
    expected_archive_keys = [
        f"{prefix}/{(STARTED.date() - timedelta(days=day)).isoformat()}.tar.gz"
        for day in reversed(expected_days)
    ]

    assert result.ok is True
    assert [entry.key for entry in result.manifest.entries] == expected_keys
    assert destination.uploaded == expected_archive_keys
    assert destination.copied == []
    assert [item.key for item in result.manifest.skipped_objects] == [
        f"{prefix}/{STARTED.date().isoformat()}T00-00-00.txt"
    ]
