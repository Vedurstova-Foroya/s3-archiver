"""Structured logging setup."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging import LogRecord
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import cast, override

from s3_archiver_core.errors import LoggingError
from s3_archiver_core.settings import AppSettings

_CORRELATION_FIELDS = ("correlation_id", "trace_id", "span_id")
_RESERVED_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_STABLE_CONTEXT_FIELDS = frozenset({"event", *_CORRELATION_FIELDS})


class JsonLogFormatter(logging.Formatter):
    """Render log records as newline-delimited JSON."""

    @override
    def format(self, record: LogRecord) -> str:
        """Serialize a log record to the stable JSON payload."""

        payload: dict[str, str | int | float | bool | None] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": _event_name(record),
            "message": record.getMessage(),
            "correlation_id": _optional_context_value(record, "correlation_id"),
            "trace_id": _optional_context_value(record, "trace_id"),
            "span_id": _optional_context_value(record, "span_id"),
        }
        for key, value in _context_fields(record).items():
            payload[key] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_logging(settings: AppSettings) -> Path:
    """Configure stdout and rotating file logging."""

    logger = logging.getLogger("s3_archiver")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(_coerce_level(settings.log_level))
    logger.propagate = False

    log_file = settings.log_dir / "s3-archiver.log"
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_handler = logging.StreamHandler(sys.stdout)
        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            utc=True,
        )
    except OSError as exc:
        raise LoggingError(f"Failed to initialize log directory {settings.log_dir}: {exc}") from exc

    formatter = JsonLogFormatter()
    stdout_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)
    logger.info("configured logging", extra={"event": "logging.configured"})
    return log_file


def _coerce_level(log_level: str) -> int:
    return {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
    }[log_level]


def _context_fields(record: LogRecord) -> dict[str, str | int | float | bool | None]:
    payload: dict[str, str | int | float | bool | None] = {}
    for key, value in cast(dict[str, object], record.__dict__).items():
        if key in _RESERVED_LOG_RECORD_FIELDS or key in _STABLE_CONTEXT_FIELDS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = value
    return payload


def _event_name(record: LogRecord) -> str:
    value = cast(dict[str, object], record.__dict__).get("event")
    if isinstance(value, str) and value != "":
        return value
    return "log"


def _optional_context_value(record: LogRecord, key: str) -> str | None:
    value = cast(dict[str, object], record.__dict__).get(key)
    if isinstance(value, str) and value != "":
        return value
    return None
