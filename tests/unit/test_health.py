"""Tests for health-check execution."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from mypy_boto3_s3.client import S3Client
from s3_archiver_core.errors import HealthCheckError
from s3_archiver_core.health import run_health_check
from s3_archiver_core.settings import AppSettings, S3LocationSettings

from tests.unit.health_helpers import (
    AuthFailingClient,
    ConnectivityFailingClient,
    SuccessfulClient,
    VersioningFailingClient,
    multi_route_env,
)


@pytest.mark.unit()
def test_run_health_check_reports_success(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [SuccessfulClient(), SuccessfulClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    report = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")

    assert report.source_bucket == "archive-bucket"
    assert report.destination_bucket == "destination-bucket"
    assert report.source_versioning == "Enabled"
    assert clients == []
    assert report.status == "ok"
    assert report.route_count == 1


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("raw_status", "expected_status"),
    [
        ("Suspended", "Suspended"),
        (None, "Disabled"),
    ],
)
def test_run_health_check_reports_non_enabled_source_versioning_states(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    raw_status: str | None,
    expected_status: str,
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [SuccessfulClient(raw_status), SuccessfulClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    report = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")

    assert report.source_versioning == expected_status


@pytest.mark.unit()
def test_run_health_check_validates_all_configured_routes(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    env = multi_route_env(base_env)
    settings = AppSettings.from_env(env)
    clients = [
        SuccessfulClient(),
        SuccessfulClient(),
        SuccessfulClient("Suspended"),
        SuccessfulClient(),
    ]
    built_buckets: list[str] = []

    def build_client(location: S3LocationSettings) -> S3Client:
        built_buckets.append(location.bucket)
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    report = run_health_check(settings, Path(env["LOG_DIR"]) / "s3-archiver.log")
    payload = report.as_dict()

    assert report.source_versioning == "Enabled"
    assert report.route_count == 2
    assert payload["source_bucket"] == "archive-bucket"
    assert payload["destination_bucket"] == "destination-bucket"
    assert payload["source_versioning"] == "Enabled"
    assert payload["route_count"] == "2"
    assert payload["routes"] == [
        {
            "name": "default",
            "source_provider": "oci",
            "source_bucket": "archive-bucket",
            "source_endpoint_url": (
                "https://tenant.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"
            ),
            "source_path": "",
            "destination_provider": "localstack",
            "destination_bucket": "destination-bucket",
            "destination_endpoint_url": "http://localstack:4566",
            "destination_path": "",
            "parser": "filename_timestamp",
            "copy_mode": "daily_tar_gz",
            "source_versioning": "Enabled",
            "parser_sample_count": 1,
            "parser_match_count": 1,
            "parser_skip_examples": [],
        },
        {
            "name": "secondary",
            "source_provider": "oci",
            "source_bucket": "second-source-bucket",
            "source_endpoint_url": (
                "https://tenant.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"
            ),
            "source_path": "raw/",
            "destination_provider": "localstack",
            "destination_bucket": "second-destination-bucket",
            "destination_endpoint_url": "http://localstack:4566",
            "destination_path": "mirror/",
            "parser": "direct",
            "copy_mode": "direct",
            "source_versioning": "Suspended",
            "parser_sample_count": 1,
            "parser_match_count": 1,
            "parser_skip_examples": [],
        },
    ]
    assert built_buckets == [
        "archive-bucket",
        "destination-bucket",
        "second-source-bucket",
        "second-destination-bucket",
    ]
    assert clients == []


@pytest.mark.unit()
def test_run_health_check_raises_for_later_route_source_access_error(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    env = multi_route_env(base_env)
    settings = AppSettings.from_env(env)
    clients = [SuccessfulClient(), SuccessfulClient(), AuthFailingClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    with pytest.raises(HealthCheckError, match="route 'secondary' source bucket"):
        _ = run_health_check(settings, Path(env["LOG_DIR"]) / "s3-archiver.log")


@pytest.mark.unit()
def test_run_health_check_raises_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)

    def build_client(_: S3LocationSettings) -> S3Client:
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

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, ConnectivityFailingClient()))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    with pytest.raises(HealthCheckError, match="Could not connect"):
        _ = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")


@pytest.mark.unit()
def test_run_health_check_raises_on_destination_access_error(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [SuccessfulClient(), AuthFailingClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    with pytest.raises(HealthCheckError, match="destination bucket"):
        _ = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")


@pytest.mark.unit()
def test_run_health_check_raises_on_source_versioning_error(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [VersioningFailingClient(), SuccessfulClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr(
        "s3_archiver_core.health.build_s3_client",
        build_client,
    )

    with pytest.raises(HealthCheckError, match="source bucket versioning"):
        _ = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")
