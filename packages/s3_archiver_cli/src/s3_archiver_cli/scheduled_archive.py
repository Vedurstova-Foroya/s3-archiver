"""Helpers for running archive commands from the daily scheduler."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import uuid4

import typer
from s3_archiver_core.archive_lock import FileArchiveRunLock, LockRecoveryLogger
from s3_archiver_core.errors import ArchiveRunError
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.settings import AppSettings

from s3_archiver_cli import archive_run_records as _run_records
from s3_archiver_cli.error_logging import log_error_payload as _log_error_payload
from s3_archiver_cli.scheduled_archive_payloads import timeout_payload as _timeout_payload
from s3_archiver_cli.streaming_subprocess import run_streaming_command as _run_streaming_command

type RunCommand = Callable[..., subprocess.CompletedProcess[str]]
type Echo = Callable[[str], None]


class Logger(Protocol):
    """Minimal logger protocol for scheduler wait reporting."""

    def info(self, msg: object, *args: object, extra: Mapping[str, object] | None = None) -> object:
        """Record one structured info event."""
        ...


def archive_child_command() -> list[str]:
    """Return the in-process archive child command used by wrappers."""

    return [sys.executable, "-c", "from s3_archiver_cli.main import main; main()", "archive-once"]


def scheduled_archive_command() -> list[str]:
    """Return the archive command used by the scheduler."""

    return archive_child_command()


def parse_daily_at_utc(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` scheduler input."""

    hour_text, separator, minute_text = value.partition(":")
    if separator != ":" or not hour_text.isdigit() or not minute_text.isdigit():
        raise typer.BadParameter("ARCHIVER_SCHEDULE_UTC must look like HH:MM")
    hour, minute = int(hour_text), int(minute_text)
    if hour >= 24 or minute >= 60:
        raise typer.BadParameter("ARCHIVER_SCHEDULE_UTC must look like HH:MM")
    return hour, minute


def sleep_until_next_daily_tick(
    hour: int,
    minute: int,
    *,
    now: Callable[[], datetime],
    logger: Logger,
    sleep: Callable[[float], None] = time.sleep,
    extra_delay_seconds: float = 0.0,
) -> None:
    """Sleep until the next UTC daily schedule tick, plus optional backoff."""

    if extra_delay_seconds > 0.0:
        _ = logger.info(
            "archive schedule backoff before next tick",
            extra={
                "event": "archive.schedule.backoff",
                "delay_seconds": max(int(extra_delay_seconds), 0),
            },
        )
        _ = sleep(extra_delay_seconds)
    current = now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    sleep_seconds = max((target - current).total_seconds(), 0.0)
    _ = logger.info(
        "archive schedule waiting for next tick",
        extra={
            "event": "archive.schedule.waiting",
            "scheduled_at_utc": target.isoformat(),
            "sleep_seconds": max(int(sleep_seconds), 0),
        },
    )
    _ = sleep(sleep_seconds)


def run_archive_subprocess(
    settings: AppSettings,
    log_file: Path,
    *,
    recovery_logger: LockRecoveryLogger | None = None,
    command: Sequence[str] | None = None,
    run_command: RunCommand = subprocess.run,
    stdout_echo: Echo | None = None,
    stderr_echo: Echo | None = None,
    log_error: Callable[[Mapping[str, JsonValue]], None] = _log_error_payload,
    now: Callable[[], datetime] | None = None,
    shutdown_event: Event | None = None,
) -> int:
    """Run one archive child process and relay its output."""

    emit_stdout = _stdout_echo if stdout_echo is None else stdout_echo
    emit_stderr = _stderr_echo if stderr_echo is None else stderr_echo
    clock = _utc_now if now is None else now
    process_command = list(command or archive_child_command())
    child_env = dict(os.environ)
    child_env["S3_ARCHIVER_LOCK_OWNER"] = "scheduler"
    if run_command is subprocess.run:
        try:
            return _run_streaming_command(
                process_command,
                settings,
                emit_stdout,
                emit_stderr,
                extra_env={"S3_ARCHIVER_LOCK_OWNER": "scheduler"},
                shutdown_event=shutdown_event,
            )
        except subprocess.TimeoutExpired as exc:
            _handle_subprocess_timeout(
                exc,
                settings,
                log_file,
                emit_stdout,
                emit_stderr,
                recovery_logger,
                log_error,
                clock,
            )
            return 1
    try:
        result = run_command(
            process_command,
            env=child_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.run_timeout.total_seconds(),
        )
    except subprocess.TimeoutExpired as exc:
        _handle_subprocess_timeout(
            exc,
            settings,
            log_file,
            emit_stdout,
            emit_stderr,
            recovery_logger,
            log_error,
            clock,
        )
        return 1
    _relay_output(result.stdout, emit_stdout)
    _relay_output(result.stderr, emit_stderr)
    return result.returncode


def run_scheduled_archive(
    settings: AppSettings,
    log_file: Path,
    *,
    recovery_logger: LockRecoveryLogger | None = None,
    command: Sequence[str] | None = None,
    run_command: RunCommand = subprocess.run,
    stdout_echo: Echo | None = None,
    stderr_echo: Echo | None = None,
    log_error: Callable[[Mapping[str, JsonValue]], None] = _log_error_payload,
    now: Callable[[], datetime] | None = None,
    shutdown_event: Event | None = None,
) -> None:
    """Run one scheduled archive child process and relay its output."""

    if not reconcile_archive_lock(settings, recovery_logger=recovery_logger, now=now):
        return
    child_timed_out = False

    def record_child_error(payload: Mapping[str, JsonValue]) -> None:
        nonlocal child_timed_out
        if payload.get("reason") == "archive_run_timeout":
            child_timed_out = True
        log_error(payload)

    returncode = run_archive_subprocess(
        settings,
        log_file,
        recovery_logger=recovery_logger,
        command=command or scheduled_archive_command(),
        run_command=run_command,
        stdout_echo=stdout_echo,
        stderr_echo=stderr_echo,
        log_error=record_child_error,
        now=now,
        shutdown_event=shutdown_event,
    )
    if returncode != 0 and (shutdown_event is None or not shutdown_event.is_set()):
        if child_timed_out:
            raise ArchiveRunError("archive run timed out")
        raise ArchiveRunError(f"scheduled archive child exited with code {returncode}")


def _handle_subprocess_timeout(
    exc: subprocess.TimeoutExpired,
    settings: AppSettings,
    log_file: Path,
    emit_stdout: Echo,
    emit_stderr: Echo,
    recovery_logger: LockRecoveryLogger | None,
    log_error: Callable[[Mapping[str, JsonValue]], None],
    clock: Callable[[], datetime],
) -> None:
    _relay_output(_as_text(exc.stdout), emit_stdout)
    _relay_output(_as_text(exc.stderr), emit_stderr)
    payload = _timeout_payload(settings, log_file)
    lock_payload = _run_records.read_lock_payload(settings.archive_lock_path)
    _run_records.record_subprocess_timeout(
        settings,
        payload=payload,
        log_file=log_file,
        lock_payload=lock_payload,
    )
    _ = reconcile_archive_lock(settings, recovery_logger=recovery_logger, now=clock)
    log_error(payload)
    emit_stderr(json.dumps(payload, sort_keys=True) + "\n")


def reconcile_archive_lock(
    settings: AppSettings,
    *,
    recovery_logger: LockRecoveryLogger | None = None,
    now: Callable[[], datetime] | None = None,
    recover_unknown_host: bool = False,
) -> bool:
    """Attempt stale-lock reconciliation without taking ownership of an active run."""

    clock = _utc_now if now is None else now
    run_lock = FileArchiveRunLock(settings.archive_lock_path, recovery_logger=recovery_logger)
    recovery_run_id = uuid4().hex
    acquired = (
        run_lock.acquire(
            run_id=recovery_run_id,
            run_started_at_utc=clock(),
            timeout=settings.run_timeout,
            recover_unknown_host=True,
        )
        if recover_unknown_host
        else run_lock.acquire(
            run_id=recovery_run_id,
            run_started_at_utc=clock(),
            timeout=settings.run_timeout,
        )
    )
    if acquired:
        run_lock.release(run_id=recovery_run_id)
        return True
    return False


def _relay_output(output: str, echo: Echo) -> None:
    if output:
        echo(output)


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode()
    return value


def _stdout_echo(message: str) -> None:
    """Write scheduled child stdout to the parent stdout stream."""

    typer.echo(message, nl=False)


def _stderr_echo(message: str) -> None:
    """Write scheduled child stderr to the parent stderr stream."""

    typer.echo(message, err=True, nl=False)


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(tz=UTC)
