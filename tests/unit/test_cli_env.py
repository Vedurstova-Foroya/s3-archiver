"""Tests for CLI runtime environment loading."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.health import HealthReport
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

RUNNER = CliRunner()
PARSE_ENV_FILE = cast(Callable[[Path], dict[str, str]], cli_module.__dict__["_parse_env_file"])
LOAD_RUNTIME_ENV = cast(Callable[[], dict[str, str]], cli_module.__dict__["_load_runtime_env"])


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
        assert settings.bucket == "archive-bucket"
        assert settings.log_dir == tmp_path / "logs"
        return tmp_path / "s3-archiver.log"

    def run_check(settings: AppSettings, log_file: Path) -> HealthReport:
        return HealthReport(
            status="ok",
            provider=settings.provider.value,
            bucket=settings.bucket,
            endpoint_url=settings.resolved_endpoint_url(),
            log_file=str(log_file),
            checked_at="2026-04-09T17:00:43+00:00",
        )

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
            "S3_BUCKET": "bucket-from-process-env",
        },
    )

    def configure(settings: AppSettings) -> Path:
        assert settings.bucket == "bucket-from-process-env"
        return tmp_path / "s3-archiver.log"

    def run_check(settings: AppSettings, log_file: Path) -> HealthReport:
        return HealthReport(
            status="ok",
            provider=settings.provider.value,
            bucket=settings.bucket,
            endpoint_url=settings.resolved_endpoint_url(),
            log_file=str(log_file),
            checked_at="2026-04-09T17:00:43+00:00",
        )

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", run_check)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 0
    payload = _load_payload(result.stdout)
    assert payload.get("bucket") == "bucket-from-process-env"


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
def test_load_runtime_env_returns_process_env_when_env_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_file = tmp_path / ".missing.env"
    monkeypatch.setattr(os, "environ", {"ENV_FILE": str(missing_file), "S3_BUCKET": "from-process"})

    loaded = LOAD_RUNTIME_ENV()

    assert loaded["S3_BUCKET"] == "from-process"


def _env_file_contents(tmp_path: Path) -> str:
    return "\n".join(
        (
            "S3_PROVIDER=oci",
            "S3_ACCESS_KEY_ID=access-key",
            "S3_SECRET_ACCESS_KEY=secret-key",
            "S3_REGION=eu-frankfurt-1",
            "S3_NAMESPACE=tenant",
            "S3_BUCKET=archive-bucket",
            "OCI_IAM_USER_OCID=ocid1.user.oc1..example",
            "S3_ADDRESSING_STYLE=path",
            "LOG_LEVEL=INFO",
            f"LOG_DIR={tmp_path / 'logs'}",
        )
    )


def _load_payload(output: str) -> HealthPayload:
    return cast(HealthPayload, json.loads(output))
