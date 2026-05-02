"""Tests for logging setup."""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import cast

import pytest
from s3_archiver_core.errors import LoggingError, S3ArchiverError
from s3_archiver_core.logging_config import JsonLogFormatter, configure_logging
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_configure_logging_creates_file_and_handlers(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)

    log_file = configure_logging(settings)
    logger = logging.getLogger("s3_archiver.test")
    _ = logger.info("hello world", extra={"event": "unit.log", "bucket": settings.bucket})

    assert log_file.exists()
    payload = _log_records(log_file)[-1]
    assert payload["message"] == "hello world"
    assert payload["event"] == "unit.log"
    assert payload["bucket"] == settings.bucket
    handlers = logging.getLogger("s3_archiver").handlers
    file_handler = next(
        handler for handler in handlers if isinstance(handler, TimedRotatingFileHandler)
    )
    assert file_handler.backupCount == 30
    assert file_handler.when == "MIDNIGHT"
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


@pytest.mark.unit()
def test_configure_logging_rolls_over_to_dated_files(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)
    log_file = configure_logging(settings)
    logger = logging.getLogger("s3_archiver.test")
    _ = logger.info("before rollover", extra={"event": "unit.before-rollover"})
    handlers = logging.getLogger("s3_archiver").handlers
    file_handler = next(
        handler for handler in handlers if isinstance(handler, TimedRotatingFileHandler)
    )

    file_handler.doRollover()
    _ = logger.info("after rollover", extra={"event": "unit.after-rollover"})

    rotated_files = sorted(settings.log_dir.glob("s3-archiver.log.*"))

    assert log_file.exists()
    assert len(rotated_files) == 1
    assert re.fullmatch(r"s3-archiver\.log\.\d{4}-\d{2}-\d{2}", rotated_files[0].name) is not None
    assert "unit.before-rollover" in rotated_files[0].read_text(encoding="utf-8")
    assert "unit.after-rollover" in log_file.read_text(encoding="utf-8")
    for handler in logging.getLogger("s3_archiver").handlers:
        handler.close()


@pytest.mark.unit()
def test_configure_logging_keeps_only_latest_backup_count(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)
    _ = configure_logging(settings)
    handlers = logging.getLogger("s3_archiver").handlers
    file_handler = next(
        handler for handler in handlers if isinstance(handler, TimedRotatingFileHandler)
    )
    for day in range(1, 33):
        rotated_file = settings.log_dir / f"s3-archiver.log.2026-01-{day:02d}"
        _ = rotated_file.write_text("{}", encoding="utf-8")

    files_to_delete = [Path(path).name for path in file_handler.getFilesToDelete()]

    assert len(files_to_delete) == 2
    assert files_to_delete == [
        "s3-archiver.log.2026-01-01",
        "s3-archiver.log.2026-01-02",
    ]
    for handler in logging.getLogger("s3_archiver").handlers:
        handler.close()


@pytest.mark.unit()
def test_json_log_formatter_includes_exception_details() -> None:
    formatter = JsonLogFormatter()
    record = logging.getLogger("s3_archiver.test").makeRecord(
        name="s3_archiver.test",
        level=logging.ERROR,
        fn=__file__,
        lno=1,
        msg="boom",
        args=(),
        exc_info=_exc_info(),
        extra=None,
    )

    rendered = formatter.format(record)

    payload = cast(dict[str, object], json.loads(rendered))
    assert payload["message"] == "boom"
    assert payload["exception"] is not None


@pytest.mark.unit()
def test_json_log_formatter_includes_structured_context() -> None:
    formatter = JsonLogFormatter()
    record = logging.getLogger("s3_archiver.test").makeRecord(
        name="s3_archiver.test",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="context",
        args=(),
        exc_info=None,
        extra={"event": "unit.context", "bucket": "archive-bucket", "attempt": 2},
    )

    payload = cast(dict[str, object], json.loads(formatter.format(record)))

    assert payload["event"] == "unit.context"
    assert payload["bucket"] == "archive-bucket"
    assert payload["attempt"] == 2


@pytest.mark.unit()
def test_json_log_formatter_ignores_unsupported_context_values() -> None:
    formatter = JsonLogFormatter()
    record = logging.getLogger("s3_archiver.test").makeRecord(
        name="s3_archiver.test",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="context",
        args=(),
        exc_info=None,
        extra={"event": "unit.context", "details": {"ignored": True}},
    )

    payload = cast(dict[str, object], json.loads(formatter.format(record)))

    assert payload["event"] == "unit.context"
    assert "details" not in payload


def _exc_info() -> tuple[type[S3ArchiverError], S3ArchiverError, TracebackType | None]:
    try:
        raise S3ArchiverError("boom")
    except S3ArchiverError as exc:
        return (S3ArchiverError, exc, exc.__traceback__)


def _log_records(log_file: Path) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(line))
        for line in log_file.read_text(encoding="utf-8").splitlines()
    ]
