"""Focused coverage tests for direct archive copy edge paths."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast, override

import pytest
from s3_archiver_core._archive_copy import copy_direct_entry
from s3_archiver_core.archive import ArchiveRoute
from s3_archiver_core.archive_manifest import ManifestEntry, build_archive_manifest
from s3_archiver_core.archive_transfer import archive_metadata
from s3_archiver_core.s3 import S3ObjectProperties

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed


@pytest.mark.unit()
def test_direct_copy_existing_verified_destination_is_reused() -> None:
    source, destination, entry = _direct_manifest_objects()
    destination = FakeBucket(
        "archive",
        destination={
            entry.destination_key: replace(
                entry.object.properties, metadata=archive_metadata(entry)
            )
        },
    )

    failure, copied = copy_direct_entry(
        ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
        entry,
        None,
    )

    assert failure is None
    assert copied is True
    assert destination.copied == []


@pytest.mark.unit()
def test_direct_copy_reports_copy_and_post_copy_verification_failures() -> None:
    source, destination, entry = _direct_manifest_objects()
    destination.fail_copy = True

    failure, copied = copy_direct_entry(
        ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
        entry,
        None,
    )

    assert failure == "data/raw.txt: copy failed"
    assert copied is False

    failure, copied = copy_direct_entry(
        ArchiveRoute(
            "direct",
            source,
            MissingDirectDestinationBucket("archive"),
            parser_kind="direct",
            copy_mode="direct",
        ),
        entry,
        None,
    )

    assert failure == "data/raw.txt: destination missing"
    assert copied is False


@pytest.mark.unit()
def test_direct_copy_logs_large_source_key(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    source, destination, entry = _direct_manifest_objects()
    monkeypatch.setenv("ARCHIVER_LARGE_OBJECT_LOG_BYTES", "1")

    with caplog.at_level(logging.INFO, logger="s3_archiver.archive"):
        failure, copied = copy_direct_entry(
            ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
            entry,
            None,
        )

    assert failure is None
    assert copied is True
    record = _single_record(caplog, "archive.object.large")
    assert _record_value(record, "source_key") == "data/raw.txt"
    assert _record_value(record, "destination_key") == "data/raw.txt"
    assert _record_value(record, "size_bytes") == 10
    assert _record_value(record, "operation") == "direct_copy"


@pytest.mark.unit()
def test_direct_copy_logs_source_key_when_object_runs_long(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import s3_archiver_core._archive_object_activity as activity_module

    _isolate_archive_logger(monkeypatch)
    source, destination, entry = _direct_manifest_objects()
    monkeypatch.setenv("ARCHIVER_LONG_OBJECT_LOG_SECONDS", "1")
    monkeypatch.setattr(activity_module, "Timer", _ImmediateTimer)

    with caplog.at_level(logging.INFO, logger="s3_archiver.archive"):
        failure, copied = copy_direct_entry(
            ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
            entry,
            None,
        )

    assert failure is None
    assert copied is True
    record = _single_record(caplog, "archive.object.long_running")
    assert _record_value(record, "source_key") == "data/raw.txt"
    assert _record_value(record, "destination_key") == "data/raw.txt"
    assert _record_value(record, "long_object_log_seconds") == 1.0


@pytest.mark.unit()
def test_direct_copy_disables_long_object_watchdog_when_threshold_is_zero(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    source, destination, entry = _direct_manifest_objects()
    monkeypatch.setenv("ARCHIVER_LONG_OBJECT_LOG_SECONDS", "0")

    with caplog.at_level(logging.INFO, logger="s3_archiver.archive"):
        failure, copied = copy_direct_entry(
            ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
            entry,
            None,
        )

    assert failure is None
    assert copied is True
    assert not [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "archive.object.long_running"
    ]


@pytest.mark.unit()
def test_direct_copy_suppresses_timer_emit_after_copy_finishes(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import s3_archiver_core._archive_object_activity as activity_module

    _isolate_archive_logger(monkeypatch)
    source, destination, entry = _direct_manifest_objects()
    monkeypatch.setenv("ARCHIVER_LONG_OBJECT_LOG_SECONDS", "1")
    monkeypatch.setattr(activity_module, "Timer", _CancelEmitsTimer)

    with caplog.at_level(logging.INFO, logger="s3_archiver.archive"):
        failure, copied = copy_direct_entry(
            ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
            entry,
            None,
        )

    assert failure is None
    assert copied is True
    assert not [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "archive.object.long_running"
    ]


@pytest.mark.unit()
def test_large_object_log_uses_default_threshold_for_invalid_env(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    source, destination, entry = _direct_manifest_objects()
    monkeypatch.setenv("ARCHIVER_LARGE_OBJECT_LOG_BYTES", "invalid")

    with caplog.at_level(logging.INFO, logger="s3_archiver.archive"):
        failure, copied = copy_direct_entry(
            ArchiveRoute("direct", source, destination, parser_kind="direct", copy_mode="direct"),
            entry,
            None,
        )

    assert failure is None
    assert copied is True
    assert not [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "archive.object.large"
    ]


class MissingDirectDestinationBucket(FakeBucket):
    @override
    def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        _ = key
        _ = version_id
        return None


def _direct_manifest_objects() -> tuple[FakeBucket, FakeBucket, ManifestEntry]:
    listed = _listed("data/raw.txt", 1, "v1")
    source = FakeBucket("source", (listed,))
    destination = FakeBucket("archive")
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=datetime(2026, 4, 27, 12, tzinfo=UTC),
        versioning_state="Enabled",
        destination=destination,
        parser_kind="direct",
        copy_mode="direct",
    )
    return source, destination, manifest.entries[0]


class _ImmediateTimer:
    daemon: bool
    _function: Callable[[], object]

    def __init__(self, _interval: float, function: Callable[[], object]) -> None:
        self.daemon = False
        self._function = function

    def start(self) -> None:
        _ = self._function()

    def cancel(self) -> None:
        pass


class _CancelEmitsTimer(_ImmediateTimer):
    @override
    def start(self) -> None:
        pass

    @override
    def cancel(self) -> None:
        _ = self._function()


def _single_record(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    records = [record for record in caplog.records if getattr(record, "event", None) == event]
    assert len(records) == 1
    return records[0]


def _record_value(record: logging.LogRecord, key: str) -> object:
    return cast(dict[str, object], record.__dict__)[key]


def _isolate_archive_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("s3_archiver")
    for handler in logger.handlers:
        handler.close()
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
