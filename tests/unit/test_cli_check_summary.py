"""Tests for the human-readable ``check`` success summary."""

from __future__ import annotations

import pytest
from s3_archiver_cli.cli_payloads import emit_check_success
from s3_archiver_core.health import HealthReport, RouteHealthReport


def _route(name: str, parser: str) -> RouteHealthReport:
    return RouteHealthReport(
        name=name,
        source_provider="localstack",
        source_bucket="src",
        source_endpoint_url="http://localstack:4566",
        source_path="data/",
        destination_provider="localstack",
        destination_bucket="dst",
        destination_endpoint_url="http://localstack:4566",
        destination_path="data/",
        parser=parser,
        copy_mode="daily_tar_gz",
        source_versioning="Enabled",
    )


def _report(routes: tuple[RouteHealthReport, ...]) -> HealthReport:
    return HealthReport(
        status="ok",
        source_provider="localstack",
        source_bucket="src",
        source_endpoint_url="http://localstack:4566",
        source_versioning="Enabled",
        destination_provider="localstack",
        destination_bucket="dst",
        destination_endpoint_url="http://localstack:4566",
        log_file="/tmp/s3-archiver.log",
        checked_at="2026-04-09T17:00:43+00:00",
        route_count=len(routes),
        routes=routes,
    )


@pytest.mark.unit()
def test_emit_check_success_prints_one_line_per_route(capsys: pytest.CaptureFixture[str]) -> None:
    report = _report(
        (
            _route("harmonie", "filename_timestamp"),
            _route("harmonie-download", "filename_timestamp"),
            _route("fae", "folder_timestamp"),
        )
    )

    emit_check_success(report)

    assert capsys.readouterr().err.strip().splitlines() == [
        "harmonie (filename_timestamp) check success",
        "harmonie-download (filename_timestamp) check success",
        "fae (folder_timestamp) check success",
        "check success",
    ]


@pytest.mark.unit()
def test_emit_check_success_ends_with_summary_when_no_routes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_check_success(_report(()))

    assert capsys.readouterr().err.strip().splitlines() == ["check success"]
