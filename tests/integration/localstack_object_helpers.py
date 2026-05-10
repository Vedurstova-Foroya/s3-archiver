"""Host-side LocalStack object helpers for integration and e2e tests."""

from __future__ import annotations

import gzip
import tarfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Literal, cast

from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody
from s3_archiver_core.s3 import S3Client, build_s3_client
from s3_archiver_core.settings import AppSettings

_RETRYABLE_LOCALSTACK_ERRORS = (
    "Connection was closed before we received a valid response",
    "Could not connect to the endpoint URL",
)
CANONICAL_RETENTION_DATASET_DAYS = tuple(range(366))
_WRITE_RETRY_ATTEMPTS = 20
_WRITE_RETRY_DELAY_SECONDS = 1.0


def localstack_s3_client(
    env: Mapping[str, str], side: Literal["source", "destination"]
) -> S3Client:
    settings = AppSettings.from_env(env)
    return build_s3_client(settings.source if side == "source" else settings.destination)


def put_test_object(
    client: S3Client,
    bucket: str,
    key: str,
    *,
    body: bytes | None = None,
    metadata: Mapping[str, str] | None = None,
    tags: Mapping[str, str] | None = None,
    **kwargs: object,
) -> dict[str, object]:
    response = dict(
        _retry_localstack_call(
            lambda: client.put_object(
                Bucket=bucket,
                Key=key,
                Body=f"payload for {key}\n".encode() if body is None else body,
                Metadata=dict(metadata or {}),
                **kwargs,
            ),
            attempts=_WRITE_RETRY_ATTEMPTS,
            delay_seconds=_WRITE_RETRY_DELAY_SECONDS,
        )
    )
    if tags:
        _ = _retry_localstack_call(
            lambda: client.put_object_tagging(
                Bucket=bucket,
                Key=key,
                Tagging={
                    "TagSet": [{"Key": tag, "Value": value} for tag, value in sorted(tags.items())]
                },
            ),
            attempts=_WRITE_RETRY_ATTEMPTS,
            delay_seconds=_WRITE_RETRY_DELAY_SECONDS,
        )
    return response


def listed_keys(client: S3Client, bucket: str) -> set[str]:
    keys: set[str] = set()
    start_after: str | None = None
    while True:
        kwargs: dict[str, object] = {"Bucket": bucket}
        if start_after is not None:
            kwargs["StartAfter"] = start_after
        response = _retry_localstack_call(lambda kwargs=kwargs: client.list_objects_v2(**kwargs))
        last_key: str | None = None
        for entry in _object_entries(response.get("Contents")):
            if entry.get("Key") is not None:
                last_key = str(entry["Key"])
                keys.add(last_key)
        if response.get("IsTruncated") is not True:
            return keys
        if last_key is None:
            raise AssertionError("LocalStack returned a truncated empty object page")
        start_after = last_key


def listed_key_versions(client: S3Client, bucket: str, key: str) -> list[tuple[str, str, bool]]:
    versions = _retry_localstack_call(
        lambda: client.list_object_versions(Bucket=bucket, Prefix=key)
    ).get("Versions")
    return [
        (str(entry["Key"]), str(entry["VersionId"]), entry.get("IsLatest") is True)
        for entry in _object_entries(versions)
        if entry.get("Key") == key and entry.get("VersionId") is not None
    ]


def read_object_text(client: S3Client, bucket: str, key: str) -> str:
    response = _retry_localstack_call(lambda: client.get_object(Bucket=bucket, Key=key))
    return cast(StreamingBody, response["Body"]).read().decode()


def read_object_bytes(client: S3Client, bucket: str, key: str) -> bytes:
    response = _retry_localstack_call(lambda: client.get_object(Bucket=bucket, Key=key))
    return cast(StreamingBody, response["Body"]).read()


def read_tar_gz_members_text(client: S3Client, bucket: str, key: str) -> dict[str, str]:
    payload = read_object_bytes(client, bucket, key)
    with (
        gzip.GzipFile(fileobj=BytesIO(payload), mode="rb") as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="r:") as archive,
    ):
        members: dict[str, str] = {}
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            members[member.name] = extracted.read().decode()
        return members


def read_tar_gz_member_pax_headers(
    client: S3Client, bucket: str, key: str
) -> dict[str, dict[str, str]]:
    payload = read_object_bytes(client, bucket, key)
    with (
        gzip.GzipFile(fileobj=BytesIO(payload), mode="rb") as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="r:") as archive,
    ):
        return {member.name: dict(member.pax_headers) for member in archive.getmembers()}


def seed_timestamped_objects(
    client: S3Client,
    bucket: str,
    *,
    prefix: str,
    days: tuple[int, ...],
    seed_now: datetime,
) -> None:
    seeded_now = seed_now.astimezone(UTC).replace(microsecond=0)
    for day in days:
        target = seeded_now - timedelta(days=day)
        _ = put_test_object(
            client,
            bucket,
            f"{prefix}/age-{day}-days.txt",
            metadata={
                "s3-archiver-test-age-days": str(day),
                "s3-archiver-test-last-modified": target.isoformat(),
            },
        )


def seed_canonical_retention_dataset(
    client: S3Client,
    bucket: str,
    *,
    prefix: str,
    seed_now: datetime,
) -> None:
    seed_timestamped_objects(
        client,
        bucket,
        prefix=prefix,
        days=CANONICAL_RETENTION_DATASET_DAYS,
        seed_now=seed_now,
    )


def retention_dataset_keys(
    prefix: str, *, days: tuple[int, ...] = CANONICAL_RETENTION_DATASET_DAYS
) -> set[str]:
    return {f"{prefix}/age-{day}-days.txt" for day in days}


def eligible_retention_days(
    retention_days: int,
    *,
    days: tuple[int, ...] = CANONICAL_RETENTION_DATASET_DAYS,
) -> set[int]:
    return {day for day in days if day > retention_days}


def _object_entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, object]] = []
    for raw_entry in cast(list[object], value):
        if isinstance(raw_entry, dict):
            entries.append(cast(dict[str, object], raw_entry))
    return entries


def _retry_localstack_call(
    operation: Callable[[], Mapping[str, object]],
    *,
    attempts: int = 5,
    delay_seconds: float = 0.5,
) -> Mapping[str, object]:
    for attempt in range(attempts):
        try:
            return operation()
        except (BotoCoreError, ClientError) as exc:
            if attempt == attempts - 1 or not _is_retryable_localstack_error(exc):
                raise
            time.sleep(delay_seconds)
    raise AssertionError("LocalStack retry loop exhausted without returning")


def _is_retryable_localstack_error(exc: Exception) -> bool:
    message = str(exc)
    return any(detail in message for detail in _RETRYABLE_LOCALSTACK_ERRORS)
