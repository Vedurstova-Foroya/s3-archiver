"""Tests for CLI archive command wiring."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import AppSettings, S3LocationSettings
from typer.testing import CliRunner

from tests.unit.cli_archive_test_support import archive_result as _archive_result
from tests.unit.cli_archive_test_support import load_archive_payload as _load_payload

RUNNER = CliRunner()


@pytest.mark.unit()
def test_archive_command_uses_timeout_enforced_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    tmp_path: Path,
) -> None:
    base_env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr(os, "environ", base_env)
    recorded_logs: list[Path] = []
    reconciled: list[AppSettings] = []

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def reconcile(settings: AppSettings, **_kwargs: object) -> bool:
        reconciled.append(settings)
        return True

    def run_archive_command(settings: AppSettings, log_file: Path) -> int:
        assert settings.routes[0].source.bucket == "archive-bucket"
        recorded_logs.append(log_file)
        return 0

    lock_path = Path(base_env["LOG_DIR"]) / "archive.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _ = lock_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "reconcile_archive_lock", reconcile)
    monkeypatch.setattr(cli_module, "_run_archive_command", run_archive_command)

    result = RUNNER.invoke(cli_module.app, ["archive"])

    assert result.exit_code == 0
    working_set = cast(dict[str, object], json.loads(result.stderr))
    assert working_set["event"] == "startup.working_set"
    assert "secret-key" not in result.stderr
    assert "destination-secret" not in result.stderr
    assert recorded_logs == [Path("/tmp/s3-archiver.log")]
    assert len(reconciled) == 1


@pytest.mark.unit()
def test_archive_command_exits_with_wrapper_failure_without_lock_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    tmp_path: Path,
) -> None:
    base_env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr(os, "environ", base_env)
    subprocess_calls: list[tuple[str, Path]] = []

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def fail_reconcile(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("lock reconciliation should not run without an existing lock")

    def run_subprocess(settings: AppSettings, log_file: Path, **_kwargs: object) -> int:
        subprocess_calls.append((settings.routes[0].source.bucket, log_file))
        return 1

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "reconcile_archive_lock", fail_reconcile)
    monkeypatch.setattr(cli_module, "run_archive_subprocess", run_subprocess)

    result = RUNNER.invoke(cli_module.app, ["archive"])

    assert result.exit_code == 1
    assert subprocess_calls == [("archive-bucket", Path("/tmp/s3-archiver.log"))]


@pytest.mark.unit()
def test_archive_command_reports_startup_error_before_wrapper_runs(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def raise_config_error(_env: dict[str, str]) -> AppSettings:
        raise ConfigError("ARCHIVER_RUN_TIMEOUT is invalid")

    monkeypatch.setattr(AppSettings, "from_env", raise_config_error)

    result = RUNNER.invoke(cli_module.app, ["archive"])

    assert result.exit_code == cli_module.CONFIG_ERROR_EXIT_CODE
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload.get("field") == "ARCHIVER_RUN_TIMEOUT"


def test_archive_command_reports_phase_failure_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def run_core_archive(
        routes: tuple[object, ...],
        *,
        run_timeout: object,
        run_started_at_utc: object | None = None,
        **_kwargs: object,
    ) -> ArchiveRunResult:
        _unused = (routes, run_timeout, run_started_at_utc, _kwargs)
        return _archive_result(copy=ArchivePhaseResult("copy", ("old.txt: denied",)))

    monkeypatch.setattr(cli_module, "configure_logging", configure)

    def run_health(_settings: AppSettings, _log_file: Path) -> object:
        return object()

    monkeypatch.setattr(cli_module, "run_health_check", run_health)

    def build_client(_location: S3LocationSettings) -> object:
        return object()

    monkeypatch.setattr(cli_module, "build_s3_client", build_client)
    monkeypatch.setattr(cli_module, "run_archive", run_core_archive)

    result = RUNNER.invoke(cli_module.app, ["archive-once"])

    assert result.exit_code == 1
    assert result.stdout == ""
    payload = _load_payload(result.stderr)
    assert payload["status"] == "error"
    assert payload.get("phase") == "archive.copy"
    assert payload.get("key") == "old.txt"


@pytest.mark.unit()
def test_archive_command_wires_debug_transfer_logger(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    base_env["LOG_LEVEL"] = "DEBUG"
    monkeypatch.setattr(os, "environ", base_env)
    debug_loggers: list[object] = []

    def configure(_settings: AppSettings) -> Path:
        return Path("/tmp/log")

    def run_health(_settings: AppSettings, _log_file: Path) -> object:
        return object()

    def build_client(_location: S3LocationSettings) -> object:
        return object()

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_health_check", run_health)
    monkeypatch.setattr(cli_module, "build_s3_client", build_client)

    def run_core_archive(
        routes: tuple[object, ...],
        *,
        run_timeout: object,
        run_started_at_utc: object | None = None,
        debug_logger: object | None = None,
        **_kwargs: object,
    ) -> ArchiveRunResult:
        _unused = (routes, run_timeout, run_started_at_utc, _kwargs)
        debug_loggers.append(debug_logger)
        return _archive_result()

    monkeypatch.setattr(cli_module, "run_archive", run_core_archive)

    result = RUNNER.invoke(cli_module.app, ["archive-once"])

    assert result.exit_code == 0
    assert callable(debug_loggers[0])
