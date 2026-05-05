"""Integration tests against LocalStack S3."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Mapping
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core.health import run_health_check
from s3_archiver_core.logging_config import configure_logging
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_RUNTIME_LOG_DIR = (
    REPO_ROOT / ".local" / "integration-runtime" / "var" / "log" / "s3-archiver"
)


def _integration_env() -> dict[str, str]:
    endpoint = os.environ.get("LOCALSTACK_S3_URL", "http://127.0.0.1:4566")
    return {
        "S3_PROVIDER": "localstack",
        "S3_ACCESS_KEY_ID": "test",
        "S3_SECRET_ACCESS_KEY": "test",
        "S3_REGION": "us-east-1",
        "S3_BUCKET": "s3-archiver-integration",
        "S3_ENDPOINT_URL": endpoint,
        "S3_ADDRESSING_STYLE": "path",
        "LOG_LEVEL": "INFO",
        "LOG_DIR": str(INTEGRATION_RUNTIME_LOG_DIR),
    }


@pytest.mark.integration()
def test_health_check_succeeds_against_localstack(localstack_service: None) -> None:
    _ = localstack_service
    _reset_integration_runtime_log_dir()
    settings = AppSettings.from_env(_integration_env())
    log_file = configure_logging(settings)
    logger = logging.getLogger("s3_archiver.integration")
    try:
        report = run_health_check(settings, log_file)

        assert report.status == "ok"
        assert settings.log_dir == INTEGRATION_RUNTIME_LOG_DIR
        assert log_file == INTEGRATION_RUNTIME_LOG_DIR / "s3-archiver.log"
        assert log_file.exists()
        records = _log_records(log_file)
        assert any(
            record.get("event") == "health.started"
            and record.get("bucket") == settings.bucket
            and record.get("endpoint_url") == settings.resolved_endpoint_url()
            for record in records
        )
        assert any(
            record.get("event") == "health.succeeded" and record.get("bucket") == settings.bucket
            for record in records
        )
        file_handler = next(
            handler
            for handler in logging.getLogger("s3_archiver").handlers
            if isinstance(handler, TimedRotatingFileHandler)
        )
        file_handler.doRollover()
        _ = logger.info("after rollover", extra={"event": "integration.after-rollover"})

        rotated_files = sorted(settings.log_dir.glob("s3-archiver.log.*"))

        assert len(rotated_files) == 1
        assert (
            re.fullmatch(r"s3-archiver\.log\.\d{4}-\d{2}-\d{2}", rotated_files[0].name) is not None
        )
        assert '"event": "health.succeeded"' in rotated_files[0].read_text(encoding="utf-8")
        assert '"event": "integration.after-rollover"' in log_file.read_text(encoding="utf-8")
    finally:
        _close_logging_handlers()


@pytest.mark.integration()
def test_localstack_ready_hook_creates_bucket(localstack_service: None) -> None:
    _ = localstack_service
    settings = AppSettings.from_env(_integration_env())
    client = build_s3_client(settings)

    response = client.head_bucket(Bucket=settings.bucket)

    assert response is not None


@pytest.mark.integration()
def test_s3_client_supports_bucket_listing(localstack_service: None) -> None:
    _ = localstack_service
    settings = AppSettings.from_env(_integration_env())
    client = build_s3_client(settings)
    key = "integration/listing-probe.txt"
    body = b"listed"

    _ = client.put_object(Bucket=settings.bucket, Key=key, Body=body)
    response = client.list_objects_v2(Bucket=settings.bucket, Prefix="integration/")

    assert key in _listed_keys(response)


@pytest.mark.integration()
def test_s3_client_supports_object_round_trip(localstack_service: None) -> None:
    _ = localstack_service
    settings = AppSettings.from_env(_integration_env())
    client = build_s3_client(settings)
    key = "integration/probe.txt"
    body = b"s3-archiver"

    _ = client.put_object(Bucket=settings.bucket, Key=key, Body=body)
    response = client.get_object(Bucket=settings.bucket, Key=key)
    payload = response["Body"].read()

    assert payload == body


def _listed_keys(response: Mapping[str, object]) -> set[str]:
    contents = response.get("Contents")
    if not isinstance(contents, list):
        return set()
    keys: set[str] = set()
    for entry_obj in cast(list[object], contents):
        if not isinstance(entry_obj, dict):
            continue
        entry = cast(dict[str, object], entry_obj)
        key = entry.get("Key")
        if isinstance(key, str):
            keys.add(key)
    return keys


def _log_records(log_file: Path) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(line))
        for line in log_file.read_text(encoding="utf-8").splitlines()
    ]


def _close_logging_handlers() -> None:
    for handler in logging.getLogger("s3_archiver").handlers:
        handler.close()


def _reset_integration_runtime_log_dir() -> None:
    shutil.rmtree(INTEGRATION_RUNTIME_LOG_DIR, ignore_errors=True)
