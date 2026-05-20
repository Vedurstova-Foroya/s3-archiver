"""Tests for the CLI entrypoint."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.errors import ConfigError, HealthCheckError, LoggingError, S3ArchiverError
from s3_archiver_core.health import HealthReport
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

RUNNER = CliRunner()


class HealthPayload(TypedDict):
    """Typed CLI health payload."""

    status: str
    message: str
    phase: NotRequired[str]
    field: NotRequired[str | None]
    bucket: NotRequired[str]


@pytest.mark.unit()
def test_check_command_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def run_check(settings: AppSettings, log_file: Path) -> HealthReport:
        return _health_report(settings, log_file)

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", run_check)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 0
    payload = _load_payload(result.stdout)
    assert payload["status"] == "ok"
    working_set = cast(dict[str, object], cast(object, _load_payload(result.stderr)))
    assert working_set["event"] == "startup.working_set"
    assert "access-key" not in result.stderr
    assert "secret-key" not in result.stderr
    assert "destination-secret" not in result.stderr


@pytest.mark.unit()
def test_check_command_prepares_runtime_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    prepared: list[Path] = []

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def prepare_temp_dir(temp_dir: Path) -> None:
        prepared.append(temp_dir)

    def run_check(settings: AppSettings, log_file: Path) -> HealthReport:
        return _health_report(settings, log_file)

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "prepare_runtime_temp_dir", prepare_temp_dir)
    monkeypatch.setattr(cli_module, "run_health_check", run_check)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 0
    assert prepared == [AppSettings.from_env(base_env).temp_dir]


@pytest.mark.unit()
def test_check_command_uses_config_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(_: dict[str, str]) -> AppSettings:
        raise ConfigError("bad env")

    monkeypatch.setattr(AppSettings, "from_env", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.CONFIG_ERROR_EXIT_CODE
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload["message"] == "bad env"
    assert payload.get("phase") == "startup.env_validation"


@pytest.mark.unit()
def test_check_command_uses_config_exit_code_for_invalid_provider(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    base_env["S3_SOURCE_PROVIDER"] = "broken"
    monkeypatch.setattr(os, "environ", base_env)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.CONFIG_ERROR_EXIT_CODE
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert "ARCHIVER_CONFIG_JSON[0].source.provider" in payload["message"]
    assert payload.get("field") == "ARCHIVER_CONFIG_JSON[0].source.provider"


@pytest.mark.unit()
def test_check_command_uses_logging_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def raise_error(_: AppSettings) -> Path:
        raise LoggingError("log sink failed")

    monkeypatch.setattr(cli_module, "configure_logging", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.LOGGING_ERROR_EXIT_CODE
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload["message"] == "log sink failed"
    assert payload.get("field") == "logging"


@pytest.mark.unit()
def test_check_command_uses_health_exit_code_for_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def raise_error(_: AppSettings, _log_file: Path) -> HealthReport:
        raise HealthCheckError("auth failed: denied")

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.HEALTH_CHECK_ERROR_EXIT_CODE
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload["message"] == "auth failed: denied"
    assert payload.get("phase") == "startup.preflight"
    assert payload.get("field") == "s3_connectivity"
    assert payload.get("source_bucket") == "archive-bucket"
    assert payload.get("destination_bucket") == "destination-bucket"


@pytest.mark.unit()
def test_check_command_uses_health_exit_code_for_connectivity_failure(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def raise_error(_: AppSettings, _log_file: Path) -> HealthReport:
        raise HealthCheckError("connectivity failed: endpoint unavailable")

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.HEALTH_CHECK_ERROR_EXIT_CODE
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload["message"] == "connectivity failed: endpoint unavailable"
    assert payload.get("field") == "s3_connectivity"


@pytest.mark.unit()
def test_check_command_reports_failed_preflight_check_field(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def raise_error(_: AppSettings, _log_file: Path) -> HealthReport:
        raise HealthCheckError("Failed to access destination bucket 'destination-bucket': denied")

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == cli_module.HEALTH_CHECK_ERROR_EXIT_CODE
    payload = _load_payload(result.stderr)
    assert payload.get("field") == "destination_bucket_access"


@pytest.mark.unit()
def test_check_command_uses_generic_exit_code_for_unknown_domain_error(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    class UnknownDomainError(S3ArchiverError):
        """Test-only domain error to exercise fallback exit handling."""

    monkeypatch.setattr(os, "environ", base_env)

    def raise_error(_: AppSettings) -> Path:
        raise UnknownDomainError("unexpected failure")

    monkeypatch.setattr(cli_module, "configure_logging", raise_error)

    result = RUNNER.invoke(cli_module.app, ["check"])

    assert result.exit_code == 1
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload["message"] == "unexpected failure"


@pytest.mark.unit()
def test_bare_command_prints_help_and_exits_zero() -> None:
    result = RUNNER.invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "check" in result.stdout
    assert "archive" in result.stdout
    assert "cleanup-preview" not in result.stdout
    assert "demo-cleanup" not in result.stdout


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
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = cast(object, json.loads(stripped))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return cast(HealthPayload, cast(object, payload))
    raise AssertionError(f"expected JSON payload in output: {output!r}")


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
