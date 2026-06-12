"""Tests for scheduled-archive helpers and direct archive runtime wiring."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.main as cli_module
import s3_archiver_cli.scheduled_archive as scheduled_archive_module
import typer
from s3_archiver_core.errors import ArchiveRunError
from s3_archiver_core.settings import AppSettings


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))


_run_archive = cast(
    Callable[[AppSettings, Path], dict[str, object]],
    _private_attr(cli_module, "_run_archive"),
)
_as_text = cast(
    Callable[[str | bytes | None], str],
    _private_attr(scheduled_archive_module, "_as_text"),
)
_stdout_echo = cast(Callable[[str], None], _private_attr(scheduled_archive_module, "_stdout_echo"))
_stderr_echo = cast(Callable[[str], None], _private_attr(scheduled_archive_module, "_stderr_echo"))


@pytest.mark.unit()
def test_scheduled_archive_command_targets_archive_cli() -> None:
    assert scheduled_archive_module.scheduled_archive_command() == [
        sys.executable,
        "-c",
        "from s3_archiver_cli.main import main; main()",
        "archive-once",
    ]


@pytest.mark.unit()
def test_run_scheduled_archive_relays_child_process_streams(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    stdout_messages: list[str] = []
    stderr_messages: list[str] = []
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        env = cast(dict[str, str], kwargs["env"])
        assert env["S3_ARCHIVER_LOCK_OWNER"] == "scheduler"
        return subprocess.CompletedProcess(command, 0, stdout='{"status":"ok"}\n', stderr="warn\n")

    scheduled_archive_module.run_scheduled_archive(
        settings,
        Path("/tmp/log"),
        command=["archive"],
        run_command=fake_run_command,
        stdout_echo=stdout_messages.append,
        stderr_echo=stderr_messages.append,
    )

    assert commands == [["archive"]]
    assert stdout_messages == ['{"status":"ok"}\n']
    assert stderr_messages == ["warn\n"]


@pytest.mark.unit()
def test_run_scheduled_archive_reconciles_lock_before_child_run(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    events: list[str] = []

    class RecordingLock:
        def __init__(self, path: Path, **_kwargs: object) -> None:
            assert path == Path(base_env["LOG_DIR"]) / "archive.lock"

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            assert run_started_at_utc.tzinfo == UTC
            _ = timeout
            events.append(f"lock.acquire:{run_id}")
            return True

        def release(self, *, run_id: str) -> None:
            events.append(f"lock.release:{run_id}")

    def fake_run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        events.append(f"run:{command[0]}")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(scheduled_archive_module, "FileArchiveRunLock", RecordingLock)

    scheduled_archive_module.run_scheduled_archive(
        settings,
        Path("/tmp/log"),
        command=["archive"],
        run_command=fake_run_command,
    )

    assert len(events) == 3
    assert events[0].startswith("lock.acquire:")
    assert events[1] == events[0].replace("acquire", "release")
    assert events[2] == "run:archive"


@pytest.mark.unit()
def test_run_scheduled_archive_skips_child_when_active_lock_remains(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    commands: list[list[str]] = []

    class RefusingLock:
        def __init__(self, path: Path, **_kwargs: object) -> None:
            assert path == Path(base_env["LOG_DIR"]) / "archive.lock"

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            return False

        def release(self, *, run_id: str) -> None:
            raise AssertionError(f"release should not run for {run_id}")

    def fake_run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(scheduled_archive_module, "FileArchiveRunLock", RefusingLock)

    scheduled_archive_module.run_scheduled_archive(
        settings,
        Path("/tmp/log"),
        command=["archive"],
        run_command=fake_run_command,
    )

    assert commands == []


@pytest.mark.unit()
def test_run_scheduled_archive_times_out_and_recovers_stale_lock(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    stderr_messages: list[str] = []
    logged_payloads: list[dict[str, object]] = []
    acquired: list[str] = []
    released: list[str] = []

    class RecordingLock:
        def __init__(self, path: Path, **_kwargs: object) -> None:
            assert path == Path(base_env["LOG_DIR"]) / "archive.lock"

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            assert run_started_at_utc.tzinfo == UTC
            acquired.append(run_id)
            _ = timeout
            return True

        def release(self, *, run_id: str) -> None:
            released.append(run_id)

    def fake_run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=1, output="", stderr="")

    monkeypatch.setattr(scheduled_archive_module, "FileArchiveRunLock", RecordingLock)

    with pytest.raises(ArchiveRunError, match="archive run timed out"):
        scheduled_archive_module.run_scheduled_archive(
            settings,
            Path("/tmp/log"),
            command=["archive"],
            run_command=fake_run_command,
            stderr_echo=stderr_messages.append,
            log_error=lambda payload: logged_payloads.append(
                cast(dict[str, object], dict(payload))
            ),
        )

    assert len(acquired) == 2
    assert released == acquired
    payload = _load_payload(stderr_messages[-1])
    assert payload["field"] == "ARCHIVER_RUN_TIMEOUT"
    assert payload["message"] == "archive run timed out"
    assert payload["phase"] == "archive.run"
    assert payload["reason"] == "archive_run_timeout"
    assert payload["timed_out"] is True
    assert logged_payloads[-1]["phase"] == "archive.run"
    records = sorted((Path(base_env["LOG_DIR"]) / "archive-runs").glob("*.json"))
    assert len(records) == 1
    decoded = cast(object, json.loads(records[0].read_text(encoding="utf-8")))
    assert isinstance(decoded, dict)
    record = cast(dict[str, object], decoded)
    assert record["status"] == "failed"
    payload = record["payload"]
    assert isinstance(payload, dict)
    assert payload["reason"] == "archive_run_timeout"


@pytest.mark.unit()
def test_run_scheduled_archive_timeout_skips_release_when_recovery_lock_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    acquired: list[str] = []
    released: list[str] = []

    class RecoveryRefusingLock:
        def __init__(self, path: Path, **_kwargs: object) -> None:
            assert path == Path(base_env["LOG_DIR"]) / "archive.lock"

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            _ = (run_started_at_utc, timeout)
            acquired.append(run_id)
            return len(acquired) == 1

        def release(self, *, run_id: str) -> None:
            released.append(run_id)

    def fake_run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=1, output=None, stderr=None)

    monkeypatch.setattr(scheduled_archive_module, "FileArchiveRunLock", RecoveryRefusingLock)

    with pytest.raises(ArchiveRunError, match="archive run timed out"):
        scheduled_archive_module.run_scheduled_archive(
            settings,
            Path("/tmp/log"),
            command=["archive"],
            run_command=fake_run_command,
        )

    assert len(acquired) == 2
    assert released == [acquired[0]]


@pytest.mark.unit()
def test_scheduled_archive_text_and_echo_helpers() -> None:
    echoed: list[tuple[str, bool, bool]] = []
    monkeypatch = pytest.MonkeyPatch()

    def record_echo(message: str, *, err: bool = False, nl: bool = True) -> None:
        echoed.append((message, err, nl))

    monkeypatch.setattr(typer, "echo", record_echo)
    try:
        assert _as_text(None) == ""
        assert _as_text(b"warn\n") == "warn\n"
        assert _as_text("ok\n") == "ok\n"
        _stdout_echo("ok\n")
        _stderr_echo("warn\n")
    finally:
        monkeypatch.undo()

    assert echoed == [("ok\n", False, False), ("warn\n", True, False)]


def _load_payload(output: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = cast(object, json.loads(stripped))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return cast(dict[str, object], cast(object, payload))
    raise AssertionError(f"expected JSON payload in output: {output!r}")
