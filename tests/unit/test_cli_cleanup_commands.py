"""Tests for the cleanup CLI commands and the lock-acquiring cleanup driver."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.cleanup_commands as cleanup_commands
import s3_archiver_cli.cleanup_runtime as cleanup_runtime
import s3_archiver_cli.main as cli_module
import typer
from s3_archiver_core.cleanup import CleanupResult
from s3_archiver_core.errors import CleanupError
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

from tests.unit.cli_cleanup_test_support import (
    RecordingLock,
    RefusingLock,
    build_deleting,
    build_opaque,
    empty_payload,
    make_settings,
    ok_payload,
    write_pending,
)

RUNNER = CliRunner()


def _runtime_attr(name: str) -> object:
    return cast(object, getattr(cleanup_runtime, name))


_as_text = cast(Callable[[str | bytes | None], str], _runtime_attr("_as_text"))
_stdout_echo = cast(Callable[[str], None], _runtime_attr("_stdout_echo"))
_stderr_echo = cast(Callable[[str], None], _runtime_attr("_stderr_echo"))


@pytest.mark.unit()
def test_cleanup_runtime_text_and_echo_helpers() -> None:
    echoed: list[tuple[str, bool, bool]] = []
    monkeypatch = pytest.MonkeyPatch()

    def record_echo(message: str, *, err: bool = False, nl: bool = True) -> None:
        echoed.append((message, err, nl))

    monkeypatch.setattr(typer, "echo", record_echo)
    try:
        assert _as_text(b"warn\n") == "warn\n"
        _stdout_echo("ok\n")
        _stderr_echo("warn\n")
    finally:
        monkeypatch.undo()

    assert echoed == [("ok\n", False, False), ("warn\n", True, False)]


@pytest.mark.unit()
def test_cleanup_failure_detail_defaults_when_no_failures() -> None:
    assert cleanup_commands.cleanup_failure_detail(CleanupResult((), empty=False)) == (
        "cleanup run failed"
    )


@pytest.mark.unit()
def test_run_cleanup_drains_pending_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=False)
    deletes: list[dict[str, object]] = []
    write_pending(settings)
    monkeypatch.setattr(cleanup_commands, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cleanup_commands, "build_s3_client", build_deleting(deletes))

    payload = cleanup_commands.run_cleanup_once(settings, Path("/tmp/log"), None)

    assert payload["status"] == "ok"
    assert deletes == [
        {
            "Bucket": "archive-bucket",
            "Delete": {
                "Objects": [{"Key": "data/a.xml", "VersionId": "v1"}],
                "Quiet": True,
            },
        }
    ]
    assert not (settings.cleanup_pending_dir / "run-1.jsonl").exists()


@pytest.mark.unit()
def test_run_cleanup_raises_when_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=False)
    monkeypatch.setattr(cleanup_commands, "FileArchiveRunLock", RefusingLock)

    with pytest.raises(CleanupError, match="already held"):
        _ = cleanup_commands.run_cleanup_once(settings, Path("/tmp/log"), None)


@pytest.mark.unit()
def test_run_cleanup_is_empty_without_pending(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=False)
    monkeypatch.setattr(cleanup_commands, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cleanup_commands, "build_s3_client", build_opaque)

    payload = cleanup_commands.run_cleanup_once(settings, Path("/tmp/log"), None)

    assert payload["status"] == "empty"


@pytest.mark.unit()
def test_cleanup_once_command_exits_zero_on_ok(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    base_env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr(os, "environ", base_env)
    monkeypatch.setattr(cleanup_commands, "run_cleanup_once", ok_payload)

    result = RUNNER.invoke(cli_module.app, ["cleanup-once"])

    assert result.exit_code == 0


@pytest.mark.unit()
def test_cleanup_once_command_exits_one_on_error(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    base_env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr(os, "environ", base_env)
    monkeypatch.setattr(cleanup_commands, "run_cleanup_once", empty_payload)

    result = RUNNER.invoke(cli_module.app, ["cleanup-once"])

    assert result.exit_code == 1


@pytest.mark.unit()
def test_cleanup_command_runs_child_and_reconciles_lock(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    base_env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr(os, "environ", base_env)
    reconciled: list[AppSettings] = []
    manifests: list[Path | None] = []

    def reconcile(settings: AppSettings, **_kwargs: object) -> bool:
        reconciled.append(settings)
        return True

    def run_child(
        _settings: AppSettings, _log_file: Path, *, manifest: Path | None = None, **_kwargs: object
    ) -> int:
        manifests.append(manifest)
        return 0

    def configure(_settings: AppSettings) -> Path:
        return Path("/tmp/log")

    lock_path = Path(base_env["LOG_DIR"]) / "archive.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _ = lock_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "reconcile_archive_lock", reconcile)
    monkeypatch.setattr(cli_module, "run_cleanup_subprocess", run_child)

    result = RUNNER.invoke(cli_module.app, ["cleanup", "--manifest", "given.jsonl"])

    assert result.exit_code == 0
    assert reconciled
    assert manifests == [Path("given.jsonl")]


@pytest.mark.unit()
def test_cleanup_command_propagates_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    base_env["LOG_DIR"] = str(tmp_path / "logs")
    monkeypatch.setattr(os, "environ", base_env)

    def configure(_settings: AppSettings) -> Path:
        return Path("/tmp/log")

    def run_child(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "run_cleanup_subprocess", run_child)

    result = RUNNER.invoke(cli_module.app, ["cleanup"])

    assert result.exit_code == 1


@pytest.mark.unit()
def test_cleanup_command_raises_cli_error_on_bad_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "environ", {"LOG_DIR": str(tmp_path / "logs"), "LOG_LEVEL": "INFO"})

    result = RUNNER.invoke(cli_module.app, ["cleanup"])

    assert result.exit_code == 2
