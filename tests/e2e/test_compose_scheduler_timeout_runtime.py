"""End-to-end runtime probes for scheduler timeout recovery."""

from __future__ import annotations

import subprocess
import textwrap

import pytest
from s3_archiver_localstack_support import last_json_object
from s3_archiver_localstack_support.compose import run_compose
from s3_archiver_localstack_support.harness import bucket_pair_from_env, compose_runtime_log_dir


@pytest.mark.e2e()
def test_compose_scheduler_reports_timeout_then_retries_on_next_tick(
    compose_env: dict[str, str],
) -> None:
    log_dir = compose_runtime_log_dir(bucket_pair_from_env(compose_env))
    probe = textwrap.dedent(
        """
        /opt/venv/bin/python - <<'PY'
        import json
        import os
        import sys
        from pathlib import Path

        from s3_archiver_cli import main as cli
        from s3_archiver_cli import scheduled_archive as scheduled

        events = []
        state = {"sleep_calls": 0, "command_calls": 0}
        timeout_probe = (
            "import json, os, time; "
            "from datetime import UTC, datetime, timedelta; "
            "from pathlib import Path; "
            "from s3_archiver_core.archive_lock import FileArchiveRunLock; "
            "lock = FileArchiveRunLock(Path(os.environ['LOG_DIR']) / 'archive.lock'); "
            "assert lock.acquire("
            "run_id='timed-out-run', "
            "run_started_at_utc=datetime.now(tz=UTC), "
            "timeout=timedelta(seconds=1)"
            "); "
            "print(json.dumps({'lock_acquired': True}), flush=True); "
            "time.sleep(10)"
        )

        def fake_sleep(hour: int, minute: int, **_kwargs) -> None:
            state["sleep_calls"] += 1
            events.append(f"sleep-{state['sleep_calls']}:{hour:02d}:{minute:02d}")
            if state["sleep_calls"] == 2:
                lock_cleared = not (Path(os.environ["LOG_DIR"]) / "archive.lock").exists()
                events.append(f"lock-cleared:{lock_cleared}")
            if state["sleep_calls"] == 3:
                print(json.dumps({"events": events}, sort_keys=True))
                raise SystemExit(0)

        def fake_command():
            state["command_calls"] += 1
            events.append(f"command-{state['command_calls']}")
            if state["command_calls"] == 1:
                return [sys.executable, "-c", timeout_probe]
            return [
                sys.executable,
                "-c",
                "import json; print(json.dumps({'status': 'ok', 'run_id': 'scheduled-run'}))",
            ]

        cli._sleep_until_next_daily_tick = fake_sleep
        scheduled.scheduled_archive_command = fake_command
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
        "ARCHIVER_RUN_TIMEOUT=1s",
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
        "command-1",
        "sleep-2:04:05",
        "lock-cleared:True",
        "command-2",
        "sleep-3:04:05",
    ]
    assert '"lock_acquired": true' in result.stdout
    assert '"status": "ok"' in result.stdout
    assert '"field": "ARCHIVER_RUN_TIMEOUT"' in result.stderr
    assert '"message": "archive run timed out"' in result.stderr
    assert '"phase": "archive.run"' in result.stderr
    assert '"timed_out": true' in result.stderr


def _run_compose(
    env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_compose(env, *args, check=check)


def _payload(output: str) -> dict[str, object]:
    return last_json_object(output)
