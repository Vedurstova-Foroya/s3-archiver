"""Streaming subprocess helpers for archive child commands."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from threading import Thread
from typing import IO

from s3_archiver_core.settings import AppSettings

type Echo = Callable[[str], None]

PIPE_JOIN_TIMEOUT_SECONDS = 30.0


def run_streaming_command(
    command: list[str],
    settings: AppSettings,
    emit_stdout: Echo,
    emit_stderr: Echo,
) -> int:
    """Run a subprocess while relaying stdout and stderr line-by-line."""

    process = subprocess.Popen(
        command,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout_thread = _pipe_thread(process.stdout, emit_stdout)
    stderr_thread = _pipe_thread(process.stderr, emit_stderr)
    try:
        return_code = process.wait(timeout=settings.run_timeout.total_seconds())
    except subprocess.TimeoutExpired as exc:
        process.kill()
        _ = process.wait()
        _join_pipe_threads(stdout_thread, stderr_thread)
        raise subprocess.TimeoutExpired(command, exc.timeout, output=None, stderr=None) from exc
    _join_pipe_threads(stdout_thread, stderr_thread)
    return return_code


def _pipe_thread(
    pipe: IO[str] | None,
    echo: Echo,
) -> Thread:
    thread = Thread(target=_relay_pipe, args=(pipe, echo), daemon=True)
    thread.start()
    return thread


def _relay_pipe(pipe: IO[str] | None, echo: Echo) -> None:
    if pipe is None:
        return
    with pipe:
        for line in pipe:
            echo(line)


def _join_pipe_threads(
    *threads: Thread, timeout_seconds: float = PIPE_JOIN_TIMEOUT_SECONDS
) -> None:
    logger = logging.getLogger("s3_archiver.archive")
    for thread in threads:
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            logger.warning(
                "archive subprocess pipe thread did not exit",
                extra={
                    "event": "archive.subprocess.pipe_thread_timeout",
                    "timeout_seconds": timeout_seconds,
                },
            )
