"""LocalStack-only test harness helpers."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

from botocore.exceptions import BotoCoreError, ClientError

from s3_archiver_localstack_support._common import is_retryable_localstack_error, object_entries

LOCALSTACK_HOST_ENDPOINT = "http://127.0.0.1:4566"
LOCALSTACK_COMPOSE_ENDPOINT = "http://localstack:4566"
LOCALSTACK_ENDPOINT_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "localstack", "localstack-alt", "localhost.localstack.cloud"}
)


@dataclass(frozen=True, slots=True)
class LocalstackBucketPair:
    """Source and destination bucket names for an isolated LocalStack run."""

    source: str
    destination: str


class LocalstackS3AdminClient(Protocol):
    """Subset of S3 admin operations used to prepare and clean LocalStack buckets.

    PEP 544 structural type — the ``...`` method bodies are interface stubs,
    not abstract methods. The real boto3 S3 client satisfies it by matching
    the shape, so no subclassing is needed.
    """

    def head_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        """Check whether a bucket exists."""
        ...

    def create_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        """Create a bucket."""
        ...

    def list_buckets(self) -> object:
        """List buckets."""
        ...

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        """List current object versions."""
        ...

    def list_object_versions(self, **kwargs: object) -> dict[str, object]:
        """List all object versions and delete markers."""
        ...

    def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> object:  # noqa: N803
        """Delete object versions from a bucket."""
        ...

    def delete_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        """Delete an empty bucket."""
        ...


def new_localstack_bucket_pair() -> LocalstackBucketPair:
    """Return unique source and destination bucket names for one isolated run."""

    suffix = uuid.uuid4().hex
    return LocalstackBucketPair(
        source=f"s3-archiver-source-{suffix}",
        destination=f"s3-archiver-destination-{suffix}",
    )


def bucket_pair_from_env(env: dict[str, str]) -> LocalstackBucketPair:
    """Read an isolated LocalStack bucket pair from a compose environment."""

    return LocalstackBucketPair(env["TEST_S3_SOURCE_BUCKET"], env["TEST_S3_DESTINATION_BUCKET"])


def localstack_compose_env(
    bucket_pair: LocalstackBucketPair,
    *,
    app_env_file: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build Docker Compose environment variables for an isolated LocalStack run."""

    env = dict(os.environ if environ is None else environ)
    localstack_host_endpoint = env.get("LOCALSTACK_S3_URL", LOCALSTACK_HOST_ENDPOINT)
    env["APP_ENV_FILE"] = str(app_env_file)
    env["LOCALSTACK_S3_URL"] = localstack_host_endpoint
    if (localstack_host_port := urlparse(localstack_host_endpoint).port) is not None:
        env["LOCALSTACK_HOST_PORT"] = str(localstack_host_port)
    else:
        _ = env.pop("LOCALSTACK_HOST_PORT", None)
    env["TEST_S3_SOURCE_BUCKET"] = bucket_pair.source
    env["TEST_S3_DESTINATION_BUCKET"] = bucket_pair.destination
    return env


def localstack_test_env(
    bucket_pair: LocalstackBucketPair,
    *,
    endpoint: str,
    log_dir: str,
) -> dict[str, str]:
    """Build app environment variables for an isolated LocalStack bucket pair."""

    env = {
        "APP_ENV_FILE": "/dev/null",
        "S3_SOURCE_PROVIDER": "localstack",
        "S3_SOURCE_ACCESS_KEY": "source-test",
        "S3_SOURCE_SECRET_KEY": "source-test",
        "S3_SOURCE_REGION": "us-east-1",
        "S3_SOURCE_BUCKET": bucket_pair.source,
        "S3_SOURCE_ENDPOINT": endpoint,
        "S3_SOURCE_ADDRESSING_STYLE": "path",
        "S3_DESTINATION_PROVIDER": "localstack",
        "S3_DESTINATION_ACCESS_KEY": "destination-test",
        "S3_DESTINATION_SECRET_KEY": "destination-test",
        "S3_DESTINATION_REGION": "us-east-1",
        "S3_DESTINATION_BUCKET": bucket_pair.destination,
        "S3_DESTINATION_ENDPOINT": endpoint,
        "S3_DESTINATION_ADDRESSING_STYLE": "path",
        "ARCHIVER_CONFIG_JSON": _localstack_config_json(bucket_pair, endpoint=endpoint),
        "ARCHIVER_RUN_TIMEOUT": "7d",
        "LOG_LEVEL": "INFO",
        "LOG_DIR": log_dir,
    }
    assert_localstack_test_target(env)
    return env


def compose_runtime_log_dir(bucket_pair: LocalstackBucketPair) -> str:
    """Return the in-container log directory for a compose-backed run."""

    return f"/var/log/s3-archiver/{bucket_pair.source}"


def _localstack_config_json(bucket_pair: LocalstackBucketPair, *, endpoint: str) -> str:
    _ = endpoint
    return (
        '[{"name":"localstack-daily","parser":"filename_timestamp",'
        '"copy_mode":"daily_tar_gz",'
        f'"source":{{"bucket":"{bucket_pair.source}","path":""}},'
        f'"destination":{{"bucket":"{bucket_pair.destination}"}}}}]'
    )


def write_localstack_env_file(
    tmp_path: Path,
    bucket_pair: LocalstackBucketPair,
    *,
    endpoint: str,
    log_dir: str,
    filename: str = "localstack.env",
    overrides: Mapping[str, str] | None = None,
) -> Path:
    """Write a LocalStack app environment file and return its path."""

    env = localstack_test_env(bucket_pair, endpoint=endpoint, log_dir=log_dir)
    if overrides is not None:
        env.update(overrides)
    return write_env_file(tmp_path / filename, env)


def write_env_file(env_file: Path, env: Mapping[str, str]) -> Path:
    """Write sorted key-value environment entries and return the file path."""

    _ = env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(env.items())),
        encoding="utf-8",
    )
    return env_file


def assert_localstack_test_target(env: Mapping[str, str]) -> None:
    """Reject non-LocalStack endpoints before destructive test operations run."""

    for field in ("S3_SOURCE_PROVIDER", "S3_DESTINATION_PROVIDER"):
        if env.get(field) != "localstack":
            raise RuntimeError(f"{field} must be 'localstack' for integration/e2e tests")
    for field in ("S3_SOURCE_ENDPOINT", "S3_DESTINATION_ENDPOINT"):
        _assert_localstack_endpoint(field, env.get(field))


def ensure_localstack_bucket(client: LocalstackS3AdminClient, bucket: str) -> None:
    """Create a LocalStack bucket if it is not already present."""

    try:
        _ = client.create_bucket(Bucket=bucket)
    except ClientError as exc:
        error_obj: object = exc.response.get("Error", {})
        error = cast(dict[str, object], error_obj)
        if error.get("Code") not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise


def delete_localstack_bucket(client: LocalstackS3AdminClient, bucket: str) -> None:
    """Delete a LocalStack bucket and all object versions it contains."""

    for attempt in range(5):  # pragma: no branch
        try:
            _delete_all_versions(client, bucket)
            _delete_current_objects(client, bucket)
            _ = client.delete_bucket(Bucket=bucket)
            return
        except (BotoCoreError, ClientError) as exc:
            if (
                isinstance(exc, ClientError)
                and exc.response.get("Error", {}).get("Code") == "NoSuchBucket"
            ):
                return
            if attempt == 4 or not is_retryable_localstack_error(exc):
                raise RuntimeError(
                    f"Failed to delete LocalStack test bucket {bucket!r}: {exc}"
                ) from exc
            time.sleep(0.5)


def _assert_localstack_endpoint(field: str, endpoint: str | None) -> None:
    if endpoint is None:
        raise RuntimeError(f"{field} must be set for integration/e2e tests")
    host = urlparse(endpoint).hostname
    if host not in LOCALSTACK_ENDPOINT_HOSTS:
        raise RuntimeError(f"{field} host {host!r} is not allowed for integration/e2e tests")


def _delete_all_versions(client: LocalstackS3AdminClient, bucket: str) -> None:
    key_marker: str | None = None
    version_marker: str | None = None
    while True:
        kwargs: dict[str, object] = {"Bucket": bucket, "MaxKeys": 1000}
        if key_marker is not None:
            kwargs["KeyMarker"] = key_marker
        if version_marker is not None:
            kwargs["VersionIdMarker"] = version_marker
        page = client.list_object_versions(**kwargs)
        objects = _version_delete_entries(page)
        if objects:
            _ = client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if page.get("IsTruncated") is not True:
            return
        key_marker = _optional_string(page.get("NextKeyMarker"))
        version_marker = _optional_string(page.get("NextVersionIdMarker"))


def _delete_current_objects(client: LocalstackS3AdminClient, bucket: str) -> None:
    continuation_token: str | None = None
    while True:
        kwargs: dict[str, object] = {"Bucket": bucket, "Prefix": "", "MaxKeys": 1000}
        if continuation_token is not None:
            kwargs["ContinuationToken"] = continuation_token
        page = client.list_objects_v2(**kwargs)
        objects = _current_delete_entries(page)
        if objects:
            _ = client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if page.get("IsTruncated") is not True:
            return
        continuation_token = _optional_string(page.get("NextContinuationToken"))


def _version_delete_entries(page: dict[str, object]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for section in ("Versions", "DeleteMarkers"):
        for item in object_entries(page.get(section)):
            key = _optional_string(item.get("Key"))
            version_id = _optional_string(item.get("VersionId"))
            if key is not None and version_id is not None:
                entries.append({"Key": key, "VersionId": version_id})
    return entries


def _current_delete_entries(page: dict[str, object]) -> list[dict[str, str]]:
    return [
        {"Key": key}
        for item in object_entries(page.get("Contents"))
        if (key := _optional_string(item.get("Key"))) is not None
    ]


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
