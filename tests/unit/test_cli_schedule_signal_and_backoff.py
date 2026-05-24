"""Tests for the scheduler's signal-handling, backoff, and lock-reconcile reporting."""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.main as cli_module
import s3_archiver_cli.schedule_runtime as schedule_runtime
from s3_archiver_core.errors import HealthCheckError
from s3_archiver_core.settings import AppSettings
from typer.testing import CliRunner

RUNNER = CliRunner()


def _trigger_installed_signal(signum: int) -> None:
    """Invoke whichever handler the scheduler registered for ``signum``."""

    handler = cast(object, signal.getsignal(signum))
    assert callable(handler)
    handler_fn = cast(Callable[[int, object], object], handler)
    _ = handler_fn(signum, None)


def _configure_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    def configure(_settings: AppSettings) -> Path:
        return Path("/tmp/log")

    monkeypatch.setattr(cli_module, "configure_logging", configure)


def _capture_info_events(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, dict[str, object]]],
) -> None:
    logger = logging.getLogger("s3_archiver.archive")

    def record_info(message: str, *, extra: dict[str, object]) -> None:
        events.append((message, extra))

    monkeypatch.setattr(logger, "info", record_info)


def _capture_warning_events(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, dict[str, object]]],
) -> None:
    logger = logging.getLogger("s3_archiver.archive")

    def record_warning(message: str, *, extra: dict[str, object]) -> None:
        events.append((message, extra))

    monkeypatch.setattr(logger, "warning", record_warning)


@pytest.mark.unit()
def test_schedule_command_exits_cleanly_on_sigterm_during_sleep(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    _configure_to_tmp(monkeypatch)
    info_events: list[tuple[str, dict[str, object]]] = []
    _capture_info_events(monkeypatch, info_events)

    def reconcile_lock(_settings: AppSettings, **_kwargs: object) -> bool:
        return True

    sleeps = 0

    def fake_sleep_until_tick(hour: int, minute: int, **_kwargs: object) -> None:
        nonlocal sleeps
        assert (hour, minute) == (4, 5)
        sleeps += 1
        _trigger_installed_signal(signal.SIGTERM)

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scheduler must not run after SIGTERM during sleep")

    monkeypatch.setattr(cli_module, "reconcile_archive_lock", reconcile_lock)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(cli_module, "run_scheduled_archive", fail_if_called)

    result = RUNNER.invoke(cli_module.app, ["schedule", "--daily-at-utc", "04:05"])

    assert result.exit_code == 0
    assert sleeps == 1
    shutdown = next(
        extra for _msg, extra in info_events if extra["event"] == "archive.schedule.shutdown"
    )
    assert shutdown["signal"] == "SIGTERM"


@pytest.mark.unit()
def test_schedule_command_exits_cleanly_on_sigint_after_successful_run(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    _configure_to_tmp(monkeypatch)
    info_events: list[tuple[str, dict[str, object]]] = []
    _capture_info_events(monkeypatch, info_events)

    def reconcile_lock(_settings: AppSettings, **_kwargs: object) -> bool:
        return True

    sleep_count = 0

    def fake_sleep_until_tick(_hour: int, _minute: int, **_kwargs: object) -> None:
        nonlocal sleep_count
        sleep_count += 1
        # The second sleep would block forever; the SIGINT in the post-run path
        # must short-circuit the loop at the top before we get here again.
        if sleep_count > 1:
            raise AssertionError("loop should have exited at the top after SIGINT")

    def fake_run_scheduled_archive(
        _settings: AppSettings, _log_file: Path, **_kwargs: object
    ) -> None:
        _trigger_installed_signal(signal.SIGINT)

    monkeypatch.setattr(cli_module, "reconcile_archive_lock", reconcile_lock)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(cli_module, "run_scheduled_archive", fake_run_scheduled_archive)

    result = RUNNER.invoke(cli_module.app, ["schedule", "--daily-at-utc", "04:05"])

    assert result.exit_code == 0
    assert sleep_count == 1
    shutdown = next(
        extra for _msg, extra in info_events if extra["event"] == "archive.schedule.shutdown"
    )
    assert shutdown["signal"] == "SIGINT"


@pytest.mark.unit()
def test_schedule_command_backoff_doubles_then_resets_after_success(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    _configure_to_tmp(monkeypatch)

    def reconcile_lock(_settings: AppSettings, **_kwargs: object) -> bool:
        return True

    sleep_delays: list[float] = []
    sleep_calls = 0

    def fake_sleep_until_tick(_hour: int, _minute: int, **kwargs: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        sleep_delays.append(cast(float, kwargs["extra_delay_seconds"]))
        if sleep_calls >= 5:
            _trigger_installed_signal(signal.SIGTERM)

    run_calls = 0

    def fake_run_scheduled_archive(
        _settings: AppSettings, _log_file: Path, **_kwargs: object
    ) -> None:
        nonlocal run_calls
        run_calls += 1
        if run_calls in (1, 2, 4):
            raise HealthCheckError(f"fail-{run_calls}")
        return

    monkeypatch.setattr(cli_module, "reconcile_archive_lock", reconcile_lock)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(cli_module, "run_scheduled_archive", fake_run_scheduled_archive)

    result = RUNNER.invoke(cli_module.app, ["schedule", "--daily-at-utc", "04:05"])

    assert result.exit_code == 0
    # The sleep before each run sees the backoff for the previous consecutive failures:
    # call 1 → 0 (no failures yet); call 2 → 1 (one failure); call 3 → 2 (two doubles);
    # call 4 → 0 (success at run 3 reset the counter); call 5 → 1 (run 4 failed again).
    assert sleep_delays == [0.0, 1.0, 2.0, 0.0, 1.0]


@pytest.mark.unit()
def test_schedule_command_warns_when_startup_lock_reconcile_fails(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    _configure_to_tmp(monkeypatch)
    warnings: list[tuple[str, dict[str, object]]] = []
    _capture_warning_events(monkeypatch, warnings)
    info_events: list[tuple[str, dict[str, object]]] = []
    _capture_info_events(monkeypatch, info_events)

    reconcile_calls = 0

    def reconcile_lock(_settings: AppSettings, **_kwargs: object) -> bool:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return False

    sleeps = 0

    def fake_sleep_until_tick(_hour: int, _minute: int, **_kwargs: object) -> None:
        nonlocal sleeps
        sleeps += 1
        _trigger_installed_signal(signal.SIGTERM)

    run_calls = 0

    def fake_run_scheduled_archive(
        _settings: AppSettings, _log_file: Path, **_kwargs: object
    ) -> None:
        nonlocal run_calls
        run_calls += 1

    monkeypatch.setattr(cli_module, "reconcile_archive_lock", reconcile_lock)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(cli_module, "run_scheduled_archive", fake_run_scheduled_archive)

    result = RUNNER.invoke(cli_module.app, ["schedule", "--daily-at-utc", "04:05"])

    assert result.exit_code == 0
    assert reconcile_calls == 1
    assert sleeps == 1
    assert run_calls == 0
    assert any(
        extra["event"] == "archive.schedule.lock_reconcile_failed" for _msg, extra in warnings
    )


@pytest.mark.unit()
def test_compute_backoff_delay_caps_and_doubles() -> None:
    assert schedule_runtime.compute_backoff_delay(0) == 0.0
    assert schedule_runtime.compute_backoff_delay(1) == 1.0
    assert schedule_runtime.compute_backoff_delay(2) == 2.0
    assert schedule_runtime.compute_backoff_delay(3) == 4.0
    # Cap at 300 seconds even when the exponent overshoots.
    assert schedule_runtime.compute_backoff_delay(50) == 300.0


@pytest.mark.unit()
def test_sleep_until_next_daily_tick_emits_backoff_log_when_extra_delay_set() -> None:
    from datetime import UTC, datetime

    import s3_archiver_cli.scheduled_archive as scheduled_archive_module

    sleep_calls: list[float] = []
    events: list[tuple[str, dict[str, object]]] = []

    def fake_now() -> datetime:
        return datetime(2026, 4, 23, 4, 4, 30, tzinfo=UTC)

    class RecordingLogger:
        def info(self, msg: object, *args: object, extra: object | None = None) -> object:
            _ = args
            assert isinstance(msg, str)
            assert isinstance(extra, dict)
            events.append((msg, cast(dict[str, object], extra)))
            return None

    scheduled_archive_module.sleep_until_next_daily_tick(
        4,
        5,
        now=fake_now,
        logger=RecordingLogger(),
        sleep=sleep_calls.append,
        extra_delay_seconds=7.5,
    )

    assert sleep_calls == [7.5, 30.0]
    assert events[0][1]["event"] == "archive.schedule.backoff"
    assert events[0][1]["delay_seconds"] == 7
    assert events[1][1]["event"] == "archive.schedule.waiting"


@pytest.mark.unit()
def test_install_and_restore_signals_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = monkeypatch  # signals are process-wide; rely on restore_schedule_signals.
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    flag = schedule_runtime.ShutdownFlag()
    previous = schedule_runtime.install_schedule_signals(flag)
    try:
        _trigger_installed_signal(signal.SIGTERM)
        assert flag.requested is True
        assert flag.signal_name == "SIGTERM"
    finally:
        schedule_runtime.restore_schedule_signals(previous)
    assert signal.getsignal(signal.SIGTERM) is original_term
    assert signal.getsignal(signal.SIGINT) is original_int
