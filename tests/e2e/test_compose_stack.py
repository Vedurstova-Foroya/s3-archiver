"""End-to-end tests for the Docker Compose stack."""

from __future__ import annotations

import json
import subprocess
import textwrap
import time
from pathlib import Path
from typing import TypedDict, cast

import pytest
from s3_archiver_cli.main import HEALTH_CHECK_ERROR_EXIT_CODE, LOGGING_ERROR_EXIT_CODE

_COMPOSE_RETRY_DELAY_SECONDS = 2.0
_COMPOSE_RUN_RETRIES = 4
REPO_ROOT = Path(__file__).resolve().parents[2]


class ErrorPayload(TypedDict):
    status: str
    message: str


class RotationPayload(TypedDict):
    current_contents: str
    rotated_contents: str
    rotated_logs: list[str]


@pytest.mark.e2e()
def test_compose_app_healthcheck_succeeds(
    compose_env: dict[str, str],
    localstack_service: None,
) -> None:
    _ = localstack_service
    result = _run_compose(compose_env, "run", "--rm", "app")
    final_line = result.stdout.strip().splitlines()[-1]

    assert '"event": "logging.configured"' in result.stdout
    assert '"event": "health.started"' in result.stdout
    assert '"event": "health.succeeded"' in result.stdout
    assert '"status": "ok"' in final_line
    assert '"bucket": "s3-archiver-integration"' in final_line


@pytest.mark.e2e()
def test_compose_run_starts_localstack_without_pytest_orchestration(
    compose_env: dict[str, str],
) -> None:
    _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)
    try:
        result = _run_compose(compose_env, "run", "--rm", "app")
    finally:
        _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)
    final_line = result.stdout.strip().splitlines()[-1]

    assert '"event": "health.started"' in result.stdout
    assert '"event": "health.succeeded"' in result.stdout
    assert '"status": "ok"' in final_line
    assert '"bucket": "s3-archiver-integration"' in final_line


@pytest.mark.e2e()
def test_compose_app_writes_persisted_logs(
    compose_env: dict[str, str],
    localstack_service: None,
) -> None:
    _ = localstack_service
    _ = _run_compose(compose_env, "run", "--rm", "app")
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "app",
        "sh",
        "-lc",
        "test -s /var/log/s3-archiver/s3-archiver.log && cat /var/log/s3-archiver/s3-archiver.log",
    )

    assert '"event": "health.succeeded"' in result.stdout


@pytest.mark.e2e()
def test_compose_app_persists_rotated_logs(
    compose_env: dict[str, str],
    localstack_service: None,
) -> None:
    _ = localstack_service
    rotation_probe = textwrap.dedent(
        """
        /opt/venv/bin/python - <<'PY'
        import json
        import logging
        import os
        from logging.handlers import TimedRotatingFileHandler

        from s3_archiver_core.logging_config import configure_logging
        from s3_archiver_core.settings import AppSettings

        settings = AppSettings.from_env(dict(os.environ))
        log_file = configure_logging(settings)
        logger = logging.getLogger("s3_archiver.rotation")
        logger.info("before rollover", extra={"event": "rotation.before"})
        file_handler = next(
            handler
            for handler in logging.getLogger("s3_archiver").handlers
            if isinstance(handler, TimedRotatingFileHandler)
        )
        file_handler.doRollover()
        logger.info("after rollover", extra={"event": "rotation.after"})
        rotated_files = sorted(settings.log_dir.glob("s3-archiver.log.*"))
        if not rotated_files:
            raise SystemExit("no rotated files created")
        payload = {
            "current_contents": log_file.read_text(encoding="utf-8"),
            "rotated_contents": rotated_files[-1].read_text(encoding="utf-8"),
            "rotated_logs": [path.name for path in rotated_files],
        }
        print(json.dumps(payload, sort_keys=True))
        PY
        """
    ).strip()
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "app",
        "sh",
        "-lc",
        rotation_probe,
    )
    payload = _rotation_payload(result.stdout)

    assert any(name.startswith("s3-archiver.log.") for name in payload["rotated_logs"])
    assert '"event": "rotation.before"' in payload["rotated_contents"]
    assert '"event": "rotation.after"' in payload["current_contents"]


@pytest.mark.e2e()
def test_compose_app_fails_fast_when_log_dir_is_unwritable(
    compose_env: dict[str, str],
    localstack_service: None,
) -> None:
    _ = localstack_service
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "-e",
        "LOG_DIR=/proc/s3-archiver",
        "app",
        check=False,
    )

    assert result.returncode == LOGGING_ERROR_EXIT_CODE
    payload = _error_payload(result.stderr)
    assert payload["status"] == "error"
    assert "Failed to initialize log directory" in payload["message"]


def _run_compose(
    env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--profile", "test", *args]
    for attempt in range(_COMPOSE_RUN_RETRIES + 1):
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result
        if not check:
            return result
        if attempt == _COMPOSE_RUN_RETRIES or _is_non_retryable_compose_error(result):
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
        time.sleep(_COMPOSE_RETRY_DELAY_SECONDS)
    raise AssertionError("compose retry loop exhausted without returning")


def _is_non_retryable_compose_error(result: subprocess.CompletedProcess[str]) -> bool:
    retryable_messages = (
        "No such container",
        "marked for removal",
        "HeadBucket operation: Not Found",
        'Could not connect to the endpoint URL: "http://localstack:4566/',
    )
    if result.returncode in {137, HEALTH_CHECK_ERROR_EXIT_CODE}:
        return False
    return not any(
        message in result.stderr or message in result.stdout for message in retryable_messages
    )


def _error_payload(output: str) -> ErrorPayload:
    json_line = next(line for line in reversed(output.splitlines()) if line.startswith("{"))
    return cast(ErrorPayload, json.loads(json_line))


def _rotation_payload(output: str) -> RotationPayload:
    json_line = next(line for line in reversed(output.splitlines()) if line.startswith("{"))
    return cast(RotationPayload, json.loads(json_line))
