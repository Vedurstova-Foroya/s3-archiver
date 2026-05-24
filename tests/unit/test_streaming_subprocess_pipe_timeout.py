"""Pipe-thread join timeout coverage for the streaming subprocess helper."""

from __future__ import annotations

import logging
import threading
from typing import override

import pytest
from s3_archiver_cli import streaming_subprocess


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


class _RecordHandler(logging.Handler):
    records: list[logging.LogRecord]

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self.records = records

    @override
    def emit(self, record: logging.LogRecord) -> None:
        record.message = record.getMessage()
        self.records.append(record)
