"""Tests for archive run locks."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core import archive_lock
from s3_archiver_core.archive_lock import FileArchiveRunLock, parse_duration


def _write_lock(path: Path, payload: object) -> None:
    _ = path.write_text(json.dumps(payload), encoding="utf-8")


def _read_lock(path: Path) -> Mapping[str, object]:
    decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(decoded, dict)
    return cast(Mapping[str, object], decoded)


def _dead_process_is_alive(pid: int) -> bool:
    _ = pid
    return False


@pytest.mark.unit()
def test_file_lock_releases_only_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    lock = FileArchiveRunLock(lock_path)
    started = datetime.now(tz=UTC)

    assert lock.acquire(run_id="first", run_started_at_utc=started, timeout=timedelta(days=1))
    assert not lock.acquire(run_id="second", run_started_at_utc=started, timeout=timedelta(days=1))
    lock.release(run_id="wrong")
    assert lock_path.exists()
    lock.release(run_id="first")
    assert not lock_path.exists()


@pytest.mark.unit()
def test_parse_duration_accepts_hours() -> None:
    assert parse_duration("12h") == timedelta(hours=12)


@pytest.mark.unit()
def test_file_lock_records_process_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "archive.lock"
    started = datetime(2024, 4, 20, tzinfo=UTC)
    monkeypatch.setenv("S3_ARCHIVER_LOCK_OWNER", "scheduler")

    assert FileArchiveRunLock(lock_path).acquire(
        run_id="current",
        run_started_at_utc=started,
        timeout=timedelta(days=7),
    )

    payload = _read_lock(lock_path)
    assert payload["pid"] == os.getpid()
    assert payload["hostname"] == socket.gethostname()
    assert payload["owner"] == "scheduler"


@pytest.mark.unit()
def test_file_lock_recovers_timed_out_lock_for_live_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    payload = {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "run_id": "active",
        "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
    }
    _write_lock(
        lock_path,
        payload,
    )
    recoveries: list[tuple[str, Mapping[str, object]]] = []

    acquired = FileArchiveRunLock(
        lock_path,
        recovery_logger=lambda reason, logged_payload: recoveries.append((reason, logged_payload)),
    ).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(seconds=1),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"
    assert recoveries == [("stale_lock_timed_out", payload)]


@pytest.mark.unit()
def test_file_lock_recovers_timed_out_lock_from_other_host(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    payload = {
        "hostname": "other-host",
        "pid": 123456,
        "run_id": "remote",
        "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
    }
    _write_lock(lock_path, payload)
    recoveries: list[tuple[str, Mapping[str, object]]] = []

    acquired = FileArchiveRunLock(
        lock_path,
        recovery_logger=lambda reason, logged_payload: recoveries.append((reason, logged_payload)),
    ).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(seconds=1),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"
    assert recoveries == [("stale_lock_timed_out", payload)]


@pytest.mark.unit()
def test_file_lock_recovers_timed_out_lock_for_dead_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    _write_lock(
        lock_path,
        {
            "hostname": socket.gethostname(),
            "pid": 123456,
            "run_id": "dead",
            "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
        },
    )
    monkeypatch.setattr(archive_lock, "_process_is_alive", _dead_process_is_alive)
    recoveries: list[tuple[str, Mapping[str, object]]] = []

    acquired = FileArchiveRunLock(
        lock_path,
        recovery_logger=lambda reason, payload: recoveries.append((reason, payload)),
    ).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(seconds=1),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"
    assert recoveries == [
        (
            "stale_lock_timed_out",
            {
                "hostname": socket.gethostname(),
                "pid": 123456,
                "run_id": "dead",
                "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
            },
        )
    ]


@pytest.mark.unit()
def test_file_lock_recovers_dead_process_before_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    payload = {
        "hostname": socket.gethostname(),
        "pid": 123456,
        "run_id": "dead",
        "run_started_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    _write_lock(lock_path, payload)
    monkeypatch.setattr(archive_lock, "_process_is_alive", _dead_process_is_alive)
    recoveries: list[tuple[str, Mapping[str, object]]] = []

    acquired = FileArchiveRunLock(
        lock_path,
        recovery_logger=lambda reason, logged_payload: recoveries.append((reason, logged_payload)),
    ).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"
    assert recoveries == [("stale_lock_abandoned", payload)]


@pytest.mark.unit()
def test_file_lock_recovers_timed_out_legacy_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    _write_lock(
        lock_path,
        {
            "run_id": "legacy",
            "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
        },
    )

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(seconds=1),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"


@pytest.mark.unit()
def test_file_lock_recovers_invalid_process_metadata(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    _write_lock(
        lock_path,
        {
            "hostname": socket.gethostname(),
            "pid": True,
            "run_id": "invalid",
            "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
        },
    )

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(seconds=1),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"


@pytest.mark.unit()
def test_file_lock_recovers_invalid_metadata(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    _write_lock(lock_path, {"run_id": "broken", "run_started_at_utc": "not-a-date"})
    recoveries: list[tuple[str, Mapping[str, object]]] = []

    acquired = FileArchiveRunLock(
        lock_path,
        recovery_logger=lambda reason, payload: recoveries.append((reason, payload)),
    ).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"
    assert recoveries == [
        ("invalid_lock_metadata", {"run_id": "broken", "run_started_at_utc": "not-a-date"})
    ]


@pytest.mark.unit()
def test_file_lock_recovers_parseable_naive_timestamp_as_invalid_metadata(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "archive.lock"
    payload = {"run_id": "naive", "run_started_at_utc": "2024-04-20T12:00:00"}
    _write_lock(lock_path, payload)
    recoveries: list[tuple[str, Mapping[str, object]]] = []

    acquired = FileArchiveRunLock(
        lock_path,
        recovery_logger=lambda reason, logged_payload: recoveries.append((reason, logged_payload)),
    ).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is True
    assert _read_lock(lock_path)["run_id"] == "next"
    assert recoveries == [("invalid_lock_metadata", payload)]
