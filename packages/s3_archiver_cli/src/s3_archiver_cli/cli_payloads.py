"""Shared CLI exit-code, working-set, and archive payload helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from s3_archiver_core.archive import ArchiveRunResult
from s3_archiver_core.archive_manifest import ManifestEntry
from s3_archiver_core.errors import ConfigError, HealthCheckError, LoggingError, S3ArchiverError
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.route_payloads import working_set_payload
from s3_archiver_core.settings import AppSettings

from s3_archiver_cli import error_logging as _error_logging
from s3_archiver_cli.archive_progress_reporting import (
    include_archive_payload_details as _include_archive_payload_details,
)

CONFIG_ERROR_EXIT_CODE = 2
LOGGING_ERROR_EXIT_CODE = 3
HEALTH_CHECK_ERROR_EXIT_CODE = 4


def exit_code_for_error(error: S3ArchiverError) -> int:
    """Return the CLI exit code for one domain error."""

    if isinstance(error, ConfigError):
        return CONFIG_ERROR_EXIT_CODE
    if isinstance(error, LoggingError):
        return LOGGING_ERROR_EXIT_CODE
    if isinstance(error, HealthCheckError):
        return HEALTH_CHECK_ERROR_EXIT_CODE
    return 1


def emit_working_set(settings: AppSettings) -> None:
    """Emit the redacted startup working set to stderr."""

    payload: dict[str, JsonValue] = {
        "event": "startup.working_set",
        "working_set": working_set_payload(settings),
    }
    typer.echo(json.dumps(payload, sort_keys=True), err=True)


def archive_result_payload(
    result: ArchiveRunResult, settings: AppSettings, log_file: Path
) -> dict[str, JsonValue]:
    """Build the CLI payload for a completed or failed archive invocation."""

    include_details = _include_archive_payload_details()
    if result.ok:
        return _error_logging.archive_result_payload(
            "ok", result, settings, log_file, include_details=include_details
        )
    return _error_logging.archive_failure_payload(
        result, settings, log_file, include_details=include_details
    )


def log_transfer_decision(entry: ManifestEntry, strategy: str) -> None:
    """Emit a debug log noting the chosen transfer strategy for one object."""

    logging.getLogger("s3_archiver.archive").debug(
        "archive transfer strategy selected",
        extra={
            "event": "archive.transfer.strategy_selected",
            "key": entry.key,
            "source_bucket": entry.source_bucket,
            "strategy": strategy,
        },
    )
