"""Tests for scheduler command wiring and subprocess execution."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.main as cli_module
import s3_archiver_cli.scheduled_archive as scheduled_archive_module
import typer
from s3_archiver_core.errors import ConfigError, HealthCheckError
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

RUNNER = CliRunner()


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))


_parse_daily_at_utc = cast(
    Callable[[str], tuple[int, int]],
    _private_attr(cli_module, "_parse_daily_at_utc"),
)
_run_archive = cast(
    Callable[[AppSettings, Path], dict[str, object]],
    _private_attr(cli_module, "_run_archive"),
)
_sleep_until_next_daily_tick = cast(
    Callable[[int, int], None],
    _private_attr(cli_module, "_sleep_until_next_daily_tick"),
)
_as_text = cast(
    Callable[[str | bytes | None], str],
    _private_attr(scheduled_archive_module, "_as_text"),
)
_stdout_echo = cast(Callable[[str], None], _private_attr(scheduled_archive_module, "_stdout_echo"))
_stderr_echo = cast(Callable[[str], None], _private_attr(scheduled_archive_module, "_stderr_echo"))


@pytest.mark.unit()
def test_schedule_command_runs_scheduled_archive_after_first_tick(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    events: list[str] = []
    sleep_calls = 0

    def configure(_settings: AppSettings) -> Path:
        return Path("/tmp/log")

    def reconcile_lock(_settings: AppSettings, **_kwargs: object) -> bool:
        events.append("reconcile")
        return True

    def fake_sleep_until_tick(hour: int, minute: int, **_kwargs: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise RuntimeError("stop scheduler test")
        assert (hour, minute) == (4, 5)
        events.append("sleep")

    def fake_run_scheduled_archive(
        settings: AppSettings, log_file: Path, **_kwargs: object
    ) -> None:
        _ = settings
        assert log_file == Path("/tmp/log")
        events.append("run")

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "reconcile_archive_lock", reconcile_lock)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(cli_module, "run_scheduled_archive", fake_run_scheduled_archive)

    result = RUNNER.invoke(cli_module.app, ["schedule", "--daily-at-utc", "04:05"])

    assert isinstance(result.exception, RuntimeError)
    assert "stop scheduler test" in str(result.exception)
    assert events == ["reconcile", "sleep", "run"]
    working_set = cast(dict[str, object], json.loads(result.stderr.splitlines()[0]))
    assert working_set["event"] == "startup.working_set"
    assert "secret-key" not in result.stderr


@pytest.mark.unit()
def test_schedule_command_continues_after_child_error_on_later_tick(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    events: list[str] = []
    sleep_calls = 0
    run_attempts = 0

    def configure(_settings: AppSettings) -> Path:
        return Path("/tmp/log")

    def fake_sleep_until_tick(hour: int, minute: int, **_kwargs: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        events.append(f"sleep-{sleep_calls}")
        assert (hour, minute) == (4, 5)
        if sleep_calls == 3:
            raise RuntimeError("stop scheduler test")

    def fake_run_scheduled_archive(
        _settings: AppSettings, _log_file: Path, **_kwargs: object
    ) -> None:
        nonlocal run_attempts
        run_attempts += 1
        events.append(f"run-{run_attempts}")
        if run_attempts == 1:
            return
        typer.echo(json.dumps({"status": "ok", "run_id": "scheduled-run"}))

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(cli_module, "run_scheduled_archive", fake_run_scheduled_archive)

    result = RUNNER.invoke(cli_module.app, ["schedule", "--daily-at-utc", "04:05"])

    assert isinstance(result.exception, RuntimeError)
    assert "stop scheduler test" in str(result.exception)
    assert events == ["sleep-1", "run-1", "sleep-2", "run-2", "sleep-3"]
    stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    assert cast(dict[str, object], json.loads(stderr_lines[0]))["event"] == "startup.working_set"
    assert _load_payload(result.stdout)["status"] == "ok"


@pytest.mark.unit()
def test_schedule_command_returns_json_for_startup_config_errors() -> None:
    monkeypatch = pytest.MonkeyPatch()

    def raise_config_error(_: dict[str, str]) -> AppSettings:
        raise ConfigError("bad env")

    monkeypatch.setattr(AppSettings, "from_env", raise_config_error)
    try:
        result = RUNNER.invoke(cli_module.app, ["schedule"])
    finally:
        monkeypatch.undo()

    assert result.exit_code == cli_module.CONFIG_ERROR_EXIT_CODE
    assert _load_payload(result.stderr)["message"] == "bad env"


@pytest.mark.unit()
def test_schedule_command_logs_child_domain_errors_and_keeps_looping(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    sleep_calls = 0

    def configure(_settings: AppSettings) -> Path:
        return Path("/tmp/log")

    def fake_sleep_until_tick(hour: int, minute: int, **_kwargs: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        assert (hour, minute) == (4, 5)
        if sleep_calls == 2:
            raise RuntimeError("stop scheduler test")

    def raise_child_error(_settings: AppSettings, _log_file: Path, **_kwargs: object) -> None:
        raise HealthCheckError("auth failed: denied")

    monkeypatch.setattr(cli_module, "configure_logging", configure)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(cli_module, "run_scheduled_archive", raise_child_error)

    result = RUNNER.invoke(cli_module.app, ["schedule", "--daily-at-utc", "04:05"])

    assert isinstance(result.exception, RuntimeError)
    assert "stop scheduler test" in str(result.exception)
    payload = _load_payload(result.stderr)
    assert payload["message"] == "auth failed: denied"
    assert payload["phase"] == "startup.preflight"


@pytest.mark.unit()
def test_parse_daily_at_utc_rejects_invalid_shapes() -> None:
    with pytest.raises(typer.BadParameter, match="HH:MM"):
        _ = _parse_daily_at_utc("04-05")

    with pytest.raises(typer.BadParameter, match="HH:MM"):
        _ = _parse_daily_at_utc("24:00")


@pytest.mark.unit()
def test_sleep_until_next_daily_tick_waits_until_future_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 23, 4, 6, tzinfo=UTC)
    sleep_calls: list[float] = []
    contexts: list[dict[str, object]] = []
    logger = logging.getLogger("s3_archiver.archive")

    class FrozenDateTime:
        @staticmethod
        def now(*, tz: object) -> datetime:
            _ = tz
            return now

    def record_info(_message: str, *, extra: dict[str, object]) -> None:
        contexts.append(extra)

    monkeypatch.setattr(cli_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(time, "sleep", sleep_calls.append)
    monkeypatch.setattr(logger, "info", record_info)

    _sleep_until_next_daily_tick(4, 5)

    assert sleep_calls == [86340.0]
    assert contexts == [
        {
            "event": "archive.schedule.waiting",
            "scheduled_at_utc": "2026-04-24T04:05:00+00:00",
            "sleep_seconds": 86340,
        }
    ]


@pytest.mark.unit()
def test_sleep_until_next_daily_tick_waits_until_later_same_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 23, 4, 4, 30, tzinfo=UTC)
    sleep_calls: list[float] = []
    logger = logging.getLogger("s3_archiver.archive")

    class FrozenDateTime:
        @staticmethod
        def now(*, tz: object) -> datetime:
            _ = tz
            return now

    monkeypatch.setattr(cli_module, "datetime", FrozenDateTime)

    def noop_info(_message: str, *, extra: dict[str, object]) -> None:
        _ = extra

    monkeypatch.setattr(time, "sleep", sleep_calls.append)
    monkeypatch.setattr(logger, "info", noop_info)

    _sleep_until_next_daily_tick(4, 5)

    assert sleep_calls == [30.0]


@pytest.mark.unit()
def test_scheduled_archive_command_targets_archive_cli() -> None:
    assert scheduled_archive_module.scheduled_archive_command() == [
        sys.executable,
        "-c",
        "from s3_archiver_cli.main import main; main()",
        "archive-once",
    ]


def _load_payload(output: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = cast(object, json.loads(stripped))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return cast(dict[str, object], cast(object, payload))
    raise AssertionError(f"expected JSON payload in output: {output!r}")
