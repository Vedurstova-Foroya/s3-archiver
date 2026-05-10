"""Structured CLI error logging helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.errors import (
    ArchiveRunError,
    ConfigError,
    HealthCheckError,
    LoggingError,
    S3ArchiverError,
)
from s3_archiver_core.settings import AppSettings

from s3_archiver_cli.archive_cleanup_status import (
    apply_group_cleanup_statuses,
    failure_key,
    mismatch_payload,
    payload_cleanup_known_keys,
)
from s3_archiver_cli.archive_payloads import (
    archive_group_payloads,
    destination_archive_keys,
    json_list,
    manifest_target_day,
    phase_status,
    skipped_object_payloads,
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]


def log_error_payload(payload: Mapping[str, JsonValue], error: Exception | None = None) -> None:
    """Log a structured error payload after logging is configured."""

    if payload.get("phase") == "startup.env_validation":
        return
    if not logging.getLogger("s3_archiver").handlers:
        return
    logger = logging.getLogger("s3_archiver.archive")
    log_method = logger.exception if error is not None else logger.error
    log_method(
        str(payload.get("message", "s3 archiver error")),
        extra=_error_log_extra(payload),
    )


def _error_log_extra(payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    extra: dict[str, JsonValue] = {
        "event": "s3_archiver.error",
        "error_payload_json": json.dumps(payload, sort_keys=True),
    }
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            extra[f"error_{key}"] = value
    return extra


def archive_result_payload(
    status: str,
    result: ArchiveRunResult,
    settings: AppSettings,
    log_file: Path,
) -> dict[str, JsonValue]:
    """Build the CLI payload for a completed archive invocation."""

    target_day = manifest_target_day(result.manifest)
    archive_groups = archive_group_payloads(
        result.manifest, cleanup_status=phase_status(result.cleanup)
    )
    apply_group_cleanup_statuses(result, archive_groups)
    destination_keys = destination_archive_keys(archive_groups)
    skipped_objects = skipped_object_payloads(result.manifest)
    archive_group_values = json_list(archive_groups)
    skipped_object_values = json_list(skipped_objects)
    manifest_payload: dict[str, JsonValue] = {
        "object_count": len(result.manifest.entries),
        "target_day": target_day,
        "archive_count": len(archive_groups),
        "source_object_count": len(result.manifest.entries),
        "skipped_object_count": len(skipped_objects),
        "destination_archive_keys": destination_keys,
        "skipped_objects": skipped_object_values,
        "archive_groups": archive_group_values,
        "run_started_at_utc": result.manifest.run_started_at_utc.isoformat(),
        "retention_cutoff_utc": result.manifest.retention_cutoff_utc.isoformat(),
    }
    return {
        "status": status,
        "run_id": result.run_id,
        "source_bucket": settings.source.bucket,
        "destination_bucket": settings.destination.bucket,
        "log_file": str(log_file),
        "target_day": target_day,
        "archive_count": len(archive_groups),
        "source_object_count": len(result.manifest.entries),
        "skipped_object_count": len(skipped_objects),
        "destination_archive_keys": destination_keys,
        "archive_groups": archive_group_values,
        "manifest": manifest_payload,
        "phases": {
            "list": _phase_payload(result.list),
            "copy": _phase_payload(result.copy),
            "verify": _phase_payload(result.verify),
            "cleanup": _phase_payload(result.cleanup),
        },
    }


def archive_failure_payload(
    result: ArchiveRunResult,
    settings: AppSettings,
    log_file: Path,
) -> dict[str, JsonValue]:
    """Build the CLI error payload for a failed archive invocation."""

    phase, detail = _first_archive_failure(result)
    timed_out = detail == "archive run timed out"
    payload = archive_result_payload("error", result, settings, log_file)
    cleanup_known_keys = payload_cleanup_known_keys(phase, payload)
    payload.update(
        {
            "phase": f"archive.{phase}",
            "field": "ARCHIVER_RUN_TIMEOUT" if timed_out else None,
            "message": detail if timed_out else "archive run failed",
            "details": detail,
            "key": failure_key(detail, cleanup_known_keys),
            "mismatch": mismatch_payload(phase, detail, cleanup_known_keys),
            "reason": "archive_run_timeout" if timed_out else None,
            "timed_out": timed_out,
        }
    )
    return payload


def error_payload(
    error: S3ArchiverError, settings: AppSettings | None = None
) -> dict[str, JsonValue]:
    """Build a structured CLI error payload for startup or runtime failures."""

    phase = (
        "startup.env_validation"
        if isinstance(error, ConfigError)
        else "archive.run"
        if isinstance(error, ArchiveRunError)
        else "startup.preflight"
    )
    return {
        "status": "error",
        "phase": phase,
        "field": _error_field(error),
        "message": str(error),
        "details": str(error),
        "source_bucket": settings.source.bucket if settings is not None else None,
        "destination_bucket": settings.destination.bucket if settings is not None else None,
        "key": None,
        "mismatch": None,
    }


def _phase_payload(result: ArchivePhaseResult) -> dict[str, JsonValue]:
    return {
        "status": phase_status(result),
        "failure_count": len(result.failures),
        "failures": list(result.failures),
    }


def _first_archive_failure(result: ArchiveRunResult) -> tuple[str, str]:
    for phase in (result.list, result.copy, result.verify, result.cleanup):
        if phase.failures:
            return phase.phase, phase.failures[0]
    return "unknown", "archive run failed"


def _error_field(error: S3ArchiverError) -> str | None:
    if isinstance(error, ConfigError):
        return _field_from_error_message(str(error))
    if isinstance(error, LoggingError):
        return "logging"
    if isinstance(error, HealthCheckError):
        return _preflight_field_from_health_error(str(error))
    return None


def _field_from_error_message(message: str) -> str | None:
    first_token = message.partition(" ")[0]
    if (
        first_token.isidentifier()
        or first_token.startswith("S3_")
        or first_token.startswith("ARCHIVER_")
    ):
        return first_token
    return None


def _preflight_field_from_health_error(message: str) -> str | None:
    lowered = message.lower()
    if "source bucket versioning" in lowered:
        return "source_bucket_versioning"
    if "source bucket" in lowered:
        return "source_bucket_access"
    if "destination bucket" in lowered:
        return "destination_bucket_access"
    return "s3_connectivity"
