"""End-to-end tests for the Docker Compose stack."""

from __future__ import annotations

import subprocess

import pytest


@pytest.mark.e2e()
def test_compose_app_healthcheck_succeeds(
    compose_env: dict[str, str],
    localstack_service: None,
) -> None:
    _ = localstack_service
    result = _run_compose(compose_env, "run", "--rm", "app")
    final_line = result.stdout.strip().splitlines()[-1]

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


def _run_compose(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "--profile", "test", *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
