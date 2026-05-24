"""End-to-end tests for the Docker Compose stack."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import TypedDict, cast

import pytest
from s3_archiver_cli.main import HEALTH_CHECK_ERROR_EXIT_CODE, LOGGING_ERROR_EXIT_CODE
from s3_archiver_localstack_support import last_json_object
from s3_archiver_localstack_support.compose import find_repo_root, run_stack_compose
from s3_archiver_localstack_support.harness import bucket_pair_from_env, compose_runtime_log_dir

REPO_ROOT = find_repo_root()


class ErrorPayload(TypedDict):
    status: str
    message: str


class RotationPayload(TypedDict):
    current_contents: str
    rotated_contents: str
    rotated_logs: list[str]


@pytest.mark.e2e()
def test_compose_app_without_command_shows_help(compose_env: dict[str, str]) -> None:
    result = _run_compose(compose_env, "run", "--rm", "app")

    assert "Usage:" in result.stdout
    assert "check" in result.stdout
    assert "archive" in result.stdout


@pytest.mark.e2e()
def test_compose_app_check_succeeds(
    compose_env: dict[str, str],
    localstack_bucket_pair: object,
) -> None:
    _ = localstack_bucket_pair
    result = _run_compose(compose_env, "run", "--rm", "app", "check")
    final_line = result.stdout.strip().splitlines()[-1]

    assert '"event": "logging.configured"' in result.stdout
    assert '"event": "health.started"' in result.stdout
    assert '"event": "health.succeeded"' in result.stdout
    assert '"status": "ok"' in final_line
    assert f'"source_bucket": "{bucket_pair_from_env(compose_env).source}"' in final_line


@pytest.mark.e2e()
def test_compose_localstack_startup_does_not_precreate_test_buckets(
    compose_env: dict[str, str],
) -> None:
    _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)
    try:
        _ = _run_compose(compose_env, "up", "-d", "localstack")
        result = _run_compose(
            compose_env,
            "exec",
            "-T",
            "localstack",
            "awslocal",
            "s3api",
            "list-buckets",
        )
    finally:
        _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)
    payload = last_json_object(result.stdout)
    bucket_names = {
        str(bucket["Name"])
        for bucket in cast(list[dict[str, object]], payload.get("Buckets", []))
        if "Name" in bucket
    }
    bucket_pair = bucket_pair_from_env(compose_env)

    assert bucket_pair.source not in bucket_names
    assert bucket_pair.destination not in bucket_names


@pytest.mark.e2e()
def test_compose_app_writes_persisted_logs(
    compose_env: dict[str, str],
    localstack_bucket_pair: object,
) -> None:
    _ = localstack_bucket_pair
    _ = _run_compose(compose_env, "run", "--rm", "app", "check")
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "app",
        "-lc",
        'test -s "$LOG_DIR/s3-archiver.log" && cat "$LOG_DIR/s3-archiver.log"',
    )

    assert '"event": "health.succeeded"' in result.stdout


@pytest.mark.e2e()
def test_compose_app_persists_rotated_logs(
    compose_env: dict[str, str],
    localstack_bucket_pair: object,
) -> None:
    _ = localstack_bucket_pair
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
        "--entrypoint",
        "sh",
        "app",
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
    localstack_bucket_pair: object,
) -> None:
    _ = localstack_bucket_pair
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "-e",
        "LOG_DIR=/proc/s3-archiver",
        "app",
        "check",
        check=False,
    )

    assert result.returncode == LOGGING_ERROR_EXIT_CODE
    payload = _error_payload(result.stderr)
    assert payload["status"] == "error"
    assert "Failed to initialize log directory" in payload["message"]


@pytest.mark.unit()
def test_compose_env_uses_bucket_isolated_log_dir(compose_env: dict[str, str]) -> None:
    bucket_pair = bucket_pair_from_env(compose_env)
    env_file = Path(compose_env["APP_ENV_FILE"])
    env_lines = env_file.read_text(encoding="utf-8").splitlines()

    assert f"LOG_DIR={compose_runtime_log_dir(bucket_pair)}" in env_lines


@pytest.mark.e2e()
def test_runtime_image_excludes_test_and_localstack_assets(compose_env: dict[str, str]) -> None:
    probe = (
        "test ! -e /app/tests && "
        "test ! -e /app/docker/localstack && "
        "test ! -e /opt/s3-archiver-test-support"
    )
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "sh",
        "app",
        "-lc",
        probe,
    )

    assert result.returncode == 0


@pytest.mark.e2e()
def test_compose_scheduler_service_runs_schedule_command(
    compose_env: dict[str, str],
) -> None:
    result = subprocess.run(
        ["docker", "compose", "--profile", "test", "config", "scheduler"],
        cwd=REPO_ROOT,
        env=compose_env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "command:" in result.stdout
    assert "- schedule" in result.stdout
    assert "restart: unless-stopped" in result.stdout
    assert "ARCHIVER_CONFIG_JSON:" in result.stdout


@pytest.mark.e2e()
def test_compose_services_default_to_dotenv_when_app_env_file_unset(tmp_path: Path) -> None:
    _ = shutil.copy(REPO_ROOT / "compose.yaml", tmp_path / "compose.yaml")
    _ = (tmp_path / ".env").write_text("", encoding="utf-8")
    env = os.environ.copy()
    _ = env.pop("APP_ENV_FILE", None)
    _ = env.pop("ENV_FILE", None)

    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "APP_ENV_FILE: .env" in result.stdout
    assert "ARCHIVER_CONFIG_JSON:" in result.stdout


@pytest.mark.e2e()
def test_compose_services_fail_when_env_file_missing(tmp_path: Path) -> None:
    _ = shutil.copy(REPO_ROOT / "compose.yaml", tmp_path / "compose.yaml")
    env = os.environ.copy()
    _ = env.pop("APP_ENV_FILE", None)
    _ = env.pop("ENV_FILE", None)

    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert ".env" in result.stderr


def _run_compose(
    env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_stack_compose(
        env,
        *args,
        check=check,
        extra_retryable_returncodes=(HEALTH_CHECK_ERROR_EXIT_CODE,),
    )


def _error_payload(output: str) -> ErrorPayload:
    return cast(ErrorPayload, cast(object, last_json_object(output)))


def _rotation_payload(output: str) -> RotationPayload:
    return cast(RotationPayload, cast(object, last_json_object(output)))
