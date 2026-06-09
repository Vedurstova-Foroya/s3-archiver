"""Cleanup command orchestration shared by the CLI entrypoints.

Holds the cleanup-input manifest export, the automatic chained-cleanup step run
inside the archive lock, and the lock-acquiring driver used by the standalone
``cleanup-once`` child. Keeping this out of ``main.py`` lets the CLI module stay
a thin command dispatcher.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import typer
from s3_archiver_core.archive import ArchiveRoute, ArchiveRunResult
from s3_archiver_core.archive_lock import FileArchiveRunLock
from s3_archiver_core.archive_routes import archive_routes_from_settings
from s3_archiver_core.cleanup import CleanupResult
from s3_archiver_core.cleanup_manifest import (
    cleanup_record_from_entry,
    validate_cleanup_manifest,
    write_cleanup_manifest,
)
from s3_archiver_core.errors import CleanupError
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings

from s3_archiver_cli.archive_lock_reporting import log_lock_recovery as _log_lock_recovery
from s3_archiver_cli.cleanup_runtime import cleanup_status, perform_cleanup
from s3_archiver_cli.error_logging import log_error_payload as _log_error_payload


def export_and_chain_cleanup(
    settings: AppSettings,
    routes: tuple[ArchiveRoute, ...],
    result: ArchiveRunResult,
    started: datetime,
    log_file: Path,
) -> dict[str, JsonValue] | None:
    """Export the cleanup manifest after a clean run and optionally chain cleanup.

    The cleanup-input manifest is written whenever the run fully succeeds (so a
    later manual ``cleanup`` can consume it) and re-validated immediately, hard
    failing the run if the on-disk manifest is mangled. Automatic cleanup runs
    in-process under the same archive lock only when ``CLEANUP=true``.
    """

    if not result.ok:
        return None
    _ = write_result_manifest(settings, result)
    if not settings.cleanup_enabled:
        return None
    cleanup_result, payload = perform_cleanup(
        settings, routes, manifest=None, started=started, log_file=log_file
    )
    if cleanup_status(cleanup_result) == "error":
        raise CleanupError(cleanup_failure_detail(cleanup_result))
    return payload


def write_result_manifest(settings: AppSettings, result: ArchiveRunResult) -> Path | None:
    """Write and re-validate the cleanup-input manifest for a successful run."""

    entries = result.manifest.entries
    if len(entries) == 0:
        return None
    path = settings.cleanup_pending_dir / f"{result.run_id}.jsonl"
    _ = write_cleanup_manifest(
        path,
        run_id=result.run_id,
        run_started_at_utc=result.manifest.run_started_at_utc.isoformat(),
        records=(cleanup_record_from_entry(entry) for entry in entries),
    )
    _ = validate_cleanup_manifest(path)
    return path


def cleanup_failure_detail(result: CleanupResult) -> str:
    """Return the first cleanup failure, or a generic message when absent."""

    failures = result.failures
    return failures[0] if failures else "cleanup run failed"


def run_cleanup_once(
    settings: AppSettings, log_file: Path, manifest: Path | None
) -> dict[str, JsonValue]:
    """Acquire the shared archive lock and run one cleanup pass."""

    started = datetime.now(tz=UTC)
    locked_run_id = uuid4().hex
    run_lock = FileArchiveRunLock(settings.archive_lock_path, recovery_logger=_log_lock_recovery)
    if not run_lock.acquire(
        run_id=locked_run_id, run_started_at_utc=started, timeout=settings.run_timeout
    ):
        raise CleanupError("archive run lock is already held")
    try:
        routes = archive_routes_from_settings(settings, build_s3_client)
        _result, payload = perform_cleanup(
            settings, routes, manifest=manifest, started=started, log_file=log_file
        )
    finally:
        run_lock.release(run_id=locked_run_id)
    return payload


def emit_cleanup_payload(payload: Mapping[str, JsonValue]) -> bool:
    """Emit a cleanup payload as JSON; return whether it reported success."""

    is_ok = payload.get("status") == "ok"
    if not is_ok:
        _log_error_payload(payload)
    typer.echo(json.dumps(payload, sort_keys=True), err=not is_ok)
    return is_ok
