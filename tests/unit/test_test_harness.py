"""Unit tests for the shared integration/e2e harness."""

from __future__ import annotations

import importlib
import socket
import subprocess
from collections.abc import Callable
from types import ModuleType
from typing import cast
from urllib.error import URLError

import pytest
from botocore.exceptions import ClientError
from s3_archiver_core.settings import AppSettings

harness = importlib.import_module("conftest")
TIME_MODULE = cast(ModuleType, harness.__dict__["time"])
RUN_COMPOSE = cast(
    Callable[..., subprocess.CompletedProcess[str]],
    harness.__dict__["_run_compose"],
)
WAIT_FOR_LOCALSTACK_READINESS = cast(
    Callable[[float], None],
    harness.__dict__["_wait_for_localstack_readiness"],
)
CAN_CONNECT = cast(Callable[[str, int], bool], harness.__dict__["_can_connect"])
HEALTHCHECK_RESPONDS = cast(Callable[[str], bool], harness.__dict__["_healthcheck_responds"])
BUCKET_IS_READY = cast(Callable[[AppSettings], bool], harness.__dict__["_bucket_is_ready"])


class FakeSocket:
    """Minimal socket test double for `_can_connect`."""

    def __init__(self, return_code: int) -> None:
        self.return_code: int = return_code
        self.timeout: float | None = None

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect_ex(self, address: tuple[str, int]) -> int:
        _ = address
        return self.return_code


class SuccessResponse:
    """Context manager used to simulate a healthy HTTP probe."""

    def __enter__(self) -> SuccessResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class BucketClientSuccess:
    """Client double that reports a ready bucket."""

    def head_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        return {"Bucket": Bucket}


class BucketClientFailure:
    """Client double that reports a missing bucket."""

    def head_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        _ = Bucket
        raise ClientError(
            {"Error": {"Code": "404", "Message": "missing"}},
            "HeadBucket",
        )


def _base_localstack_settings() -> AppSettings:
    return AppSettings.from_env(
        {
            "S3_PROVIDER": "localstack",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test",
            "S3_REGION": "us-east-1",
            "S3_BUCKET": "s3-archiver-integration",
            "S3_ENDPOINT_URL": "http://127.0.0.1:4566",
            "S3_ADDRESSING_STYLE": "path",
            "LOG_LEVEL": "INFO",
            "LOG_DIR": "/tmp/s3-archiver-logs",
        }
    )


def _completed_process(
    args: tuple[str, ...],
    returncode: int,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.mark.unit()
def test_run_compose_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[str, ...]] = []
    sleeps: list[float] = []
    results = iter(
        (
            _completed_process(("docker", "compose"), 1, stderr="marked for removal"),
            _completed_process(("docker", "compose"), 0, stdout="ok"),
        )
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(("docker", "compose"))
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(TIME_MODULE, "sleep", sleeps.append)

    result = RUN_COMPOSE({}, "down", retries=1)

    assert result.returncode == 0
    assert len(attempts) == 2
    assert sleeps == [1.0]


@pytest.mark.unit()
def test_run_compose_raises_on_non_retryable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed_process(("docker", "compose"), 1, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        _ = RUN_COMPOSE({}, "down", retries=1)


@pytest.mark.unit()
def test_can_connect_returns_true_for_open_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_socket(_family: int, _kind: int) -> FakeSocket:
        return FakeSocket(0)

    monkeypatch.setattr(socket, "socket", fake_socket)

    assert CAN_CONNECT("127.0.0.1", 4566) is True


@pytest.mark.unit()
def test_healthcheck_responds_handles_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def healthy_urlopen(*_args: object, **_kwargs: object) -> SuccessResponse:
        return SuccessResponse()

    monkeypatch.setattr(harness, "urlopen", healthy_urlopen)

    assert HEALTHCHECK_RESPONDS("http://127.0.0.1:4566/_localstack/health") is True

    def raise_url_error(*_args: object, **_kwargs: object) -> SuccessResponse:
        raise URLError("offline")

    monkeypatch.setattr(harness, "urlopen", raise_url_error)

    assert HEALTHCHECK_RESPONDS("http://127.0.0.1:4566/_localstack/health") is False


@pytest.mark.unit()
def test_bucket_is_ready_reports_client_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def ready_client(_settings: AppSettings) -> BucketClientSuccess:
        return BucketClientSuccess()

    monkeypatch.setattr(harness, "build_s3_client", ready_client)

    assert BUCKET_IS_READY(_base_localstack_settings()) is True

    def missing_client(_settings: AppSettings) -> BucketClientFailure:
        return BucketClientFailure()

    monkeypatch.setattr(harness, "build_s3_client", missing_client)

    assert BUCKET_IS_READY(_base_localstack_settings()) is False


@pytest.mark.unit()
def test_wait_for_localstack_readiness_retries_until_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter((0.0, 0.0, 0.5, 0.5))
    can_connect_values = iter((False, True))
    health_values = iter((True,))
    bucket_values = iter((True,))
    sleeps: list[float] = []

    monkeypatch.delenv("LOCALSTACK_S3_URL", raising=False)
    monkeypatch.setattr(TIME_MODULE, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(TIME_MODULE, "sleep", sleeps.append)

    def fake_can_connect(*_args: object) -> bool:
        return next(can_connect_values)

    def fake_healthcheck_responds(*_args: object) -> bool:
        return next(health_values)

    def fake_bucket_is_ready(*_args: object) -> bool:
        return next(bucket_values)

    monkeypatch.setattr(harness, "_can_connect", fake_can_connect)
    monkeypatch.setattr(harness, "_healthcheck_responds", fake_healthcheck_responds)
    monkeypatch.setattr(harness, "_bucket_is_ready", fake_bucket_is_ready)

    WAIT_FOR_LOCALSTACK_READINESS(1.0)

    assert sleeps == [0.5]


@pytest.mark.unit()
def test_wait_for_localstack_readiness_rejects_invalid_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALSTACK_S3_URL", "not-a-url")

    with pytest.raises(RuntimeError, match="Invalid LOCALSTACK_S3_URL"):
        WAIT_FOR_LOCALSTACK_READINESS(1.0)
