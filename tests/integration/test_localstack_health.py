"""Integration tests against LocalStack S3."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Literal, cast

import pytest
import s3_archiver_cli.main as cli_module
from botocore.response import StreamingBody
from mypy_boto3_s3.type_defs import VersioningConfigurationTypeDef
from s3_archiver_core.health import run_health_check
from s3_archiver_core.logging_config import configure_logging
from s3_archiver_core.s3 import VersioningState, build_s3_client
from s3_archiver_core.settings import AppSettings
from s3_archiver_localstack_support.compose import find_repo_root
from s3_archiver_localstack_support.harness import (
    LOCALSTACK_HOST_ENDPOINT,
    LocalstackBucketPair,
    assert_localstack_test_target,
    bucket_pair_from_env,
    localstack_test_env,
)
from s3_archiver_localstack_support.objects import listed_keys
from typer.testing import CliRunner

REPO_ROOT = find_repo_root()
INTEGRATION_RUNTIME_LOG_DIR = (
    REPO_ROOT / ".local" / "integration-runtime" / "var" / "log" / "s3-archiver"
)
RUNNER = CliRunner()


def _integration_env(bucket_pair: LocalstackBucketPair) -> dict[str, str]:
    endpoint = os.environ.get("LOCALSTACK_S3_URL", LOCALSTACK_HOST_ENDPOINT)
    return localstack_test_env(
        bucket_pair, endpoint=endpoint, log_dir=str(INTEGRATION_RUNTIME_LOG_DIR)
    )


@pytest.mark.integration()
def test_health_check_succeeds_against_isolated_localstack_buckets(
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    _reset_integration_runtime_log_dir()
    settings = AppSettings.from_env(_integration_env(localstack_bucket_pair))
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
            and record.get("bucket") == settings.routes[0].source.bucket
            and record.get("endpoint_url") == settings.routes[0].source.resolved_endpoint_url()
            for record in records
        )
        assert any(
            record.get("event") == "health.succeeded"
            and record.get("bucket") == settings.routes[0].source.bucket
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
def test_localstack_service_readiness_requires_only_s3_api(
    compose_env: dict[str, str],
    localstack_service: None,
) -> None:
    _ = localstack_service
    bucket_pair = bucket_pair_from_env(compose_env)
    settings = AppSettings.from_env(_integration_env(bucket_pair))
    client = build_s3_client(settings.routes[0].source)
    list_buckets = client.list_buckets()
    bucket_names = {
        str(bucket["Name"])
        for bucket in cast(list[dict[str, object]], list_buckets.get("Buckets", []))
        if "Name" in bucket
    }

    assert bucket_pair.source not in bucket_names
    assert bucket_pair.destination not in bucket_names


@pytest.mark.integration()
def test_s3_client_supports_bucket_listing(
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    settings = AppSettings.from_env(_integration_env(localstack_bucket_pair))
    client = build_s3_client(settings.routes[0].source)
    key = "integration/listing-probe.txt"
    body = b"listed"

    _ = client.put_object(Bucket=settings.routes[0].source.bucket, Key=key, Body=body)

    assert key in listed_keys(client, settings.routes[0].source.bucket)


@pytest.mark.integration()
def test_s3_client_supports_isolated_object_round_trip(
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    settings = AppSettings.from_env(_integration_env(localstack_bucket_pair))
    client = build_s3_client(settings.routes[0].source)
    key = "integration/probe.txt"
    body = b"s3-archiver"

    _ = client.put_object(Bucket=settings.routes[0].source.bucket, Key=key, Body=body)
    response = client.get_object(Bucket=settings.routes[0].source.bucket, Key=key)
    payload = cast(StreamingBody, response["Body"]).read()

    assert payload == body


@pytest.mark.integration()
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:4566",
        "http://localstack:4566",
        "http://localhost.localstack.cloud:4566",
    ],
)
def test_localstack_guard_accepts_known_localstack_hosts(
    localstack_bucket_pair: LocalstackBucketPair,
    endpoint: str,
) -> None:
    safe_env = _integration_env(localstack_bucket_pair)
    safe_env["S3_SOURCE_ENDPOINT"] = endpoint
    safe_env["S3_DESTINATION_ENDPOINT"] = endpoint

    assert_localstack_test_target(safe_env)


@pytest.mark.integration()
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("S3_DESTINATION_ENDPOINT", "https://s3.amazonaws.com", "not allowed"),
        ("S3_SOURCE_ENDPOINT", None, "must be set"),
    ],
)
def test_localstack_guard_rejects_unsafe_endpoint_configuration(
    localstack_bucket_pair: LocalstackBucketPair,
    field: str,
    value: str | None,
    match: str,
) -> None:
    unsafe_env = _integration_env(localstack_bucket_pair)
    if value is None:
        _ = unsafe_env.pop(field)
    else:
        unsafe_env[field] = value

    with pytest.raises(RuntimeError, match=match):
        assert_localstack_test_target(unsafe_env)


@pytest.mark.integration()
def test_check_command_rejects_runtime_localstack_endpoint_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _integration_env(localstack_bucket_pair)
    env["S3_DESTINATION_ENDPOINT"] = "https://s3.amazonaws.com"
    monkeypatch.setattr(os, "environ", env)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.CONFIG_ERROR_EXIT_CODE
    payload = cast(dict[str, object], json.loads(result.stderr))
    assert payload["status"] == "error"
    assert payload["field"] == "ARCHIVER_CONFIG_JSON[0].destination.endpoint_url"


@pytest.mark.integration()
@pytest.mark.parametrize(
    ("status", "expected_state"),
    [(None, "Disabled"), ("Enabled", "Enabled"), ("Suspended", "Suspended")],
)
def test_health_check_succeeds_for_source_bucket_versioning_states(
    localstack_bucket_pair: LocalstackBucketPair,
    status: Literal["Enabled", "Suspended"] | None,
    expected_state: VersioningState,
) -> None:
    settings = AppSettings.from_env(_integration_env(localstack_bucket_pair))
    client = build_s3_client(settings.routes[0].source)
    if status is not None:
        configuration: VersioningConfigurationTypeDef = {"Status": status}
        _ = client.put_bucket_versioning(
            Bucket=settings.routes[0].source.bucket,
            VersioningConfiguration=configuration,
        )

    raw_state = client.get_bucket_versioning(Bucket=settings.routes[0].source.bucket).get("Status")
    state: VersioningState = (
        cast(VersioningState, raw_state) if raw_state in {"Enabled", "Suspended"} else "Disabled"
    )
    report = run_health_check(settings, settings.log_dir / "s3-archiver.log")

    assert state == expected_state
    assert report.status == "ok"
    assert (report.source_bucket, report.source_versioning) == (
        settings.routes[0].source.bucket,
        expected_state,
    )


@pytest.mark.integration()
def test_check_command_rejects_same_localstack_bucket_with_dual_credentials(
    monkeypatch: pytest.MonkeyPatch,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _integration_env(localstack_bucket_pair)
    env["S3_DESTINATION_BUCKET"] = localstack_bucket_pair.source
    _set_route_destination_bucket(env, localstack_bucket_pair.source)
    monkeypatch.setattr(os, "environ", env)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.CONFIG_ERROR_EXIT_CODE
    payload = cast(dict[str, object], json.loads(result.stderr))
    assert payload["status"] == "error"
    assert payload["field"] == "route"
    assert "source and destination storage locations must differ" in str(payload["message"])


def _set_route_destination_bucket(env: dict[str, str], bucket: str) -> None:
    routes = cast(list[dict[str, object]], json.loads(env["ARCHIVER_CONFIG_JSON"]))
    route = routes[0]
    destination = cast(dict[str, object], route["destination"])
    destination["bucket"] = bucket
    env["ARCHIVER_CONFIG_JSON"] = json.dumps(routes)


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
