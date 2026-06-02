"""Tests for the parser-compatibility step of the health check.

The check command samples up to PARSER_SAMPLE_LIMIT keys from each route's
source path and verifies the configured parser would actually select them.
This catches misconfigured parsers (e.g. folder_timestamp on a layout that
doesn't have YYYY/MM/DD folders) at check time instead of producing silent
zero-archive runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, final, override

import pytest
from mypy_boto3_s3.client import S3Client
from s3_archiver_core.errors import HealthCheckError
from s3_archiver_core.health import PARSER_SAMPLE_LIMIT, run_health_check
from s3_archiver_core.parsers.kinds import ParserKind
from s3_archiver_core.parsers.protocol import ParserContext, ParserListedObject
from s3_archiver_core.parsers.results import SelectedObject, SkippedObject
from s3_archiver_core.settings import AppSettings, S3LocationSettings

from tests.unit.archive_s3_fakes import client_error
from tests.unit.health_helpers import SuccessfulClient


def _item(key: str) -> dict[str, object]:
    return {"Key": key, "Size": 1, "LastModified": datetime(2024, 1, 1, tzinfo=UTC)}


def _version_item(key: str) -> dict[str, object]:
    return _item(key) | {"IsLatest": True, "VersionId": "v1"}


class EmptySourceClient(SuccessfulClient):
    """Successful client whose source listing returns no objects."""

    @override
    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {"Contents": [], "IsTruncated": False}

    @override
    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {"Versions": [], "IsTruncated": False}


class UnmatchedSourceClient(SuccessfulClient):
    """Successful client whose source listing returns parser-incompatible keys."""

    @override
    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {
            "Contents": [_item("no-date-here.txt"), _item("also-undated.bin")],
            "IsTruncated": False,
        }

    @override
    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {
            "Versions": [_version_item("no-date-here.txt"), _version_item("also-undated.bin")],
            "IsTruncated": False,
        }


@pytest.mark.unit()
def test_parser_check_passes_when_source_is_empty(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [EmptySourceClient(), SuccessfulClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr("s3_archiver_core.health.build_s3_client", build_client)

    report = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")

    assert report.status == "ok"
    assert report.routes[0].parser_sample_count == 0
    assert report.routes[0].parser_match_count == 0
    assert report.routes[0].parser_skip_examples == ()


@pytest.mark.unit()
def test_parser_check_fails_when_no_sampled_key_matches(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [UnmatchedSourceClient(), SuccessfulClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr("s3_archiver_core.health.build_s3_client", build_client)

    with pytest.raises(HealthCheckError, match="matched 0 of 2 sampled object"):
        _ = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")


@pytest.mark.unit()
def test_parser_check_records_match_when_keys_have_timestamps(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [
        SuccessfulClient(
            sample_keys=(
                "2024-01-01T00:00:00Z_a.bin",
                "2024-01-02T00:00:00Z_b.bin",
                "no-date.bin",
            )
        ),
        SuccessfulClient(),
    ]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr("s3_archiver_core.health.build_s3_client", build_client)

    report = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")

    route = report.routes[0]
    assert report.status == "ok"
    assert route.parser_sample_count == 3
    assert route.parser_match_count == 2
    assert len(route.parser_skip_examples) == 1
    assert "no-date.bin" in route.parser_skip_examples[0]


@pytest.mark.unit()
def test_parser_check_caps_sample_count_and_skip_examples(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    matched = tuple(f"2024-01-01T00:00:{index:02d}Z_a.bin" for index in range(40))
    unmatched = tuple(f"undated-{index:02d}.bin" for index in range(10))
    settings = AppSettings.from_env(base_env)
    clients = [
        SuccessfulClient(sample_keys=unmatched + matched),
        SuccessfulClient(),
    ]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr("s3_archiver_core.health.build_s3_client", build_client)

    report = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")

    route = report.routes[0]
    assert route.parser_sample_count == PARSER_SAMPLE_LIMIT
    assert route.parser_match_count == PARSER_SAMPLE_LIMIT - len(unmatched)
    assert len(route.parser_skip_examples) == 3


class ListErrorClient(SuccessfulClient):
    """Successful client whose source listing raises a botocore ClientError."""

    @override
    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        raise client_error("ServiceUnavailable", status=503)

    @override
    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        raise client_error("ServiceUnavailable", status=503)


@pytest.mark.unit()
def test_parser_check_raises_when_source_listing_fails(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [ListErrorClient(), SuccessfulClient()]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr("s3_archiver_core.health.build_s3_client", build_client)

    with pytest.raises(HealthCheckError, match="Failed to sample source objects for route"):
        _ = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")


@final
class _RaisingThenMatchingParser:
    """Parser that raises ValueError on the first key, then matches everything else."""

    _calls: int

    def __init__(self) -> None:
        self._calls = 0

    def parse(
        self, listed: ParserListedObject, context: ParserContext
    ) -> SelectedObject | SkippedObject:
        _ = context
        self._calls += 1
        if self._calls == 1:
            raise ValueError("synthetic parser failure")
        return SelectedObject(listed.last_modified, "last_modified", "")


def _build_raising_parser(_kind: ParserKind) -> _RaisingThenMatchingParser:
    return _RaisingThenMatchingParser()


@pytest.mark.unit()
def test_parser_check_records_value_error_as_skip_example(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]
) -> None:
    settings = AppSettings.from_env(base_env)
    clients = [
        SuccessfulClient(sample_keys=("first.bin", "second.bin")),
        SuccessfulClient(),
    ]

    def build_client(_: S3LocationSettings) -> S3Client:
        return cast(S3Client, cast(object, clients.pop(0)))

    monkeypatch.setattr("s3_archiver_core.health.build_s3_client", build_client)
    monkeypatch.setattr("s3_archiver_core.health.parser_for_kind", _build_raising_parser)

    report = run_health_check(settings, Path(base_env["LOG_DIR"]) / "s3-archiver.log")

    route = report.routes[0]
    assert route.parser_sample_count == 2
    assert route.parser_match_count == 1
    assert len(route.parser_skip_examples) == 1
    assert "synthetic parser failure" in route.parser_skip_examples[0]
    assert "first.bin" in route.parser_skip_examples[0]
