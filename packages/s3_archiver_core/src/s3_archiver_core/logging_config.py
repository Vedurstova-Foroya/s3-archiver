"""Structured logging setup."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging import LogRecord
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import override

from s3_archiver_core.errors import LoggingError
from s3_archiver_core.settings import AppSettings


class JsonLogFormatter(logging.Formatter):
    """Render log records as newline-delimited JSON."""

    @override
    def format(self, record: LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = record.__dict__.get("event")
        if isinstance(event, str):
            payload["event"] = event
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
