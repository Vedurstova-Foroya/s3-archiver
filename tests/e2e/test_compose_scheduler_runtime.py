"""End-to-end runtime probes for scheduler and archive lock behavior."""

from __future__ import annotations

import json
import subprocess
import textwrap
from typing import cast

import pytest
from s3_archiver_localstack_support import last_json_object
from s3_archiver_localstack_support.compose import run_compose
from s3_archiver_localstack_support.harness import bucket_pair_from_env, compose_runtime_log_dir


@pytest.mark.e2e()
def test_compose_scheduler_waits_for_next_tick_after_lock_refusal(
    compose_env: dict[str, str],
) -> None:
    log_dir = compose_runtime_log_dir(bucket_pair_from_env(compose_env))
    probe = textwrap.dedent(
        """
        /opt/venv/bin/python - <<'PY'
        import json

        from s3_archiver_cli import main as cli

        events = []
        state = {"sleep_calls": 0, "run_attempts": 0}

        def fake_sleep(hour: int, minute: int, **_kwargs) -> None:
            state["sleep_calls"] += 1
            events.append(f"sleep-{state['sleep_calls']}:{hour:02d}:{minute:02d}")
            if state["sleep_calls"] == 3:
                print(json.dumps({"events": events}, sort_keys=True))
                raise SystemExit(0)

        def fake_run_scheduled_archive(settings, log_file, **kwargs):
            _ = (settings, log_file, kwargs)
            state["run_attempts"] += 1
            events.append(f"run-{state['run_attempts']}")
            if state["run_attempts"] == 1:
                return
            cli.typer.echo(json.dumps({"status": "ok", "run_id": "scheduled-run"}, sort_keys=True))

        cli._sleep_until_next_daily_tick = fake_sleep
        cli.run_scheduled_archive = fake_run_scheduled_archive
        cli.schedule(daily_at_utc="04:05")
        PY
        """
    ).strip()
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "--no-deps",
        "-e",
        "APP_ENV_FILE=/dev/null",
        "-e",
        f"LOG_DIR={log_dir}",
        "--entrypoint",
        "sh",
        "app",
        "-lc",
        probe,
    )
    payload = _payload(result.stdout)
    assert payload["events"] == [
        "sleep-1:04:05",
        "run-1",
        "sleep-2:04:05",
        "run-2",
        "sleep-3:04:05",
    ]
    stderr_json_lines = [line for line in result.stderr.splitlines() if line.startswith("{")]
    assert len(stderr_json_lines) == 1
    assert (
        cast(dict[str, object], json.loads(stderr_json_lines[0]))["event"] == "startup.working_set"
    )
    assert '"run_id": "scheduled-run"' in result.stdout


@pytest.mark.e2e()
def test_compose_archive_recovers_timed_out_prior_container_lock_before_archive_work(
    compose_env: dict[str, str],
) -> None:
    log_dir = compose_runtime_log_dir(bucket_pair_from_env(compose_env))
    recovery_probe = textwrap.dedent(
        """
        /opt/venv/bin/python - <<'PY'
        import json
        import os
        import socket
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        from s3_archiver_cli import main as cli
        from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
        from s3_archiver_core.archive_manifest import ArchiveManifest
        from s3_archiver_core.settings import AppSettings

        events = []
        recoveries = []

        def fake_recovery(reason, payload):
            events.append(f"recovery:{reason}")
            recoveries.append(payload)

        def fake_health(settings, log_file):
            _ = (settings, log_file)
            events.append("health")
            return object()

        def fake_build(location):
            events.append(f"build:{location.bucket}")
            return object()

        def fake_run_archive(routes, **kwargs):
            _ = (routes, kwargs)
            events.append("run_archive")
            started = datetime(2026, 4, 23, tzinfo=UTC)
            return ArchiveRunResult(
                run_id="inner-run-id",
                manifest=ArchiveManifest(
                    run_started_at_utc=started,
                    entries=(),
                ),
                copy=ArchivePhaseResult("copy"),
                verify=ArchivePhaseResult("verify"),
                list=ArchivePhaseResult("list"),
            )

        cli._log_lock_recovery = fake_recovery
        cli.run_health_check = fake_health
        cli.build_s3_client = fake_build
        cli.run_archive = fake_run_archive

        lock_path = Path(os.environ["LOG_DIR"]) / "archive.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stale_payload = {
            "hostname": "prior-container-host",
            "pid": 123456,
            "run_id": "stale-run",
            "run_started_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
        }
        lock_path.write_text(json.dumps(stale_payload, sort_keys=True), encoding="utf-8")

        settings = AppSettings.from_env(cli._load_runtime_env())
        payload = cli._run_archive(settings, Path("/tmp/s3-archiver.log"))
        print(
            json.dumps(
                {
                    "current_hostname": socket.gethostname(),
                    "events": events,
                    "lock_exists_after": (settings.log_dir / "archive.lock").exists(),
                    "stale_hostname": recoveries[0]["hostname"],
                    "status": payload["status"],
                },
                sort_keys=True,
            )
        )
        PY
        """
    ).strip()
    _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)
    try:
        recovery_result = _run_compose(
            compose_env,
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "APP_ENV_FILE=/dev/null",
            "-e",
            f"LOG_DIR={log_dir}",
            "--entrypoint",
            "sh",
            "app",
            "-lc",
            recovery_probe,
        )
        recovery_payload = _payload(recovery_result.stdout)
    finally:
        _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)

    assert recovery_payload["current_hostname"] != "prior-container-host"
    assert recovery_payload["stale_hostname"] == "prior-container-host"
    assert recovery_payload["status"] == "ok"
    assert recovery_payload["lock_exists_after"] is False
    assert recovery_payload["events"] == [
        "recovery:stale_lock_timed_out",
        "health",
        f"build:{compose_env['TEST_S3_SOURCE_BUCKET']}",
        f"build:{compose_env['TEST_S3_DESTINATION_BUCKET']}",
        "run_archive",
    ]


def _run_compose(
    env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_compose(env, *args, check=check)


def _payload(output: str) -> dict[str, object]:
    return last_json_object(output)
