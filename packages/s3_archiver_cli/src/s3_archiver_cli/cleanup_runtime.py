"""Helpers for running cleanup commands as timeout-enforced child processes.

The manual ``cleanup`` command relays a ``cleanup-once`` child through the same
streaming subprocess runner the archiver uses, so a hung cleanup is killed and
its stale lock is reconciled. ``perform_cleanup`` is the shared driver used both
by that child and by the archive process when it chains cleanup automatically.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import typer
from s3_archiver_core.archive_lock import LockRecoveryLogger
from s3_archiver_core.archive_routes import ArchiveRoute
from s3_archiver_core.cleanup import CleanupResult, run_cleanup
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.route_payloads import route_summary_payload
from s3_archiver_core.settings import AppSettings

from s3_archiver_cli.error_logging import log_error_payload as _log_error_payload
from s3_archiver_cli.scheduled_archive import reconcile_archive_lock
from s3_archiver_cli.streaming_subprocess import run_streaming_command as _run_streaming_command

type RunCommand = Callable[..., subprocess.CompletedProcess[str]]
type Echo = Callable[[str], None]

CLEANUP_CHILD_SUBCOMMAND = "cleanup-once"
_FAILURE_SAMPLE_LIMIT = 100


def cleanup_child_command(manifest: Path | None) -> list[str]:
    """Return the in-process cleanup child command."""

    command = [
        sys.executable,
        "-c",
        "from s3_archiver_cli.main import main; main()",
        CLEANUP_CHILD_SUBCOMMAND,
    ]
    if manifest is not None:
        command.extend(["--manifest", str(manifest)])
    return command


def selected_manifests(settings: AppSettings, manifest: Path | None) -> list[Path]:
    """Return the explicit manifest, or every pending manifest to drain."""

    if manifest is not None:
        return [manifest]
    pending_dir = settings.cleanup_pending_dir
    if not pending_dir.is_dir():
        return []
    return sorted(pending_dir.glob("*.jsonl"))


def perform_cleanup(
    settings: AppSettings,
    routes: tuple[ArchiveRoute, ...],
    *,
    manifest: Path | None,
    started: datetime,
    log_file: Path,
    now: Callable[[], datetime] | None = None,
) -> tuple[CleanupResult, dict[str, JsonValue]]:
    """Run cleanup over the selected manifests and build its result payload."""

    clock = _utc_now if now is None else now
    deadline = started + settings.run_timeout
    manifests = selected_manifests(settings, manifest)
    result = run_cleanup(
        routes,
        manifests=manifests,
        cleaned_dir=settings.cleanup_cleaned_dir,
        timed_out=lambda: clock() > deadline,
    )
    return result, cleanup_payload(result, settings, log_file, len(manifests))


def cleanup_status(result: CleanupResult) -> str:
    """Return the structured status string for a cleanup result."""

    if result.empty:
        return "empty"
    return "ok" if result.ok else "error"


def cleanup_payload(
    result: CleanupResult,
    settings: AppSettings,
    log_file: Path,
    manifest_count: int,
) -> dict[str, JsonValue]:
    """Build the CLI payload for one cleanup invocation."""

    status = cleanup_status(result)
    failures = result.failures
    payload: dict[str, JsonValue] = {
        "status": status,
        "phase": "cleanup.run",
        **route_summary_payload(settings),
        "log_file": str(log_file),
        "manifest_count": manifest_count,
        "processed_manifest_count": len(result.outcomes),
        "removed_manifest_count": sum(1 for outcome in result.outcomes if outcome.removed),
        "object_count": result.object_count,
        "cleaned_count": result.cleaned_count,
        "failure_count": len(failures),
        "failures": _json_strings(failures[:_FAILURE_SAMPLE_LIMIT]),
        "failures_truncated": len(failures) > _FAILURE_SAMPLE_LIMIT,
        "manifests": _json_strings(str(outcome.path) for outcome in result.outcomes),
    }
    if status == "empty":
        payload["message"] = "cleanup manifest is empty; nothing to clean"
        payload["details"] = "cleanup manifest is empty"
        payload["reason"] = "cleanup_manifest_empty"
    elif status == "error":
        payload["message"] = "cleanup run failed"
        payload["details"] = failures[0] if failures else "cleanup run failed"
        payload["reason"] = "cleanup_run_failed"
    return payload


def run_cleanup_subprocess(
    settings: AppSettings,
    log_file: Path,
    *,
    manifest: Path | None = None,
    recovery_logger: LockRecoveryLogger | None = None,
    command: list[str] | None = None,
    run_command: RunCommand = subprocess.run,
    stdout_echo: Echo | None = None,
    stderr_echo: Echo | None = None,
    log_error: Callable[[Mapping[str, JsonValue]], None] = _log_error_payload,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Run one cleanup child process, killing it if it hangs and relaying output."""

    emit_stdout = _stdout_echo if stdout_echo is None else stdout_echo
    emit_stderr = _stderr_echo if stderr_echo is None else stderr_echo
    clock = _utc_now if now is None else now
    process_command = list(command or cleanup_child_command(manifest))
    if run_command is subprocess.run:
        try:
            return _run_streaming_command(process_command, settings, emit_stdout, emit_stderr)
        except subprocess.TimeoutExpired as exc:
            _handle_cleanup_subprocess_timeout(
                exc, settings, log_file, emit_stdout, emit_stderr, recovery_logger, log_error, clock
            )
            return 1
    try:
        result = run_command(
            process_command,
            env=dict(os.environ),
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.run_timeout.total_seconds(),
        )
    except subprocess.TimeoutExpired as exc:
        _handle_cleanup_subprocess_timeout(
            exc, settings, log_file, emit_stdout, emit_stderr, recovery_logger, log_error, clock
        )
        return 1
    _relay_output(result.stdout, emit_stdout)
    _relay_output(result.stderr, emit_stderr)
    return result.returncode


def _handle_cleanup_subprocess_timeout(
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
    payload = _cleanup_timeout_payload(settings, log_file)
    _ = reconcile_archive_lock(settings, recovery_logger=recovery_logger, now=clock)
    log_error(payload)
    emit_stderr(json.dumps(payload, sort_keys=True) + "\n")


def _cleanup_timeout_payload(settings: AppSettings, log_file: Path) -> dict[str, JsonValue]:
    return {
        "status": "error",
        "phase": "cleanup.run",
        "field": "ARCHIVER_RUN_TIMEOUT",
        "message": "cleanup run timed out",
        "details": "cleanup run timed out",
        **route_summary_payload(settings),
        "key": None,
        "mismatch": None,
        "reason": "cleanup_run_timeout",
        "timed_out": True,
        "log_file": str(log_file),
    }


def _json_strings(values: Iterable[str]) -> list[JsonValue]:
    return [cast(JsonValue, value) for value in values]


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
    typer.echo(message, nl=False)


def _stderr_echo(message: str) -> None:
    typer.echo(message, err=True, nl=False)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
