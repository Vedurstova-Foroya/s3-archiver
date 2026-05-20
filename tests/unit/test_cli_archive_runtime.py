"""Tests for CLI archive-once runtime wiring."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.settings import AppSettings, S3LocationSettings
from typer.testing import CliRunner

from tests.unit.cli_archive_test_support import archive_result as _archive_result
from tests.unit.cli_archive_test_support import load_archive_payload as _load_payload

RUNNER = CliRunner()


@pytest.mark.unit()
def test_archive_once_command_runs_core_workflow_with_lock(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    built_locations: list[str] = []
    call_order: list[str] = []
    lock_paths: list[Path] = []
    run_timeouts: list[object] = []
    health_checked: list[Path] = []
    expected_temp_dir = AppSettings.from_env(base_env).temp_dir

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def prepare_temp_dir(temp_dir: Path) -> None:
        assert temp_dir == expected_temp_dir
        call_order.append("temp.prepare")

    def build_client(location: S3LocationSettings) -> object:
        built_locations.append(location.bucket)
        return object()

    def run_health(_settings: AppSettings, log_file: Path) -> object:
        call_order.append("health")
        health_checked.append(log_file)
        return object()

    class RecordingLock:
        def __init__(self, path: Path, **_kwargs: object) -> None:
            lock_paths.append(path)

        def acquire(self, *, run_id: str, run_started_at_utc: object, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            call_order.append("lock.acquire")
            return True

        def release(self, *, run_id: str) -> None:
            _ = run_id
            call_order.append("lock.release")

    def run_core_archive(
        routes: tuple[object, ...],
        *,
        run_timeout: object,
        run_started_at_utc: object | None = None,
        **_kwargs: object,
    ) -> ArchiveRunResult:
        _ = (routes, run_started_at_utc, _kwargs)
        call_order.append("run_archive")
        run_timeouts.append(run_timeout)
        return _archive_result()

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "prepare_runtime_temp_dir", prepare_temp_dir)
    monkeypatch.setattr(cli_module, "build_s3_client", build_client)
    monkeypatch.setattr(cli_module, "run_health_check", run_health)
    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cli_module, "run_archive", run_core_archive)

    result = RUNNER.invoke(cli_module.app, ["archive-once"])

    assert result.exit_code == 0
    payload = _load_payload(result.stdout)
    assert payload["status"] == "ok"
    working_set = cast(dict[str, object], json.loads(result.stderr))
    routes = cast(
        list[dict[str, object]],
        cast(dict[str, object], working_set["working_set"])["routes"],
    )
    assert working_set["event"] == "startup.working_set"
    assert routes == [
        {
            "name": "default",
            "parser_kind": "filename_timestamp",
            "copy_mode": "daily_tar_gz",
            "source_bucket": "archive-bucket",
            "source_path": "",
            "destination_bucket": "destination-bucket",
            "destination_path": "",
        }
    ]
    assert "access-key" not in result.stderr
    assert "secret-key" not in result.stderr
    assert "destination-secret" not in result.stderr
    assert payload.get("source_bucket") == "archive-bucket"
    assert payload.get("destination_bucket") == "destination-bucket"
    assert built_locations == ["archive-bucket", "destination-bucket"]
    assert health_checked == [Path("/tmp/s3-archiver.log")]
    assert lock_paths == [Path(base_env["LOG_DIR"]) / "archive.lock"]
    assert run_timeouts == [timedelta(days=7)]
    assert call_order == ["lock.acquire", "temp.prepare", "health", "run_archive", "lock.release"]


@pytest.mark.unit()
def test_archive_once_command_skips_temp_cleanup_when_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    temp_prepares: list[Path] = []

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def prepare_temp_dir(temp_dir: Path) -> None:
        temp_prepares.append(temp_dir)

    class RefusingLock:
        def __init__(self, _path: Path, **_kwargs: object) -> None:
            return

        def acquire(self, *, run_id: str, run_started_at_utc: object, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            return False

        def release(self, *, run_id: str) -> None:
            raise AssertionError(f"release should not run for {run_id}")

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "prepare_runtime_temp_dir", prepare_temp_dir)
    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RefusingLock)

    result = RUNNER.invoke(cli_module.app, ["archive-once"])

    assert result.exit_code == 1
    assert temp_prepares == []


@pytest.mark.unit()
def test_archive_once_timeout_releases_lock_and_records_failed_run(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    released: list[str] = []

    def configure(_: AppSettings) -> Path:
        return Path("/tmp/s3-archiver.log")

    def prepare_temp_dir(_temp_dir: Path) -> None:
        return

    def run_health(_settings: AppSettings, _log_file: Path) -> object:
        return object()

    def build_client(_location: S3LocationSettings) -> object:
        return object()

    class RecordingLock:
        def __init__(self, _path: Path, **_kwargs: object) -> None:
            return

        def acquire(self, *, run_id: str, run_started_at_utc: object, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            return True

        def release(self, *, run_id: str) -> None:
            released.append(run_id)

    def run_core_archive(
        routes: tuple[object, ...],
        *,
        run_timeout: object,
        run_started_at_utc: object | None = None,
        **_kwargs: object,
    ) -> ArchiveRunResult:
        _ = (routes, run_timeout, run_started_at_utc, _kwargs)
        return _archive_result(copy=ArchivePhaseResult("copy", ("archive run timed out",)))

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "prepare_runtime_temp_dir", prepare_temp_dir)
    monkeypatch.setattr(cli_module, "run_health_check", run_health)
    monkeypatch.setattr(cli_module, "build_s3_client", build_client)
    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cli_module, "run_archive", run_core_archive)

    result = RUNNER.invoke(cli_module.app, ["archive-once"])

    assert result.exit_code == 1
    assert len(released) == 1
    record_path = Path(base_env["LOG_DIR"]) / "archive-runs" / f"{released[0]}.json"
    decoded = cast(object, json.loads(record_path.read_text(encoding="utf-8")))
    assert isinstance(decoded, dict)
    record = cast(dict[str, object], decoded)
    assert record["status"] == "failed"
    payload = record["payload"]
    assert isinstance(payload, dict)
    assert payload["reason"] == "archive_run_timeout"
