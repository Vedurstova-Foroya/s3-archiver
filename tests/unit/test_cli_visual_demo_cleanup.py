"""Tests for the cleanup-performing visual demo path."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.main as cli_module
import s3_archiver_cli.visual_demo as demo_module
import s3_archiver_cli.visual_demo_command as demo_command_module
import typer
from s3_archiver_core.settings import AppSettings, S3LocationSettings
from typer.testing import CliRunner

from tests.unit.archive_workflow_fakes import listed_object as _listed

RUNNER = CliRunner()


class FakeReport:
    def as_dict(self) -> dict[str, demo_module.JsonValue]:
        return {"status": "ok", "checked_at": "2026-04-24T12:00:00+00:00"}


def _configure_logging(_: AppSettings) -> Path:
    return Path("/tmp/s3-archiver.log")


def _fake_build_client(_location: S3LocationSettings) -> object:
    return object()


def _demo_settings(base_env: dict[str, str], tmp_path: Path) -> AppSettings:
    settings = AppSettings.from_env(base_env)
    return replace(settings, retention_days=60, cleanup_enabled=False, temp_dir=tmp_path / "tmp")


@pytest.mark.unit()
def test_demo_cleanup_command_forces_cleanup_mode(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    monkeypatch.setattr(cli_module, "configure_logging", _configure_logging)

    def run_command(
        *,
        perform_cleanup: bool,
        run_payload_command: object,
        archive_runner: object,
        cleanup_preview_runner: object,
        emit: Callable[[str], None],
    ) -> None:
        _ = run_payload_command, archive_runner, cleanup_preview_runner
        assert perform_cleanup is True
        payload: dict[str, demo_module.JsonValue] = {"status": "ok", "cleanup_mode": "cleanup"}
        emit(json.dumps(payload, sort_keys=True))

    monkeypatch.setattr(demo_command_module, "run", run_command)

    result = RUNNER.invoke(cli_module.app, ["demo-cleanup"])

    assert result.exit_code == 0
    payload = cast(dict[str, object], json.loads(result.stdout.splitlines()[-1]))
    assert payload["cleanup_mode"] == "cleanup"


@pytest.mark.unit()
def test_run_visual_demo_cleanup_mode_deletes_verified_source_objects(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    tmp_path: Path,
) -> None:
    settings = _demo_settings(base_env, tmp_path)
    old_key = "demo/2024-02-20T00-00-00.txt"
    retained_key = "demo/2024-02-21T00-00-00.txt"
    state: dict[str, list[object]] = {
        settings.source.bucket: [_listed(old_key, 61), _listed(retained_key, 59)],
        settings.destination.bucket: [],
    }

    class FakeArchiveBucket:
        bucket: str

        def __init__(self, _client: object, bucket: str, _temp_dir: Path) -> None:
            self.bucket = bucket

        def versioning_state(self) -> str:
            return "Enabled"

        def list_source_objects(self, _versioning_state: str) -> Iterable[object]:
            return tuple(state[self.bucket])

    def archive_runner(
        settings_arg: AppSettings, _log_file: Path
    ) -> dict[str, demo_module.JsonValue]:
        assert settings_arg.cleanup_enabled is True
        state[settings.source.bucket] = [_listed(retained_key, 59)]
        state[settings.destination.bucket] = [_listed("demo/2024-02-20.tar.gz", 0)]
        return _cleanup_archive_payload()

    def cleanup_runner(_settings: AppSettings, _log_file: Path) -> dict[str, demo_module.JsonValue]:
        raise AssertionError("cleanup mode must not run cleanup preview")

    def fake_health_check(_settings: AppSettings, _log_file: Path) -> FakeReport:
        return FakeReport()

    lines: list[str] = []
    monkeypatch.setattr(demo_module, "run_health_check", fake_health_check)
    monkeypatch.setattr(demo_module, "build_s3_client", _fake_build_client)
    monkeypatch.setattr(demo_module, "S3ArchiveBucket", FakeArchiveBucket)

    summary = demo_module.run_visual_demo(
        settings,
        settings.log_dir / "s3-archiver.log",
        archive_runner=archive_runner,
        cleanup_preview_runner=cleanup_runner,
        emit=lines.append,
        now=lambda: datetime(2024, 4, 20, tzinfo=UTC),
        perform_cleanup=True,
    )

    assert summary["cleanup_mode"] == "cleanup"
    assert summary["cleanup_performed"] is True
    assert summary["cleanup_preview"] is None
    assert summary["cleanup_deleted_source_object_count"] == 1
    snapshots = cast(dict[str, object], summary["snapshots"])
    after_cleanup = cast(dict[str, object], snapshots["after_cleanup"])
    assert after_cleanup["source_object_count"] == 1
    assert any("== S3 Archiver Cleanup Visual Demo ==" in line for line in lines)
    assert any("== After cleanup ==" in line for line in lines)
    assert any("cleanup deleted source object count: 1" in line for line in lines)
    assert not any("== Cleanup Preview ==" in line for line in lines)


@pytest.mark.unit()
@pytest.mark.parametrize("perform_cleanup", [False, True])
def test_visual_demo_command_run_forwards_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    perform_cleanup: bool,
) -> None:
    settings = AppSettings.from_env(base_env)
    log_file = Path("/tmp/s3-archiver.log")
    lines: list[str] = []
    emit = lines.append
    forwarded_cleanup_modes: list[bool] = []
    forwarded_archive_runners: list[demo_module.ArchiveRunner] = []
    forwarded_cleanup_runners: list[demo_module.CleanupPreviewRunner] = []
    forwarded_emitters: list[demo_module.Emitter] = []

    def archive_runner(_settings: AppSettings, _log_file: Path) -> dict[str, demo_module.JsonValue]:
        return {"status": "ok", "runner": "archive"}

    def cleanup_runner(_settings: AppSettings, _log_file: Path) -> dict[str, demo_module.JsonValue]:
        return {"status": "ok", "runner": "cleanup-preview"}

    def fake_run_visual_demo(
        _settings: AppSettings,
        _log_file: Path,
        *,
        archive_runner: demo_module.ArchiveRunner,
        cleanup_preview_runner: demo_module.CleanupPreviewRunner,
        emit: demo_module.Emitter,
        perform_cleanup: bool = False,
    ) -> dict[str, demo_module.JsonValue]:
        forwarded_cleanup_modes.append(perform_cleanup)
        forwarded_archive_runners.append(archive_runner)
        forwarded_cleanup_runners.append(cleanup_preview_runner)
        forwarded_emitters.append(emit)
        emit("forwarded")
        return {"status": "ok"}

    def run_payload_command(
        command: Callable[[AppSettings, Path], dict[str, demo_module.JsonValue]],
    ) -> dict[str, demo_module.JsonValue]:
        return command(settings, log_file)

    monkeypatch.setattr(demo_command_module, "run_visual_demo", fake_run_visual_demo)

    demo_command_module.run(
        perform_cleanup=perform_cleanup,
        run_payload_command=run_payload_command,
        archive_runner=archive_runner,
        cleanup_preview_runner=cleanup_runner,
        emit=emit,
    )

    assert forwarded_cleanup_modes == [perform_cleanup]
    assert forwarded_archive_runners == [archive_runner]
    assert forwarded_cleanup_runners == [cleanup_runner]
    assert forwarded_emitters == [emit]
    assert lines == ["forwarded"]


@pytest.mark.unit()
def test_visual_demo_command_run_raises_exit_for_error_status() -> None:
    def archive_runner(_settings: AppSettings, _log_file: Path) -> dict[str, demo_module.JsonValue]:
        return {"status": "ok"}

    def cleanup_runner(_settings: AppSettings, _log_file: Path) -> dict[str, demo_module.JsonValue]:
        return {"status": "ok"}

    def run_payload_command(
        command: Callable[[AppSettings, Path], dict[str, demo_module.JsonValue]],
    ) -> dict[str, demo_module.JsonValue]:
        _ = command
        return {"status": "error"}

    with pytest.raises(typer.Exit) as exc_info:
        demo_command_module.run(
            perform_cleanup=False,
            run_payload_command=run_payload_command,
            archive_runner=archive_runner,
            cleanup_preview_runner=cleanup_runner,
            emit=lambda _line: None,
        )

    assert exc_info.value.exit_code == 1


def _cleanup_archive_payload() -> dict[str, demo_module.JsonValue]:
    return {
        "status": "ok",
        "archive_groups": [
            {
                "destination_archive_key": "demo/2024-02-20.tar.gz",
                "source_object_count": 1,
                "skipped_object_count": 0,
                "cleanup_status": "ok",
            }
        ],
        "phases": {
            "list": {"status": "ok", "failure_count": 0},
            "copy": {"status": "ok", "failure_count": 0},
            "verify": {"status": "ok", "failure_count": 0},
            "cleanup": {"status": "ok", "failure_count": 0},
        },
    }
