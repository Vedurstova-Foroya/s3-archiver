"""Shared test helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from s3_archiver_core.settings import AppSettings
from s3_archiver_localstack_support.buckets import (
    delete_localstack_bucket_pair,
    ensure_localstack_bucket_pair,
    localstack_admin_client,
)
from s3_archiver_localstack_support.compose import find_repo_root, run_compose
from s3_archiver_localstack_support.harness import (
    LOCALSTACK_COMPOSE_ENDPOINT,
    LOCALSTACK_HOST_ENDPOINT,
    LocalstackBucketPair,
    assert_localstack_test_target,
    bucket_pair_from_env,
    compose_runtime_log_dir,
    localstack_compose_env,
    localstack_test_env,
    new_localstack_bucket_pair,
    write_localstack_env_file,
)
from s3_archiver_localstack_support.readiness import wait_for_localstack_readiness

REPO_ROOT = find_repo_root()
_COMPOSE_RETRY_DELAY_SECONDS = 1.0
_COMPOSE_UP_RETRIES = 3


@pytest.fixture()
def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "S3_SOURCE_PROVIDER": "oci",
        "S3_SOURCE_ACCESS_KEY": "access-key",
        "S3_SOURCE_SECRET_KEY": "secret-key",
        "S3_SOURCE_REGION": "eu-frankfurt-1",
        "S3_SOURCE_NAMESPACE": "tenant",
        "S3_SOURCE_BUCKET": "archive-bucket",
        "S3_SOURCE_IAM_USER_OCID": "ocid1.user.oc1..example",
        "S3_SOURCE_ENDPOINT": (
            "https://tenant.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"
        ),
        "S3_SOURCE_ADDRESSING_STYLE": "path",
        "S3_DESTINATION_PROVIDER": "localstack",
        "S3_DESTINATION_ACCESS_KEY": "destination-access",
        "S3_DESTINATION_SECRET_KEY": "destination-secret",
        "S3_DESTINATION_REGION": "us-east-1",
        "S3_DESTINATION_BUCKET": "destination-bucket",
        "S3_DESTINATION_ENDPOINT": "http://localstack:4566",
        "S3_DESTINATION_ADDRESSING_STYLE": "path",
        "ARCHIVER_CONFIG_JSON": (
            '[{"name":"default","parser":"filename_timestamp","copy_mode":"daily_tar_gz",'
            '"source":{"provider":"${S3_SOURCE_PROVIDER}",'
            '"endpoint_url":"${S3_SOURCE_ENDPOINT}",'
            '"region":"${S3_SOURCE_REGION}","namespace":"${S3_SOURCE_NAMESPACE}",'
            '"bucket":"${S3_SOURCE_BUCKET}","iam_user_ocid":"${S3_SOURCE_IAM_USER_OCID}",'
            '"path":"","access_key_id":"${S3_SOURCE_ACCESS_KEY}",'
            '"secret_access_key":"${S3_SOURCE_SECRET_KEY}",'
            '"addressing_style":"${S3_SOURCE_ADDRESSING_STYLE}"},'
            '"destination":{"provider":"${S3_DESTINATION_PROVIDER}",'
            '"endpoint_url":"${S3_DESTINATION_ENDPOINT}",'
            '"region":"${S3_DESTINATION_REGION}","bucket":"${S3_DESTINATION_BUCKET}",'
            '"path":"",'
            '"access_key_id":"${S3_DESTINATION_ACCESS_KEY}",'
            '"secret_access_key":"${S3_DESTINATION_SECRET_KEY}",'
            '"addressing_style":"${S3_DESTINATION_ADDRESSING_STYLE}"}}]'
        ),
        "LOG_LEVEL": "INFO",
        "LOG_DIR": str(tmp_path / "logs"),
    }


@pytest.fixture()
def compose_env(tmp_path: Path) -> dict[str, str]:
    bucket_pair = new_localstack_bucket_pair()
    app_env_file = write_localstack_env_file(
        tmp_path,
        bucket_pair,
        endpoint=LOCALSTACK_COMPOSE_ENDPOINT,
        log_dir=compose_runtime_log_dir(bucket_pair),
    )
    return localstack_compose_env(bucket_pair, app_env_file=app_env_file)


@pytest.fixture()
def localstack_service(compose_env: dict[str, str]) -> Generator[None, None, None]:
    _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)
    try:
        _ = _run_compose(
            compose_env,
            "up",
            "-d",
            "localstack",
            retries=_COMPOSE_UP_RETRIES,
        )
        _wait_for_localstack_readiness(env=compose_env)
        yield
    finally:
        _ = _run_compose(compose_env, "down", "-v", "--remove-orphans", check=False)


@pytest.fixture()
def localstack_bucket_pair(
    compose_env: dict[str, str],
    localstack_service: None,
) -> Generator[LocalstackBucketPair, None, None]:
    _ = localstack_service
    bucket_pair = bucket_pair_from_env(compose_env)
    test_env = localstack_test_env(
        bucket_pair,
        endpoint=compose_env.get("LOCALSTACK_S3_URL", LOCALSTACK_HOST_ENDPOINT),
        log_dir=str(REPO_ROOT / ".local" / "pytest-logs"),
    )
    assert_localstack_test_target(test_env)
    settings = AppSettings.from_env(test_env)
    client = localstack_admin_client(settings)
    ensure_localstack_bucket_pair(client, bucket_pair)
    try:
        yield bucket_pair
    finally:
        delete_localstack_bucket_pair(client, bucket_pair)


def _run_compose(
    env: dict[str, str],
    *args: str,
    check: bool = True,
    retries: int = 0,
) -> subprocess.CompletedProcess[str]:
    return run_compose(
        env,
        *args,
        check=check,
        retries=retries,
        retry_delay_seconds=_COMPOSE_RETRY_DELAY_SECONDS,
        repo_root=REPO_ROOT,
    )


def _wait_for_localstack_readiness(
    timeout_seconds: float = 90.0,
    *,
    env: dict[str, str] | None = None,
) -> None:
    endpoint_source = {} if env is None else env
    endpoint = endpoint_source.get("LOCALSTACK_S3_URL", LOCALSTACK_HOST_ENDPOINT)
    wait_for_localstack_readiness(
        endpoint=endpoint,
        log_dir=str(REPO_ROOT / ".local" / "pytest-logs"),
        timeout_seconds=timeout_seconds,
    )
