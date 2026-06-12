"""Structured CLI error logging helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.archive_payloads import (
    archive_manifest_payload,
    phase_status,
)
from s3_archiver_core.errors import (
    ArchiveRunError,
    CleanupError,
    CleanupManifestError,
    ConfigError,
    HealthCheckError,
    LoggingError,
    S3ArchiverError,
)
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.route_payloads import route_summary_payload
from s3_archiver_core.settings import AppSettings

_PHASE_FAILURE_SAMPLE_LIMIT = 100


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
    *,
    include_details: bool = True,
) -> dict[str, JsonValue]:
    """Build the CLI payload for a completed archive invocation."""

    manifest_payload = archive_manifest_payload(
        result.manifest,
        include_archive_days=True,
        include_run_started_at_utc=True,
        include_details=include_details,
    )
    payload: dict[str, JsonValue] = {
        "status": status,
        "run_id": result.run_id,
        **route_summary_payload(settings),
        "log_file": str(log_file),
        "run_started_at_utc": manifest_payload["run_started_at_utc"],
        "target_day": manifest_payload["target_day"],
        "archive_count": manifest_payload["archive_count"],
        "direct_copy_count": manifest_payload["direct_copy_count"],
        "source_object_count": manifest_payload["source_object_count"],
        "skipped_object_count": manifest_payload["skipped_object_count"],
        "archive_days": manifest_payload["archive_days"],
        "payload_detail": "full" if include_details else "summary",
        "payload_detail_truncated": not include_details,
        "phases": {
            "list": _phase_payload(result.list),
            "copy": _phase_payload(result.copy),
            "verify": _phase_payload(result.verify),
        },
    }
    if include_details:
        payload.update(
            {
                "destination_archive_keys": manifest_payload["destination_archive_keys"],
                "destination_keys": manifest_payload["destination_keys"],
                "archive_groups": manifest_payload["archive_groups"],
                "direct_entries": manifest_payload["direct_entries"],
                "skipped_objects": manifest_payload["skipped_objects"],
            }
        )
    return payload


def archive_failure_payload(
    result: ArchiveRunResult,
    settings: AppSettings,
    log_file: Path,
    *,
    include_details: bool = True,
) -> dict[str, JsonValue]:
    """Build the CLI error payload for a failed archive invocation."""

    phase, detail = _first_archive_failure(result)
    timed_out = detail == "archive run timed out"
    payload = archive_result_payload(
        "error",
        result,
        settings,
        log_file,
        include_details=include_details,
    )
    payload.update(
        {
            "phase": f"archive.{phase}",
            "field": "ARCHIVER_RUN_TIMEOUT" if timed_out else None,
            "message": detail if timed_out else "archive run failed",
            "details": detail,
            "key": _failure_key(detail),
            "mismatch": _mismatch_payload(phase, detail),
            "reason": "archive_run_timeout" if timed_out else None,
            "timed_out": timed_out,
        }
    )
    return payload


def error_payload(
    error: S3ArchiverError, settings: AppSettings | None = None
) -> dict[str, JsonValue]:
    """Build a structured CLI error payload for startup or runtime failures."""

    timed_out = _archive_error_timed_out(error)
    phase = (
        "startup.env_validation"
        if isinstance(error, ConfigError)
        else "archive.run"
        if isinstance(error, ArchiveRunError)
        else "cleanup.run"
        if isinstance(error, (CleanupError, CleanupManifestError))
        else "startup.preflight"
    )
    return {
        "status": "error",
        "phase": phase,
        "field": "ARCHIVER_RUN_TIMEOUT" if timed_out else _error_field(error),
        "message": str(error),
        "details": str(error),
        **route_summary_payload(settings),
        "key": None,
        "mismatch": None,
        "reason": "archive_run_timeout" if timed_out else None,
        "timed_out": timed_out,
    }


def _phase_payload(result: ArchivePhaseResult) -> dict[str, JsonValue]:
    failures = result.failures[:_PHASE_FAILURE_SAMPLE_LIMIT]
    return {
        "status": phase_status(result),
        "failure_count": len(result.failures),
        "failures": list(failures),
        "failures_truncated": len(failures) < len(result.failures),
    }


def _first_archive_failure(result: ArchiveRunResult) -> tuple[str, str]:
    for phase in (result.list, result.copy, result.verify):
        if phase.failures:
            return phase.phase, phase.failures[0]
    return "unknown", "archive run failed"


def _failure_key(detail: str) -> str | None:
    key, separator, _ = detail.partition(": ")
    if separator == "" or not key:
        return None
    return key


def _mismatch_payload(phase: str, detail: str) -> dict[str, JsonValue] | None:
    key = _failure_key(detail)
    if key is None:
        return None
    mismatch_detail = detail.removeprefix(f"{key}: ")
    category = "content" if "content" in mismatch_detail else "object"
    return {
        "category": category,
        "detail": mismatch_detail,
        "key": key,
        "phase": phase,
    }


def _error_field(error: S3ArchiverError) -> str | None:
    if isinstance(error, ConfigError):
        return _field_from_error_message(str(error))
    if isinstance(error, LoggingError):
        return "logging"
    if isinstance(error, HealthCheckError):
        return _preflight_field_from_health_error(str(error))
    return None


def _archive_error_timed_out(error: S3ArchiverError) -> bool:
    return isinstance(error, ArchiveRunError) and str(error) == "archive run timed out"


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
