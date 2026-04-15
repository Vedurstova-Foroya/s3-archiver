"""Integration tests against LocalStack S3."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from s3_archiver_core.health import run_health_check
from s3_archiver_core.logging_config import configure_logging
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings


def _integration_env(tmp_path: Path) -> dict[str, str]:
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
        "LOG_DIR": str(tmp_path / "logs"),
    }


@pytest.mark.integration()
def test_health_check_succeeds_against_localstack(tmp_path: Path, localstack_service: None) -> None:
    _ = localstack_service
    settings = AppSettings.from_env(_integration_env(tmp_path))
    log_file = configure_logging(settings)

    report = run_health_check(settings, log_file)

    assert report.status == "ok"
    assert log_file.exists()


@pytest.mark.integration()
def test_s3_client_supports_object_round_trip(tmp_path: Path, localstack_service: None) -> None:
    _ = localstack_service
    settings = AppSettings.from_env(_integration_env(tmp_path))
    client = build_s3_client(settings)
    key = "integration/probe.txt"
    body = b"s3-archiver"

    _ = client.put_object(Bucket=settings.bucket, Key=key, Body=body)
    response = client.get_object(Bucket=settings.bucket, Key=key)
    payload = response["Body"].read()

    assert payload == body
