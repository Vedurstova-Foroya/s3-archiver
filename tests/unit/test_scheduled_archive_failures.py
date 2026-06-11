"""Failure propagation tests for scheduled archive child runs."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event

import pytest
import s3_archiver_cli.scheduled_archive as scheduled_archive_module
from s3_archiver_core.errors import ArchiveRunError
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_run_scheduled_archive_raises_when_child_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    stderr_messages: list[str] = []

    def fake_run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 4, stdout="", stderr="denied\n")

    with pytest.raises(ArchiveRunError, match="scheduled archive child exited with code 4"):
        scheduled_archive_module.run_scheduled_archive(
            settings,
            Path("/tmp/log"),
            command=["archive"],
            run_command=fake_run_command,
            stderr_echo=stderr_messages.append,
        )

    assert stderr_messages == ["denied\n"]


@pytest.mark.unit()
def test_run_scheduled_archive_forwards_non_timeout_child_error_payloads(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    logged_payloads: list[Mapping[str, JsonValue]] = []

    def fake_run_archive_subprocess(
        child_settings: AppSettings,
        log_file: Path,
        *,
        log_error: Callable[[Mapping[str, JsonValue]], None],
        **_kwargs: object,
    ) -> int:
        _ = (child_settings, log_file, _kwargs)
        log_error({"reason": "archive_child_failed", "status": "error"})
        return 7

    monkeypatch.setattr(
        scheduled_archive_module, "run_archive_subprocess", fake_run_archive_subprocess
    )

    with pytest.raises(ArchiveRunError, match="scheduled archive child exited with code 7"):
        scheduled_archive_module.run_scheduled_archive(
            settings,
            Path("/tmp/log"),
            command=["archive"],
            log_error=logged_payloads.append,
        )

    assert logged_payloads == [{"reason": "archive_child_failed", "status": "error"}]


@pytest.mark.unit()
def test_run_scheduled_archive_does_not_raise_when_shutdown_stops_child(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    shutdown = Event()
    shutdown.set()

    def fake_run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, -2, stdout="", stderr="")

    scheduled_archive_module.run_scheduled_archive(
        settings,
        Path("/tmp/log"),
        command=["archive"],
        run_command=fake_run_command,
        shutdown_event=shutdown,
    )
