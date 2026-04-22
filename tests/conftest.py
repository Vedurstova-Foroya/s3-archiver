"""Shared test helpers."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

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
    _ = _run_compose(compose_env, "up", "-d", "--wait", "localstack")
    _wait_for_localstack_host()
    _ensure_localstack_bucket(compose_env)
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


def _ensure_localstack_bucket(env: dict[str, str]) -> None:
    _ = _run_compose(
        env,
        "exec",
        "-T",
        "localstack",
        "sh",
        "-lc",
        "awslocal s3api create-bucket --bucket s3-archiver-integration >/dev/null 2>&1 || true",
    )


def _wait_for_localstack_host(timeout_seconds: float = 30.0) -> None:
    endpoint = os.environ.get("LOCALSTACK_S3_URL", "http://127.0.0.1:4566")
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise RuntimeError(f"Invalid LOCALSTACK_S3_URL {endpoint!r}")
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{endpoint.rstrip('/')}/_localstack/health"
    while time.monotonic() < deadline:
        if _can_connect(host, port) and _healthcheck_responds(health_url):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for LocalStack host endpoint {endpoint!r}")


def _can_connect(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def _healthcheck_responds(health_url: str) -> bool:
    try:
        with urlopen(health_url, timeout=1.0):
            return True
    except (HTTPError, URLError):
        return False
