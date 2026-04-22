"""Tests for the CLI entrypoint."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict, cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.health import HealthReport
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

RUNNER = CliRunner()


class HealthPayload(TypedDict):
    """Typed CLI health payload."""

    status: str
    message: str


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

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 0
    payload = _load_payload(result.stdout)
    assert payload["status"] == "ok"


@pytest.mark.unit()
def test_check_command_exits_non_zero_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(_: dict[str, str]) -> AppSettings:
        raise ConfigError("bad env")

    monkeypatch.setattr(AppSettings, "from_env", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 1
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload["message"] == "bad env"


@pytest.mark.unit()
def test_main_runs_typer_application(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_app() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(cli_module, "app", fake_app)

    cli_module.main()

    assert called is True


def _load_payload(output: str) -> HealthPayload:
    return cast(HealthPayload, json.loads(output))
