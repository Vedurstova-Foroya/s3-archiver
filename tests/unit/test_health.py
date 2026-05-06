"""Tests for health-check execution."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from mypy_boto3_s3.client import S3Client
from s3_archiver_core.errors import HealthCheckError
from s3_archiver_core.health import run_health_check
from s3_archiver_core.settings import AppSettings


class SuccessfulClient:
    """Minimal client double for successful requests."""

    called_bucket: str | None = None

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        self.called_bucket = Bucket


class AuthFailingClient:
    """Minimal client double for authentication failures."""

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        _ = Bucket
        raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "HeadBucket")


class ConnectivityFailingClient:
    """Minimal client double for connectivity failures."""

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        _ = Bucket
        raise EndpointConnectionError(endpoint_url="http://localstack:4566")


@pytest.mark.unit()
def test_run_health_check_reports_success(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    settings = AppSettings.from_env(base_env)
    client = SuccessfulClient()

    def build_client(_: AppSettings) -> S3Client:
        return cast(S3Client, cast(object, client))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    report = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")

    assert client.called_bucket == "archive-bucket"
    assert report.status == "ok"


@pytest.mark.unit()
def test_run_health_check_raises_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)

    def build_client(_: AppSettings) -> S3Client:
        return cast(S3Client, cast(object, AuthFailingClient()))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    with pytest.raises(HealthCheckError, match="denied"):
        _ = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")


@pytest.mark.unit()
def test_run_health_check_raises_on_connectivity_error(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)

    def build_client(_: AppSettings) -> S3Client:
        return cast(S3Client, cast(object, ConnectivityFailingClient()))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    with pytest.raises(HealthCheckError, match="Could not connect"):
        _ = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")
