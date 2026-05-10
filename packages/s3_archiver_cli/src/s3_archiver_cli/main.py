"""CLI entrypoint."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import uuid4

import typer
from s3_archiver_core.archive import run_archive
from s3_archiver_core.archive_lock import FileArchiveRunLock
from s3_archiver_core.archive_manifest import ManifestEntry
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.errors import (
    ArchiveRunError,
    ConfigError,
    HealthCheckError,
    LoggingError,
    S3ArchiverError,
)
from s3_archiver_core.health import run_health_check
from s3_archiver_core.logging_config import configure_logging
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings
from s3_archiver_core.temp_files import prepare_runtime_temp_dir

from s3_archiver_cli import archive_run_records as _run_records
from s3_archiver_cli import error_logging as _error_logging
from s3_archiver_cli import scheduled_archive as _scheduled_archive
from s3_archiver_cli import visual_demo_command as _visual_demo_command
from s3_archiver_cli.archive_lock_reporting import log_lock_recovery as _log_lock_recovery
from s3_archiver_cli.cleanup_preview import run_cleanup_preview as _run_cleanup_preview
from s3_archiver_cli.env import load_runtime_env as _load_runtime_env

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
type ReconcileArchiveLock = Callable[..., bool]
type RunArchiveSubprocess = Callable[..., int]
type RunScheduledArchive = Callable[..., None]

_log_error_payload = _error_logging.log_error_payload
reconcile_archive_lock: ReconcileArchiveLock = _scheduled_archive.reconcile_archive_lock
run_archive_subprocess: RunArchiveSubprocess = _scheduled_archive.run_archive_subprocess
run_scheduled_archive: RunScheduledArchive = _scheduled_archive.run_scheduled_archive


app: typer.Typer = typer.Typer(add_completion=False, invoke_without_command=True)
CONFIG_ERROR_EXIT_CODE = 2
LOGGING_ERROR_EXIT_CODE = 3
HEALTH_CHECK_ERROR_EXIT_CODE = 4


@app.callback()
def root(ctx: typer.Context) -> None:
    """Run s3-archiver commands."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@app.command()
def check() -> None:
    """Validate configuration, logging, and bucket access."""
    settings: AppSettings | None = None
    try:
        settings, log_file = _load_settings_and_log_file()
        prepare_runtime_temp_dir(settings.temp_dir)
        report = run_health_check(settings, log_file)
    except S3ArchiverError as exc:
        _raise_cli_error(exc, settings)

    payload = report.as_dict()
    typer.echo(json.dumps(payload, sort_keys=True))


@app.command()
def archive() -> None:
    """Run one archive workflow invocation via a timeout-enforced child process."""
    settings: AppSettings | None = None
    try:
        settings, log_file = _load_settings_and_log_file()
    except S3ArchiverError as exc:
        _raise_cli_error(exc, settings)
    if _archive_lock_path(settings).exists():
        _ = reconcile_archive_lock(settings, recovery_logger=_log_lock_recovery)
    exit_code = _run_archive_command(settings, log_file)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("archive-once", hidden=True)
def archive_once() -> None:
    """Run one archive workflow invocation."""
    payload = _run_payload_command(_run_archive)
    if not _emit_archive_payload(payload):
        raise typer.Exit(code=1)


@app.command("cleanup-preview")
def cleanup_preview() -> None:
    """Print and persist the cleanup manifest without deleting any source objects."""
    payload = _run_payload_command(_run_cleanup_preview)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def demo() -> None:
    """Run a human-readable archive walkthrough backed by real S3 state."""
    _run_visual_demo_command(perform_cleanup=False)


@app.command("demo-cleanup")
def demo_cleanup() -> None:
    """Run a human-readable archive walkthrough that deletes verified source objects."""
    _run_visual_demo_command(perform_cleanup=True)


def _run_visual_demo_command(*, perform_cleanup: bool) -> None:
    _visual_demo_command.run(
        perform_cleanup=perform_cleanup,
        run_payload_command=_run_payload_command,
        archive_runner=_run_archive,
        cleanup_preview_runner=_run_cleanup_preview,
        emit=typer.echo,
    )


@app.command()
def schedule(
    daily_at_utc: Annotated[str, typer.Option(envvar="ARCHIVER_SCHEDULE_UTC")] = "02:00",
) -> None:
    """Run one archive invocation per UTC day without catch-up replay."""

    settings: AppSettings | None = None
    try:
        settings, log_file = _load_settings_and_log_file()
    except S3ArchiverError as exc:
        _raise_cli_error(exc, settings)
    hour, minute = _parse_daily_at_utc(daily_at_utc)
    _ = reconcile_archive_lock(settings, recovery_logger=_log_lock_recovery)
    while True:
        _sleep_until_next_daily_tick(hour, minute)
        try:
            run_scheduled_archive(settings, log_file, recovery_logger=_log_lock_recovery)
        except S3ArchiverError as exc:
            _emit_cli_error(exc, settings)


def main() -> None:
    """Run the CLI app."""
    app()


def _exit_code_for_error(error: S3ArchiverError) -> int:
    if isinstance(error, ConfigError):
        return CONFIG_ERROR_EXIT_CODE
    if isinstance(error, LoggingError):
        return LOGGING_ERROR_EXIT_CODE
    if isinstance(error, HealthCheckError):
        return HEALTH_CHECK_ERROR_EXIT_CODE
    return 1


def _load_settings_and_log_file() -> tuple[AppSettings, Path]:
    settings = AppSettings.from_env(_load_runtime_env())
    return settings, configure_logging(settings)


def _run_payload_command(
    command: Callable[[AppSettings, Path], dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    settings: AppSettings | None = None
    try:
        settings, log_file = _load_settings_and_log_file()
        return command(settings, log_file)
    except S3ArchiverError as exc:
        _raise_cli_error(exc, settings)


def _raise_cli_error(error: S3ArchiverError, settings: AppSettings | None) -> NoReturn:
    _emit_cli_error(error, settings)
    raise typer.Exit(code=_exit_code_for_error(error)) from error


def _emit_cli_error(error: S3ArchiverError, settings: AppSettings | None) -> None:
    payload = _error_logging.error_payload(error, settings)
    _log_error_payload(payload, error)
    typer.echo(json.dumps(payload, sort_keys=True), err=True)


def _run_archive(settings: AppSettings, log_file: Path) -> dict[str, JsonValue]:
    started = datetime.now(tz=UTC)
    locked_run_id = uuid4().hex
    run_lock = FileArchiveRunLock(_archive_lock_path(settings), recovery_logger=_log_lock_recovery)
    if not run_lock.acquire(
        run_id=locked_run_id,
        run_started_at_utc=started,
        timeout=settings.run_timeout,
    ):
        raise ArchiveRunError("archive run lock is already held")
    try:
        _run_records.record_started(
            settings,
            run_id=locked_run_id,
            run_started_at_utc=started,
            log_file=log_file,
        )
        prepare_runtime_temp_dir(settings.temp_dir)
        _ = run_health_check(settings, log_file)
        source = S3ArchiveBucket(
            build_s3_client(settings.source), settings.source.bucket, settings.temp_dir
        )
        destination = S3ArchiveBucket(
            build_s3_client(settings.destination),
            settings.destination.bucket,
            settings.temp_dir,
        )
        result = run_archive(
            source,
            destination,
            ArchiveOptions.from_settings(settings),
            run_started_at_utc=started,
            debug_logger=_log_transfer_decision if settings.log_level == "DEBUG" else None,
        )
        if result.run_id != locked_run_id:
            result = replace(result, run_id=locked_run_id)
    except Exception as exc:
        error: S3ArchiverError = (
            exc if isinstance(exc, S3ArchiverError) else ArchiveRunError(str(exc))
        )
        _run_records.record_failure(
            settings,
            run_id=locked_run_id,
            run_started_at_utc=started,
            payload=_error_logging.error_payload(error, settings),
            log_file=log_file,
        )
        if error is exc:
            raise
        raise error from exc
    finally:
        run_lock.release(run_id=locked_run_id)
    payload = (
        _error_logging.archive_result_payload("ok", result, settings, log_file)
        if result.ok
        else _error_logging.archive_failure_payload(result, settings, log_file)
    )
    _run_records.record_result(settings, result=result, payload=payload, log_file=log_file)
    return payload


def _run_archive_command(settings: AppSettings, log_file: Path) -> int:
    return run_archive_subprocess(settings, log_file, recovery_logger=_log_lock_recovery)


def _archive_lock_path(settings: AppSettings) -> Path:
    return settings.log_dir / "archive.lock"


def _emit_archive_payload(payload: Mapping[str, JsonValue]) -> bool:
    if payload.get("status") == "error":
        _log_error_payload(payload)
        typer.echo(json.dumps(payload, sort_keys=True), err=True)
        return False
    typer.echo(json.dumps(payload, sort_keys=True))
    return True


def _log_transfer_decision(entry: ManifestEntry, strategy: str) -> None:
    logging.getLogger("s3_archiver.archive").debug(
        "archive transfer strategy selected",
        extra={
            "event": "archive.transfer.strategy_selected",
            "key": entry.key,
            "source_bucket": entry.source_bucket,
            "strategy": strategy,
        },
    )


def _parse_daily_at_utc(value: str) -> tuple[int, int]:
    return _scheduled_archive.parse_daily_at_utc(value)


def _sleep_until_next_daily_tick(hour: int, minute: int) -> None:
    _scheduled_archive.sleep_until_next_daily_tick(
        hour,
        minute,
        now=lambda: datetime.now(tz=UTC),
        logger=logging.getLogger("s3_archiver.archive"),
        sleep=time.sleep,
    )
