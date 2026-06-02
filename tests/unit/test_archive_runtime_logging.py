"""Archive runtime logging tests."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast

import pytest
from s3_archiver_core.archive import run_archive
from s3_archiver_core.s3 import S3ListedObject

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes, daily_run_timeout
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2024, 4, 20, tzinfo=UTC)


def _clock() -> datetime:
    return STARTED


def _target_key(name: str = "2024-02-20T00-00-00.txt") -> str:
    return f"data/fae/2024/02/20/{name}"


@pytest.mark.unit()
def test_run_archive_logs_temp_storage_before_work(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    temp_dir = tmp_path / "runtime-temp"
    source = FakeBucket("source", (_listed(_target_key(), 90),), temp_dir=temp_dir)
    destination = FakeBucket("destination", temp_dir=temp_dir)
    monkeypatch.setattr(
        "s3_archiver_core.temp_files.shutil.disk_usage",
        _disk_usage(total=10_000_000, used=100, free=9_000_000),
    )

    with caplog.at_level(logging.INFO, logger="s3_archiver.archive"):
        result = run_archive(
            archive_routes(source, destination),
            run_timeout=daily_run_timeout(),
            run_started_at_utc=STARTED,
            clock=_clock,
        )

    assert result.ok is True
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "archive.temp_storage.available"
    ]
    assert len(records) == 1
    assert _record_value(records[0], "temp_dir") == str(temp_dir)
    assert _record_value(records[0], "free_bytes") == 9_000_000


@pytest.mark.unit()
def test_run_archive_reprints_skipped_object_summary_at_completion(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    monkeypatch.setenv("ARCHIVER_MAX_SOURCE_OBJECT_SIZE_MIB", "1")
    source = FakeBucket("source", (_large_listed(_target_key(), size=2 * 1024 * 1024),))

    with caplog.at_level(logging.WARNING, logger="s3_archiver.archive"):
        result = run_archive(
            archive_routes(source, FakeBucket("destination")),
            run_timeout=daily_run_timeout(),
            run_started_at_utc=STARTED,
            clock=_clock,
        )

    assert result.ok is True
    assert len(result.manifest.skipped_objects) == 1
    summary = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "archive.skipped_objects.summary"
    ]
    assert len(summary) == 1
    assert _record_value(summary[0], "skipped_object_count") == 1
    assert _record_value(summary[0], "skipped_reason_counts") == {
        "source object size 2097152 exceeds max source object size 1048576": 1
    }


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _disk_usage(*, total: int, used: int, free: int) -> object:
    def disk_usage(_path: object) -> _DiskUsage:
        return _DiskUsage(total=total, used=used, free=free)

    return disk_usage


def _record_value(record: logging.LogRecord, key: str) -> object:
    return cast(dict[str, object], record.__dict__)[key]


def _large_listed(key: str, *, size: int) -> S3ListedObject:
    listed = _listed(key, 90, "v1")
    return replace(listed, size=size, properties=replace(listed.properties, size=size))


def _isolate_archive_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("s3_archiver")
    for handler in logger.handlers:
        handler.close()
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
