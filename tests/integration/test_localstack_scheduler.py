"""Integration tests for scheduler and archive lock coordination."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

import pytest
import s3_archiver_cli.main as cli_module
import s3_archiver_cli.scheduled_archive as scheduled_archive_module
from s3_archiver_core.settings import AppSettings

from tests.integration.archive_cli_test_support import run_archive_command as _run_archive
from tests.integration.localstack_harness import (
    LOCALSTACK_HOST_ENDPOINT,
    LocalstackBucketPair,
    localstack_test_env,
)
from tests.integration.localstack_object_helpers import (
    listed_keys,
    localstack_s3_client,
    put_test_object,
    read_tar_gz_members_text,
)


class SchedulerErrorPayload(TypedDict):
    message: str
    phase: str
    field: str
    reason: str
    timed_out: bool


@pytest.mark.integration()
def test_schedule_skips_immediate_replay_after_lock_refusal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _integration_env(tmp_path, localstack_bucket_pair)
    settings = AppSettings.from_env(env)
    active_lock = _start_active_lock(settings.log_dir / "archive.lock")
    sleep_calls = 0

    def fake_sleep_until_tick(hour: int, minute: int) -> None:
        nonlocal sleep_calls
        _ = (hour, minute)
        sleep_calls += 1
        if sleep_calls == 2:
            active_lock.terminate()
            _ = active_lock.wait(timeout=5)
        if sleep_calls >= 3:
            raise RuntimeError("stop scheduler integration test")

    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)

    try:
        with pytest.raises(RuntimeError, match="stop scheduler integration test"):
            cli_module.schedule(daily_at_utc="04:05")
    finally:
        if active_lock.poll() is None:
            active_lock.terminate()
            _ = active_lock.wait(timeout=5)

    captured = capsys.readouterr()
    error_payload = _last_json(captured.err)
    success_payload = _last_json(captured.out)
    assert error_payload["reason"] == "archive_run_abandoned"
    assert error_payload["recovered"] is True
    assert success_payload["status"] == "ok"
    log_file = settings.log_dir / "s3-archiver.log"
    assert log_file.exists()
    log_text = log_file.read_text(encoding="utf-8")
    assert '"event": "archive.lock.recovered"' in log_text
    assert '"reason": "stale_lock_abandoned"' in log_text


@pytest.mark.integration()
def test_run_archive_recovers_timed_out_prior_host_lock_before_archive_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _integration_env(tmp_path, localstack_bucket_pair)
    settings = AppSettings.from_env(env)
    lock_path = settings.log_dir / "archive.lock"
    log_file = settings.log_dir / "s3-archiver.log"
    stale_payload = {
        "hostname": "prior-container-host",
        "pid": 4321,
        "run_id": "stale-run",
        "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _ = lock_path.write_text(json.dumps(stale_payload), encoding="utf-8")

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert not lock_path.exists()
    log_text = log_file.read_text(encoding="utf-8")
    assert '"event": "archive.lock.recovered"' in log_text
    assert '"reason": "stale_lock_timed_out"' in log_text


@pytest.mark.integration()
def test_schedule_retries_after_timeout_on_next_tick(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _integration_env(tmp_path, localstack_bucket_pair)
    env["ARCHIVER_MAX_WORKERS"] = "1"
    env["ARCHIVER_RUN_TIMEOUT"] = "1s"
    settings = AppSettings.from_env(env)
    source_client = localstack_s3_client(env, "source")
    destination_client = localstack_s3_client(env, "destination")
    prefix = "schedule-timeout"
    target_day = (datetime.now(tz=UTC) - timedelta(days=60)).date()
    key = f"{prefix}/{target_day}T00-00-00-retry.txt"
    archive_key = f"{prefix}/{target_day}.tar.gz"
    _ = put_test_object(source_client, localstack_bucket_pair.source, key)
    lock_path = settings.log_dir / "archive.lock"
    sleep_calls = 0
    command_calls = 0
    archive_command = scheduled_archive_module.scheduled_archive_command
    timeout_probe = textwrap.dedent(
        """
        import json
        import os
        import time
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        from s3_archiver_core.archive_lock import FileArchiveRunLock

        lock = FileArchiveRunLock(Path(os.environ["LOG_DIR"]) / "archive.lock")
        if not lock.acquire(
            run_id="timed-out-run",
            run_started_at_utc=datetime.now(tz=UTC),
            timeout=timedelta(seconds=1),
        ):
            raise SystemExit("failed to acquire archive lock")
        print(json.dumps({"lock_acquired": True}), flush=True)
        time.sleep(10)
        """
    ).strip()

    def fake_scheduled_archive_command() -> list[str]:
        nonlocal command_calls
        command_calls += 1
        if command_calls == 1:
            return [sys.executable, "-c", timeout_probe]
        return archive_command()

    def fake_sleep_until_tick(hour: int, minute: int) -> None:
        nonlocal sleep_calls
        _ = (hour, minute)
        sleep_calls += 1
        if sleep_calls == 1:
            return
        if sleep_calls == 2:
            assert not lock_path.exists()
            return
        raise RuntimeError("stop scheduler integration test")

    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(
        scheduled_archive_module, "scheduled_archive_command", fake_scheduled_archive_command
    )

    with pytest.raises(RuntimeError, match="stop scheduler integration test"):
        cli_module.schedule(daily_at_utc="04:05")

    captured = capsys.readouterr()
    error_payload = _last_error_payload(captured.err)
    assert '"lock_acquired": true' in captured.out
    assert error_payload["phase"] == "archive.run"
    assert error_payload["field"] == "ARCHIVER_RUN_TIMEOUT"
    assert error_payload["message"] == "archive run timed out"
    assert error_payload["reason"] == "archive_run_timeout"
    assert error_payload["timed_out"] is True
    assert command_calls == 2
    assert key in listed_keys(source_client, localstack_bucket_pair.source)
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {archive_key}
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, archive_key
    ) == {key: f"payload for {key}\n"}
    assert not lock_path.exists()


def _integration_env(tmp_path: Path, bucket_pair: LocalstackBucketPair) -> dict[str, str]:
    return localstack_test_env(
        bucket_pair,
        endpoint=os.environ.get("LOCALSTACK_S3_URL", LOCALSTACK_HOST_ENDPOINT),
        log_dir=str(tmp_path / "logs"),
    )


def _start_active_lock(lock_path: Path) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(["sleep", "30"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hostname": socket.gethostname(),
        "pid": process.pid,
        "run_id": "active-run",
        "run_started_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    _ = lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return process


def _last_json(output: str) -> dict[str, object]:
    json_line = next(line for line in reversed(output.splitlines()) if line.startswith("{"))
    return cast(dict[str, object], json.loads(json_line))


def _last_error_payload(output: str) -> SchedulerErrorPayload:
    return cast(SchedulerErrorPayload, cast(object, _last_json(output)))
