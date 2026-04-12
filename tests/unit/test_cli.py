"""Tests for the CLI entrypoint."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.health import HealthReport
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

RUNNER = CliRunner()


@pytest.mark.unit()
def test_check_command_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

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

    result = RUNNER.invoke(cli_module.app, ["check", "--json"])

    assert result.exit_code == 0
    assert '"status": "ok"' in result.stdout


@pytest.mark.unit()
def test_check_command_exits_non_zero_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(_: dict[str, str]) -> AppSettings:
        raise ConfigError("bad env")

    monkeypatch.setattr(AppSettings, "from_env", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 1
    assert "bad env" in result.stderr
