"""Tests for logging setup."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest
from s3_archiver_core.errors import LoggingError
from s3_archiver_core.logging_config import configure_logging
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_configure_logging_creates_file_and_handlers(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)

    log_file = configure_logging(settings)
    logger = logging.getLogger("s3_archiver.test")
    _ = logger.info("hello world", extra={"event": "unit.log"})

    assert log_file.exists()
    assert '"message": "hello world"' in log_file.read_text(encoding="utf-8")
    handlers = logging.getLogger("s3_archiver").handlers
    assert any(isinstance(handler, TimedRotatingFileHandler) for handler in handlers)
    for handler in logging.getLogger("s3_archiver").handlers:
        handler.close()


@pytest.mark.unit()
def test_configure_logging_fails_when_log_dir_is_invalid(
    base_env: dict[str, str], tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    _ = blocker.write_text("not a directory", encoding="utf-8")
    base_env["LOG_DIR"] = str(blocker / "logs")
    settings = AppSettings.from_env(base_env)

    with pytest.raises(LoggingError):
        _ = configure_logging(settings)
