"""Shared test helpers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "S3_PROVIDER": "oci",
        "S3_ACCESS_KEY_ID": "access-key",
        "S3_SECRET_ACCESS_KEY": "secret-key",
        "S3_REGION": "eu-frankfurt-1",
        "S3_NAMESPACE": "tenant",
        "S3_BUCKET": "archive-bucket",
        "OCI_IAM_USER_OCID": "ocid1.user.oc1..example",
        "S3_ADDRESSING_STYLE": "path",
        "LOG_LEVEL": "INFO",
        "LOG_DIR": str(tmp_path / "logs"),
    }


@pytest.fixture(scope="session")
def compose_env() -> dict[str, str]:
    env = os.environ.copy()
    env["APP_ENV_FILE"] = ".env.e2e"
    return env


@pytest.fixture(scope="session")
def localstack_service(compose_env: dict[str, str]) -> Generator[None, None, None]:
    _ = _run_compose(compose_env, "up", "-d", "localstack")
    yield
    _ = _run_compose(compose_env, "down", "-v", "--remove-orphans")


def _run_compose(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "--profile", "test", *args],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
