"""Tests for CLI runtime environment loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_cli.env import load_runtime_env, parse_env_file
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.health import HealthReport
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

RUNNER = CliRunner()
PARSE_ENV_FILE = parse_env_file
LOAD_RUNTIME_ENV = load_runtime_env


class HealthPayload(TypedDict):
    """Typed CLI health payload."""

    status: str
    message: str
    bucket: NotRequired[str]


@pytest.mark.unit()
def test_check_command_loads_default_dotenv_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _ = (tmp_path / ".env").write_text(_env_file_contents(tmp_path), encoding="utf-8")
    monkeypatch.setattr(os, "environ", {})

    def configure(settings: AppSettings) -> Path:
        assert settings.routes[0].source.bucket == "archive-bucket"
        assert settings.log_dir == tmp_path / "logs"
        return tmp_path / "s3-archiver.log"

    def run_check(settings: AppSettings, log_file: Path) -> HealthReport:
        return _health_report(settings, log_file)

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", run_check)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 0
    payload = _load_payload(result.stdout)
    assert payload["status"] == "ok"


@pytest.mark.unit()
def test_check_command_prefers_process_env_over_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.override"
    _ = env_file.write_text(_env_file_contents(tmp_path), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        os,
        "environ",
        {
            "ENV_FILE": str(env_file),
            "S3_SOURCE_BUCKET": "bucket-from-process-env",
        },
    )

    def configure(settings: AppSettings) -> Path:
        assert settings.routes[0].source.bucket == "bucket-from-process-env"
        return tmp_path / "s3-archiver.log"

    def run_check(settings: AppSettings, log_file: Path) -> HealthReport:
        return _health_report(settings, log_file)

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", run_check)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 0
    payload = _load_payload(result.stdout)
    assert payload.get("source_bucket") == "bucket-from-process-env"


@pytest.mark.unit()
def test_parse_env_file_supports_comments_exports_and_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "\n".join(
            (
                "",
                "# comment",
                "export S3_BUCKET=archive-bucket",
                'LOG_DIR="/var/log/s3-archiver"',
                "LOG_LEVEL='INFO'",
            )
        ),
        encoding="utf-8",
    )

    parsed = PARSE_ENV_FILE(env_file)

    assert parsed == {
        "S3_BUCKET": "archive-bucket",
        "LOG_DIR": "/var/log/s3-archiver",
        "LOG_LEVEL": "INFO",
    }


@pytest.mark.unit()
def test_parse_env_file_rejects_invalid_assignment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text("export =broken", encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid env assignment"):
        _ = PARSE_ENV_FILE(env_file)


@pytest.mark.unit()
def test_parse_env_file_supports_multi_line_quoted_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "\n".join(
            (
                "ARCHIVER_CONFIG_JSON='[",
                "  {",
                '    "name": "demo"',
                "  }",
                "]'",
                "LOG_LEVEL=INFO",
            )
        ),
        encoding="utf-8",
    )

    parsed = PARSE_ENV_FILE(env_file)

    assert parsed["LOG_LEVEL"] == "INFO"
    assert parsed["ARCHIVER_CONFIG_JSON"] == '[\n  {\n    "name": "demo"\n  }\n]'


@pytest.mark.unit()
def test_parse_env_file_rejects_unterminated_quoted_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text("ARCHIVER_CONFIG_JSON='[\n  {\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Unterminated quoted value for ARCHIVER_CONFIG_JSON"):
        _ = PARSE_ENV_FILE(env_file)


@pytest.mark.unit()
def test_checked_in_env_example_is_valid_route_config() -> None:
    env = PARSE_ENV_FILE(Path(".env.example"))

    settings = AppSettings.from_env(env)

    assert settings.routes[0].source.provider.value == "custom"
    assert settings.routes[0].source.endpoint_url == "https://s3.example.com"


@pytest.mark.unit()
def test_load_runtime_env_returns_process_env_when_env_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_file = tmp_path / ".missing.env"
    monkeypatch.setattr(os, "environ", {"ENV_FILE": str(missing_file), "S3_BUCKET": "from-process"})

    loaded = LOAD_RUNTIME_ENV()

    assert loaded["S3_BUCKET"] == "from-process"


@pytest.mark.unit()
def test_load_runtime_env_honors_explicit_dev_null_over_default_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _ = (tmp_path / ".env").write_text("S3_BUCKET=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(os, "environ", {"APP_ENV_FILE": "/dev/null"})

    loaded = LOAD_RUNTIME_ENV()

    assert "S3_BUCKET" not in loaded


def _env_file_contents(tmp_path: Path) -> str:
    return "\n".join(
        (
            "S3_SOURCE_PROVIDER=oci",
            "S3_SOURCE_ACCESS_KEY=access-key",
            "S3_SOURCE_SECRET_KEY=secret-key",
            "S3_SOURCE_REGION=eu-frankfurt-1",
            "S3_SOURCE_NAMESPACE=tenant",
            "S3_SOURCE_BUCKET=archive-bucket",
            "S3_SOURCE_IAM_USER_OCID=ocid1.user.oc1..example",
            "S3_SOURCE_ENDPOINT=https://tenant.compat.objectstorage.eu-frankfurt-1.oraclecloud.com",
            "S3_SOURCE_ADDRESSING_STYLE=path",
            "S3_DESTINATION_PROVIDER=localstack",
            "S3_DESTINATION_ACCESS_KEY=destination-access",
            "S3_DESTINATION_SECRET_KEY=destination-secret",
            "S3_DESTINATION_REGION=us-east-1",
            "S3_DESTINATION_BUCKET=destination-bucket",
            "S3_DESTINATION_ENDPOINT=http://localstack:4566",
            "S3_DESTINATION_ADDRESSING_STYLE=path",
            (
                'ARCHIVER_CONFIG_JSON=[{"name":"default","parser":"filename_timestamp",'
                '"copy_mode":"daily_tar_gz",'
                '"source":{"provider":"${S3_SOURCE_PROVIDER}",'
                '"endpoint_url":"${S3_SOURCE_ENDPOINT}",'
                '"region":"${S3_SOURCE_REGION}","namespace":"${S3_SOURCE_NAMESPACE}",'
                '"bucket":"${S3_SOURCE_BUCKET}",'
                '"iam_user_ocid":"${S3_SOURCE_IAM_USER_OCID}",'
                '"path":"","access_key_id":"${S3_SOURCE_ACCESS_KEY}",'
                '"secret_access_key":"${S3_SOURCE_SECRET_KEY}",'
                '"addressing_style":"${S3_SOURCE_ADDRESSING_STYLE}"},'
                '"destination":{"provider":"${S3_DESTINATION_PROVIDER}",'
                '"endpoint_url":"${S3_DESTINATION_ENDPOINT}",'
                '"region":"${S3_DESTINATION_REGION}",'
                '"bucket":"${S3_DESTINATION_BUCKET}","path":"",'
                '"access_key_id":"${S3_DESTINATION_ACCESS_KEY}",'
                '"secret_access_key":"${S3_DESTINATION_SECRET_KEY}",'
                '"addressing_style":"${S3_DESTINATION_ADDRESSING_STYLE}"}}]'
            ),
            "LOG_LEVEL=INFO",
            f"LOG_DIR={tmp_path / 'logs'}",
        )
    )


def _load_payload(output: str) -> HealthPayload:
    return cast(HealthPayload, json.loads(output))


def _health_report(settings: AppSettings, log_file: Path) -> HealthReport:
    return HealthReport(
        status="ok",
        source_provider=settings.routes[0].source.provider.value,
        source_bucket=settings.routes[0].source.bucket,
        source_endpoint_url=settings.routes[0].source.resolved_endpoint_url(),
        source_versioning="Enabled",
        destination_provider=settings.routes[0].destination.provider.value,
        destination_bucket=settings.routes[0].destination.bucket,
        destination_endpoint_url=settings.routes[0].destination.resolved_endpoint_url(),
        log_file=str(log_file),
        checked_at="2026-04-09T17:00:43+00:00",
        route_count=len(settings.routes),
    )
