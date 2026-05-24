"""Integration tests for scheduler and archive lock coordination."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypedDict, cast

import pytest
import s3_archiver_cli.main as cli_module
import s3_archiver_cli.scheduled_archive as scheduled_archive_module
from s3_archiver_core.settings import AppSettings
from s3_archiver_localstack_support import json_objects, last_json_object
from s3_archiver_localstack_support.harness import (
    LOCALSTACK_HOST_ENDPOINT,
    LocalstackBucketPair,
    localstack_test_env,
)
from s3_archiver_localstack_support.objects import (
    listed_keys,
    localstack_s3_client,
    put_test_object,
    read_tar_gz_members_text,
)

from tests.integration.archive_cli_test_support import run_archive_command as _run_archive
from tests.integration.scheduler_timeout_probe import timeout_probe_script


class SchedulerErrorPayload(TypedDict):
    message: str
    phase: str
    field: str
    reason: str
    timed_out: bool


class _DeleteObjectClient(Protocol):
    def delete_object(self, **kwargs: object) -> object:
        """Delete one object."""
        ...


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

    def fake_sleep_until_tick(hour: int, minute: int, **_kwargs: object) -> None:
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
    env["ARCHIVER_RUN_TIMEOUT"] = "3s"
    settings = AppSettings.from_env(env)
    source_client = localstack_s3_client(env, "source")
    destination_client = localstack_s3_client(env, "destination")
    prefix = "schedule-timeout"
    target_day = (datetime.now(tz=UTC) - timedelta(days=60)).date()
    timeout_key = f"{prefix}/timed-out/{target_day}T00-00-00-timeout.txt"
    retry_key = f"{prefix}/retry/{target_day}T00-00-00-retry.txt"
    timeout_archive_key = f"{prefix}/timed-out/{target_day}.tar.gz"
    retry_archive_key = f"{prefix}/retry/{target_day}.tar.gz"
    timeout_payload = "timed-out child payload\n"
    retry_payload = "successful retry payload\n"
    _ = put_test_object(
        source_client,
        localstack_bucket_pair.source,
        timeout_key,
        body=timeout_payload.encode(),
    )
    env["S3_ARCHIVER_TIMEOUT_ARCHIVE_KEY"] = timeout_archive_key
    env["S3_ARCHIVER_TIMEOUT_MEMBER_KEY"] = timeout_key
    env["S3_ARCHIVER_TIMEOUT_MEMBER_PAYLOAD"] = timeout_payload
    lock_path = settings.log_dir / "archive.lock"
    sleep_calls = 0
    command_calls = 0
    retry_seeded = False
    max_sleep_calls = 6
    archive_command = scheduled_archive_module.scheduled_archive_command
    timeout_probe = timeout_probe_script()

    def fake_scheduled_archive_command() -> list[str]:
        nonlocal command_calls
        command_calls += 1
        if command_calls == 1:
            return [sys.executable, "-c", timeout_probe]
        return archive_command()

    def fake_sleep_until_tick(hour: int, minute: int, **_kwargs: object) -> None:
        nonlocal retry_seeded, sleep_calls
        _ = (hour, minute)
        sleep_calls += 1
        if sleep_calls == 1:
            return
        if sleep_calls == 2:
            assert not lock_path.exists()
            assert read_tar_gz_members_text(
                destination_client,
                localstack_bucket_pair.destination,
                timeout_archive_key,
            ) == {timeout_key: timeout_payload}
            _ = cast(_DeleteObjectClient, cast(object, source_client)).delete_object(
                Bucket=localstack_bucket_pair.source,
                Key=timeout_key,
            )
            _ = put_test_object(
                source_client,
                localstack_bucket_pair.source,
                retry_key,
                body=retry_payload.encode(),
            )
            retry_seeded = True
            return
        if retry_archive_key in listed_keys(destination_client, localstack_bucket_pair.destination):
            assert retry_seeded
            assert read_tar_gz_members_text(
                destination_client,
                localstack_bucket_pair.destination,
                retry_archive_key,
            ) == {retry_key: retry_payload}
            raise RuntimeError("stop scheduler integration test")
        if sleep_calls >= max_sleep_calls:
            raise AssertionError(
                "scheduled archive retry loop did not write successful retry archive"
            )

    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(cli_module, "_sleep_until_next_daily_tick", fake_sleep_until_tick)
    monkeypatch.setattr(
        scheduled_archive_module, "scheduled_archive_command", fake_scheduled_archive_command
    )

    with pytest.raises(RuntimeError, match="stop scheduler integration test"):
        cli_module.schedule(daily_at_utc="04:05")

    captured = capsys.readouterr()
    error_payload = _last_error_payload(captured.err)
    stdout_payloads = _json_payloads(captured.out)
    success_payload = next(
        (payload for payload in stdout_payloads if payload.get("status") == "ok"),
        None,
    )
    assert any(
        payload.get("timeout_child_archive_key") == timeout_archive_key
        for payload in stdout_payloads
    )
    assert success_payload is not None
    destination_archive_keys = success_payload.get("destination_archive_keys")
    assert isinstance(destination_archive_keys, list)
    assert retry_archive_key in destination_archive_keys
    assert timeout_archive_key not in destination_archive_keys
    assert error_payload["phase"] == "archive.run"
    assert error_payload["field"] == "ARCHIVER_RUN_TIMEOUT"
    assert error_payload["message"] == "archive run timed out"
    assert error_payload["reason"] == "archive_run_timeout"
    assert error_payload["timed_out"] is True
    assert command_calls >= 2
    assert retry_key in listed_keys(source_client, localstack_bucket_pair.source)
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        timeout_archive_key,
        retry_archive_key,
    }
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, timeout_archive_key
    ) == {timeout_key: timeout_payload}
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, retry_archive_key
    ) == {retry_key: retry_payload}
    assert not lock_path.exists()


def _integration_env(tmp_path: Path, bucket_pair: LocalstackBucketPair) -> dict[str, str]:
    env = localstack_test_env(
        bucket_pair,
        endpoint=os.environ.get("LOCALSTACK_S3_URL", LOCALSTACK_HOST_ENDPOINT),
        log_dir=str(tmp_path / "logs"),
    )
    env["ARCHIVER_PAYLOAD_DETAIL"] = "full"
    return env


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
    return last_json_object(output)


def _json_payloads(output: str) -> list[dict[str, object]]:
    return json_objects(output)


def _last_error_payload(output: str) -> SchedulerErrorPayload:
    return cast(SchedulerErrorPayload, cast(object, _last_json(output)))
