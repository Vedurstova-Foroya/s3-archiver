"""Structured payload helpers for scheduled archive runs."""

from __future__ import annotations

from pathlib import Path

from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.route_payloads import route_summary_payload
from s3_archiver_core.settings import AppSettings


def timeout_payload(settings: AppSettings, log_file: Path) -> dict[str, JsonValue]:
    """Build the scheduler child timeout payload."""

    return {
        "status": "error",
        "phase": "archive.run",
        "field": "ARCHIVER_RUN_TIMEOUT",
        "message": "archive run timed out",
        "details": "archive run timed out",
        **route_summary_payload(settings),
        "key": None,
        "mismatch": None,
        "reason": "archive_run_timeout",
        "timed_out": True,
        "log_file": str(log_file),
    }
