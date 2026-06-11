"""Streaming subprocess helpers for archive child commands."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from threading import Event, Thread
from typing import IO

from s3_archiver_core.settings import AppSettings

type Echo = Callable[[str], None]

PIPE_JOIN_TIMEOUT_SECONDS = 30.0


def run_streaming_command(
    command: list[str],
    settings: AppSettings,
    emit_stdout: Echo,
    emit_stderr: Echo,
    *,
    extra_env: Mapping[str, str] | None = None,
    shutdown_event: Event | None = None,
) -> int:
    """Run a subprocess while relaying stdout and stderr line-by-line."""

    env = dict(os.environ)
    if extra_env is not None:
        env.update(extra_env)
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout_thread = _pipe_thread(process.stdout, emit_stdout)
    stderr_thread = _pipe_thread(process.stderr, emit_stderr)
    deadline = time.monotonic() + settings.run_timeout.total_seconds()
    try:
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                return _stop_for_shutdown(process, stdout_thread, stderr_thread)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, settings.run_timeout.total_seconds())
            try:
                return_code = process.wait(timeout=min(remaining, 1.0))
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired as exc:
        process.kill()
        _ = process.wait()
        _join_pipe_threads(stdout_thread, stderr_thread)
        raise subprocess.TimeoutExpired(command, exc.timeout, output=None, stderr=None) from exc
    _join_pipe_threads(stdout_thread, stderr_thread)
    return return_code


def _stop_for_shutdown(process: subprocess.Popen[str], *threads: Thread) -> int:
    process.send_signal(signal.SIGINT)
    try:
        return_code = process.wait(timeout=30.0)
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = process.wait()
    _join_pipe_threads(*threads)
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
