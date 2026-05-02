"""Shared test helpers."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest
from botocore.exceptions import BotoCoreError, ClientError
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_RETRY_DELAY_SECONDS = 1.0
_COMPOSE_UP_RETRIES = 3


class BucketBootstrapClient(Protocol):
    def head_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        ...


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


@pytest.fixture()
def localstack_service(compose_env: dict[str, str]) -> Generator[None, None, None]:
    _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)
    try:
        _ = _run_compose(
            compose_env,
            "up",
            "-d",
            "--wait",
            "localstack",
            retries=_COMPOSE_UP_RETRIES,
        )
        _wait_for_localstack_readiness()
        yield
    finally:
        _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)


def _run_compose(
    env: dict[str, str],
    *args: str,
    check: bool = True,
    retries: int = 0,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--profile", "test", *args]
    for attempt in range(retries + 1):
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
        if attempt == retries or _is_non_retryable_compose_error(args, result):
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
        time.sleep(_COMPOSE_RETRY_DELAY_SECONDS)
    raise AssertionError("compose retry loop exhausted without returning")


def _wait_for_localstack_readiness(timeout_seconds: float = 90.0) -> None:
    endpoint = os.environ.get("LOCALSTACK_S3_URL", "http://127.0.0.1:4566")
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise RuntimeError(f"Invalid LOCALSTACK_S3_URL {endpoint!r}")
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{endpoint.rstrip('/')}/_localstack/health"
    settings = AppSettings.from_env(
        {
            "S3_PROVIDER": "localstack",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test",
            "S3_REGION": "us-east-1",
            "S3_BUCKET": "s3-archiver-integration",
            "S3_ENDPOINT_URL": endpoint,
            "S3_ADDRESSING_STYLE": "path",
            "LOG_LEVEL": "INFO",
            "LOG_DIR": str(REPO_ROOT / ".local" / "pytest-logs"),
        }
    )
    while time.monotonic() < deadline:
        if (
            _can_connect(host, port)
            and _healthcheck_responds(health_url)
            and _bucket_is_ready(settings)
        ):
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
    except (HTTPError, URLError, OSError):
        return False


def _bucket_is_ready(settings: AppSettings) -> bool:
    client = cast(BucketBootstrapClient, build_s3_client(settings))
    try:
        _ = client.head_bucket(Bucket=settings.bucket)
    except (BotoCoreError, ClientError):
        return False
    return True


def _is_non_retryable_compose_error(
    args: tuple[str, ...],
    result: subprocess.CompletedProcess[str],
) -> bool:
    if args and args[0] == "up":
        return False
    retryable_messages = ("No such container", "marked for removal")
    return not any(message in result.stderr for message in retryable_messages)
