"""Tests for interruptible scheduler shutdown waits."""

from __future__ import annotations

import signal
import threading
import time

import pytest
from s3_archiver_cli.schedule_runtime import ShutdownFlag


@pytest.mark.unit()
def test_shutdown_flag_sleep_wakes_when_signal_requested() -> None:
    flag = ShutdownFlag()

    def request_shutdown() -> None:
        time.sleep(0.01)
        flag.request(signal.SIGTERM)

    thread = threading.Thread(target=request_shutdown)
    thread.start()
    try:
        started = time.monotonic()
        flag.sleep(30.0)
        elapsed = time.monotonic() - started
    finally:
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert elapsed < 1.0
    assert flag.requested is True
    assert flag.signal_name == "SIGTERM"
