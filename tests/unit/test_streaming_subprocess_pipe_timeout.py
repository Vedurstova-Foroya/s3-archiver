"""Pipe-thread join timeout coverage for the streaming subprocess helper."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from typing import override

import pytest
from s3_archiver_cli import streaming_subprocess
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_join_pipe_threads_returns_when_thread_exits_before_timeout() -> None:
    finished = threading.Event()

    def target() -> None:
        finished.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    streaming_subprocess._join_pipe_threads(thread, timeout_seconds=1.0)  # pyright: ignore[reportPrivateUsage]
    assert finished.is_set()
    assert not thread.is_alive()


@pytest.mark.unit()
def test_join_pipe_threads_logs_warning_when_thread_does_not_exit() -> None:
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("s3_archiver.archive")
    handler = _RecordHandler(records)
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    stop = threading.Event()

    def target() -> None:
        _ = stop.wait()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        streaming_subprocess._join_pipe_threads(thread, timeout_seconds=0.05)  # pyright: ignore[reportPrivateUsage]
        events = [
            record for record in records if record.message.startswith("archive subprocess pipe")
        ]
        assert len(events) == 1
        extras = events[0].__dict__
        assert extras["event"] == "archive.subprocess.pipe_thread_timeout"
        assert extras["timeout_seconds"] == 0.05
    finally:
        stop.set()
        thread.join(timeout=1.0)
        logger.removeHandler(handler)


@pytest.mark.unit()
def test_streaming_command_sends_sigint_when_shutdown_is_requested(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process()
    shutdown = threading.Event()
    shutdown.set()

    def popen(*_args: object, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr("s3_archiver_cli.streaming_subprocess.subprocess.Popen", popen)

    assert (
        streaming_subprocess.run_streaming_command(
            ["cmd"],
            _settings(base_env),
            lambda _line: None,
            lambda _line: None,
            shutdown_event=shutdown,
        )
        == -signal.SIGINT
    )
    assert process.signals == [signal.SIGINT]
    assert process.killed is False


@pytest.mark.unit()
def test_streaming_command_kills_child_when_shutdown_sigint_does_not_exit(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process(ignore_sigint=True)
    shutdown = threading.Event()
    shutdown.set()

    def popen(*_args: object, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr("s3_archiver_cli.streaming_subprocess.subprocess.Popen", popen)

    assert (
        streaming_subprocess.run_streaming_command(
            ["cmd"],
            _settings(base_env),
            lambda _line: None,
            lambda _line: None,
            shutdown_event=shutdown,
        )
        == -9
    )
    assert process.signals == [signal.SIGINT]
    assert process.killed is True


@pytest.mark.unit()
def test_streaming_command_kills_child_when_runtime_timeout_expires(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process()
    times = iter((0.0, 2.0))

    def popen(*_args: object, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr("s3_archiver_cli.streaming_subprocess.subprocess.Popen", popen)
    monkeypatch.setattr("s3_archiver_cli.streaming_subprocess.time.monotonic", lambda: next(times))

    with pytest.raises(subprocess.TimeoutExpired):
        _ = streaming_subprocess.run_streaming_command(
            ["cmd"],
            _settings(base_env),
            lambda _line: None,
            lambda _line: None,
        )

    assert process.killed is True


@pytest.mark.unit()
def test_streaming_command_continues_polling_until_child_exits(
    base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process(return_code=0, wait_timeouts_before_exit=1)
    times = iter((0.0, 0.1, 0.2))

    def popen(*_args: object, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr("s3_archiver_cli.streaming_subprocess.subprocess.Popen", popen)
    monkeypatch.setattr("s3_archiver_cli.streaming_subprocess.time.monotonic", lambda: next(times))

    assert (
        streaming_subprocess.run_streaming_command(
            ["cmd"], _settings(base_env), lambda _line: None, lambda _line: None
        )
        == 0
    )
    assert process.waits == 2


class _RecordHandler(logging.Handler):
    records: list[logging.LogRecord]

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self.records = records

    @override
    def emit(self, record: logging.LogRecord) -> None:
        record.message = record.getMessage()
        self.records.append(record)


@dataclass
class _Process:
    ignore_sigint: bool = False
    return_code: int | None = None
    wait_timeouts_before_exit: int = 0
    stdout: None = None
    stderr: None = None
    signals: list[int] = field(default_factory=list)
    killed: bool = False
    waits: int = 0

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.killed:
            return -9
        if self.signals and not self.ignore_sigint:
            return -self.signals[-1]
        if self.wait_timeouts_before_exit > 0:
            self.wait_timeouts_before_exit -= 1
            raise subprocess.TimeoutExpired(["cmd"], _timeout(timeout))
        if self.return_code is not None:
            return self.return_code
        raise subprocess.TimeoutExpired(["cmd"], _timeout(timeout))

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def kill(self) -> None:
        self.killed = True


def _settings(base_env: dict[str, str]) -> AppSettings:
    env = dict(base_env)
    env["ARCHIVER_RUN_TIMEOUT"] = "1s"
    return AppSettings.from_env(env)


def _timeout(timeout: float | None) -> float:
    return 0.0 if timeout is None else timeout
