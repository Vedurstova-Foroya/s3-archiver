"""Edge-case tests for archive run locks."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core.archive_lock import FileArchiveRunLock, parse_duration


def write_lock(path: Path, payload: object) -> None:
    _ = path.write_text(json.dumps(payload), encoding="utf-8")


def read_lock(path: Path) -> Mapping[str, object]:
    decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(decoded, dict)
    return cast(Mapping[str, object], decoded)


@pytest.mark.unit()
def test_file_lock_release_ignores_missing_lock(tmp_path: Path) -> None:
    FileArchiveRunLock(tmp_path / "archive.lock").release(run_id="missing")


@pytest.mark.unit()
def test_file_lock_returns_false_when_lock_install_collides_with_competing_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    _ = lock_path.write_text("{", encoding="utf-8")
    real_link = os.link

    def competing_writer(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(target)) != lock_path:
            return real_link(
                path,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )
        write_lock(
            lock_path,
            {
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "run_id": "competing",
                "run_started_at_utc": datetime.now(tz=UTC).isoformat(),
            },
        )
        raise FileExistsError

    monkeypatch.setattr(os, "link", competing_writer)

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(seconds=1),
    )

    assert acquired is False


@pytest.mark.unit()
def test_file_lock_keeps_active_non_scheduler_lock_from_other_host(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    payload = {
        "hostname": "other-host",
        "pid": 123,
        "run_id": "active",
        "run_started_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    write_lock(
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
        timeout=timedelta(days=7),
        recover_unknown_host=True,
    )

    assert acquired is False
    assert read_lock(lock_path)["run_id"] == "active"
    assert recoveries == []


@pytest.mark.unit()
def test_file_lock_recovers_unknown_host_when_enabled(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    payload = {
        "hostname": "prior-container-host",
        "owner": "scheduler",
        "pid": 123,
        "run_id": "active",
        "run_started_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    write_lock(lock_path, payload)
    recoveries: list[tuple[str, Mapping[str, object]]] = []

    acquired = FileArchiveRunLock(
        lock_path,
        recovery_logger=lambda reason, logged_payload: recoveries.append((reason, logged_payload)),
    ).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
        recover_unknown_host=True,
    )

    assert acquired is True
    assert read_lock(lock_path)["run_id"] == "next"
    assert recoveries == [("stale_lock_unknown_host", payload)]


@pytest.mark.unit()
def test_file_lock_keeps_active_lock_with_invalid_process_metadata(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    write_lock(
        lock_path,
        {
            "hostname": socket.gethostname(),
            "pid": True,
            "run_id": "active",
            "run_started_at_utc": datetime.now(tz=UTC).isoformat(),
        },
    )

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is False
    assert read_lock(lock_path)["run_id"] == "active"


@pytest.mark.unit()
def test_file_lock_release_keeps_lock_with_non_string_run_id(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    write_lock(lock_path, {"run_id": 123})

    FileArchiveRunLock(lock_path).release(run_id="123")

    assert lock_path.exists()


@pytest.mark.unit()
def test_file_lock_recovers_invalid_json_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    _ = lock_path.write_text("{", encoding="utf-8")

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is True
    assert read_lock(lock_path)["run_id"] == "next"


@pytest.mark.unit()
def test_file_lock_release_keeps_non_object_lock_payload(tmp_path: Path) -> None:
    lock_path = tmp_path / "archive.lock"
    _ = lock_path.write_text("[]", encoding="utf-8")

    FileArchiveRunLock(lock_path).release(run_id="anything")

    assert lock_path.exists()


@pytest.mark.unit()
def test_file_lock_release_ignores_concurrent_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    write_lock(lock_path, {"run_id": "owner"})

    def missing_unlink(self: Path, missing_ok: bool = False) -> None:
        _ = (self, missing_ok)
        raise FileNotFoundError

    monkeypatch.setattr(Path, "unlink", missing_unlink)

    FileArchiveRunLock(lock_path).release(run_id="owner")


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("45s", timedelta(seconds=45)),
    ],
)
def test_parse_duration_accepts_minutes_and_seconds(
    raw_value: str,
    expected: timedelta,
) -> None:
    assert parse_duration(raw_value) == expected


@pytest.mark.unit()
@pytest.mark.parametrize("raw_value", ["", "0s", "1w"])
def test_parse_duration_rejects_invalid_values(raw_value: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _ = parse_duration(raw_value)


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("raised", "acquired"),
    [
        (ProcessLookupError, True),
        (PermissionError, False),
        (OSError, True),
    ],
)
def test_file_lock_maps_process_signal_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: type[OSError],
    acquired: bool,
) -> None:
    lock_path = tmp_path / "archive.lock"
    write_lock(lock_path, active_process_payload())

    def raise_os_error(pid: int, signal: int) -> None:
        _ = (pid, signal)
        raise raised

    monkeypatch.setattr(os, "kill", raise_os_error)

    assert (
        FileArchiveRunLock(lock_path).acquire(
            run_id="next",
            run_started_at_utc=datetime.now(tz=UTC),
            timeout=timedelta(days=7),
        )
        is acquired
    )


@pytest.mark.unit()
def test_file_lock_keeps_active_lock_when_signal_check_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    write_lock(lock_path, active_process_payload())
    calls: list[tuple[int, int]] = []

    def record_kill(pid: int, signal: int) -> None:
        calls.append((pid, signal))

    monkeypatch.setattr(os, "kill", record_kill)

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is False
    assert calls == [(123, 0)]


def active_process_payload() -> Mapping[str, object]:
    return {
        "hostname": socket.gethostname(),
        "pid": 123,
        "run_id": "active",
        "run_started_at_utc": datetime.now(tz=UTC).isoformat(),
    }
