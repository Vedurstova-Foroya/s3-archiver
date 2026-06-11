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
from s3_archiver_core.archive import ArchiveRoute, ArchiveRunResult, run_archive
from s3_archiver_core.archive_lock import FileArchiveRunLock
from s3_archiver_core.archive_routes import archive_routes_from_settings
from s3_archiver_core.errors import ArchiveRunError, S3ArchiverError
from s3_archiver_core.health import run_health_check
from s3_archiver_core.logging_config import configure_logging
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings
from s3_archiver_core.temp_files import prepare_runtime_temp_dir

from s3_archiver_cli import archive_run_records as _run_records
from s3_archiver_cli import cleanup_commands as _cleanup_commands
from s3_archiver_cli import cli_payloads as _cli_payloads
from s3_archiver_cli import error_logging as _error_logging
from s3_archiver_cli.archive_lock_reporting import log_lock_recovery as _log_lock_recovery
from s3_archiver_cli.archive_progress_reporting import ArchiveProgressReporter
from s3_archiver_cli.cleanup_runtime import run_cleanup_subprocess
from s3_archiver_cli.env import load_runtime_env as _load_runtime_env
from s3_archiver_cli.schedule_runtime import (
    ShutdownFlag,
    install_schedule_signals,
    log_lock_reconcile_failed,
    restore_schedule_signals,
    run_schedule_loop,
)
from s3_archiver_cli.scheduled_archive import (
    parse_daily_at_utc,
    reconcile_archive_lock,
    run_archive_subprocess,
    run_scheduled_archive,
    sleep_until_next_daily_tick,
)

_log_error_payload = _error_logging.log_error_payload
CONFIG_ERROR_EXIT_CODE = _cli_payloads.CONFIG_ERROR_EXIT_CODE
HEALTH_CHECK_ERROR_EXIT_CODE = _cli_payloads.HEALTH_CHECK_ERROR_EXIT_CODE
LOGGING_ERROR_EXIT_CODE = _cli_payloads.LOGGING_ERROR_EXIT_CODE


app: typer.Typer = typer.Typer(add_completion=False, invoke_without_command=True)


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
        _cli_payloads.emit_working_set(settings)
        prepare_runtime_temp_dir(settings.temp_dir)
        report = run_health_check(settings, log_file)
    except S3ArchiverError as exc:
        _raise_cli_error(exc, settings)

    payload = report.as_dict()
    typer.echo(json.dumps(payload, sort_keys=True))


@app.command()
def archive() -> None:
    """Run one archive workflow invocation via a timeout-enforced child process."""
    _run_locked_parent_command(_run_archive_command)


@app.command("archive-once", hidden=True)
def archive_once() -> None:
    """Run one archive workflow invocation."""
    payload = _run_payload_command(_run_archive)
    if not _emit_archive_payload(payload):
        raise typer.Exit(code=1)


@app.command()
def schedule(
    daily_at_utc: Annotated[str, typer.Option(envvar="ARCHIVER_SCHEDULE_UTC")] = "02:00",
) -> None:
    """Run one archive invocation per UTC day without catch-up replay."""
    settings: AppSettings | None = None
    try:
        settings, log_file = _load_settings_and_log_file()
        _cli_payloads.emit_working_set(settings)
    except S3ArchiverError as exc:
        _raise_cli_error(exc, settings)
    hour, minute = _parse_daily_at_utc(daily_at_utc)
    if not reconcile_archive_lock(
        settings, recovery_logger=_log_lock_recovery, recover_unknown_host=True
    ):
        log_lock_reconcile_failed()
    flag = ShutdownFlag()
    previous = install_schedule_signals(flag)
    try:
        run_schedule_loop(
            flag,
            run_once=lambda: run_scheduled_archive(
                settings, log_file, recovery_logger=_log_lock_recovery, shutdown_event=flag.event
            ),
            sleep_until_next_tick=lambda backoff: _sleep_until_next_daily_tick(
                hour, minute, extra_delay_seconds=backoff, sleep=flag.sleep
            ),
            report_error=lambda exc: _emit_cli_error(exc, settings),
        )
    finally:
        restore_schedule_signals(previous)


@app.command()
def cleanup(
    manifest: Annotated[
        Path | None,
        typer.Option(help="Clean one specific manifest instead of all pending manifests."),
    ] = None,
) -> None:
    """Delete and verify the source objects recorded in cleanup manifests.

    Always runs regardless of the ``CLEANUP`` env var, via a timeout-enforced
    child process that shares the archive run lock.
    """

    def run_child(settings: AppSettings, log_file: Path) -> int:
        return run_cleanup_subprocess(
            settings, log_file, manifest=manifest, recovery_logger=_log_lock_recovery
        )

    _run_locked_parent_command(run_child)


@app.command("cleanup-once", hidden=True)
def cleanup_once(
    manifest: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run one cleanup invocation against pending or explicitly named manifests."""

    def command(settings: AppSettings, log_file: Path) -> dict[str, JsonValue]:
        return _cleanup_commands.run_cleanup_once(settings, log_file, manifest)

    payload = _run_payload_command(command)
    if not _cleanup_commands.emit_cleanup_payload(payload):
        raise typer.Exit(code=1)


def main() -> None:
    """Run the CLI app."""
    app()


def _run_locked_parent_command(run_child: Callable[[AppSettings, Path], int]) -> None:
    """Load settings, reconcile any stale lock, and relay one child process."""

    settings: AppSettings | None = None
    try:
        settings, log_file = _load_settings_and_log_file()
        _cli_payloads.emit_working_set(settings)
    except S3ArchiverError as exc:
        _raise_cli_error(exc, settings)
    if settings.archive_lock_path.exists():
        _ = reconcile_archive_lock(settings, recovery_logger=_log_lock_recovery)
    exit_code = run_child(settings, log_file)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


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
    raise typer.Exit(code=_cli_payloads.exit_code_for_error(error)) from error


def _emit_cli_error(error: S3ArchiverError, settings: AppSettings | None) -> None:
    payload = _error_logging.error_payload(error, settings)
    _log_error_payload(payload, error)
    typer.echo(json.dumps(payload, sort_keys=True), err=True)


def _run_archive(settings: AppSettings, log_file: Path) -> dict[str, JsonValue]:
    started = datetime.now(tz=UTC)
    locked_run_id = uuid4().hex
    run_lock = FileArchiveRunLock(settings.archive_lock_path, recovery_logger=_log_lock_recovery)
    if not run_lock.acquire(
        run_id=locked_run_id, run_started_at_utc=started, timeout=settings.run_timeout
    ):
        raise ArchiveRunError("archive run lock is already held")
    chained_cleanup_payload: dict[str, JsonValue] | None = None
    try:
        _run_records.record_started(
            settings, run_id=locked_run_id, run_started_at_utc=started, log_file=log_file
        )
        prepare_runtime_temp_dir(settings.temp_dir)
        _ = run_health_check(settings, log_file)
        routes = archive_routes_from_settings(settings, build_s3_client)
        result = _run_configured_archive(settings, routes, started)
        if result.run_id != locked_run_id:
            result = replace(result, run_id=locked_run_id)
        chained_cleanup_payload = _cleanup_commands.export_and_chain_cleanup(
            settings, routes, result, started, log_file
        )
    except Exception as exc:
        error = exc if isinstance(exc, S3ArchiverError) else ArchiveRunError(str(exc))
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
    payload = _cli_payloads.archive_result_payload(result, settings, log_file)
    if chained_cleanup_payload is not None:
        payload["cleanup"] = chained_cleanup_payload
    _run_records.record_result(settings, result=result, payload=payload, log_file=log_file)
    return payload


def _run_archive_command(settings: AppSettings, log_file: Path) -> int:
    return run_archive_subprocess(settings, log_file, recovery_logger=_log_lock_recovery)


def _run_configured_archive(
    settings: AppSettings, routes: tuple[ArchiveRoute, ...], started: datetime
) -> ArchiveRunResult:
    return run_archive(
        routes,
        run_timeout=settings.run_timeout,
        run_started_at_utc=started,
        debug_logger=_cli_payloads.log_transfer_decision if settings.log_level == "DEBUG" else None,
        progress_logger=ArchiveProgressReporter(),
    )


def _emit_archive_payload(payload: Mapping[str, JsonValue]) -> bool:
    is_error = payload.get("status") == "error"
    if is_error:
        _log_error_payload(payload)
    typer.echo(json.dumps(payload, sort_keys=True), err=is_error)
    return not is_error


def _parse_daily_at_utc(value: str) -> tuple[int, int]:
    return parse_daily_at_utc(value)


def _sleep_until_next_daily_tick(
    hour: int,
    minute: int,
    *,
    extra_delay_seconds: float = 0.0,
    sleep: Callable[[float], None] | None = None,
) -> None:
    sleep_until_next_daily_tick(
        hour,
        minute,
        now=lambda: datetime.now(tz=UTC),
        logger=logging.getLogger("s3_archiver.archive"),
        sleep=time.sleep if sleep is None else sleep,
        extra_delay_seconds=extra_delay_seconds,
    )
