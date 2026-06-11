"""S3 source-object delete helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import cast

from botocore.exceptions import ClientError

from s3_archiver_core._archive_s3_helpers import (
    is_not_implemented_error,
    optional_string,
    versioned_kwargs,
)
from s3_archiver_core.s3 import S3Client
from s3_archiver_core.source_deletes import SourceDeleteFailure, SourceDeleteRequest

MAX_DELETE_OBJECTS = 1000


def delete_source_object(
    client: S3Client,
    bucket: str,
    key: str,
    version_id: str | None = None,
    *,
    if_match: str | None = None,
) -> None:
    """Delete one source object, targeting the exact version when supplied."""

    kwargs = versioned_kwargs(bucket, key, version_id)
    if if_match is None:
        _ = client.delete_object(**kwargs)
        return
    try:
        _ = client.delete_object(**(kwargs | {"IfMatch": if_match}))
    except ClientError as exc:
        if not is_not_implemented_error(exc):
            raise
        try:
            current = _head_object(client, bucket, key, version_id)
        except ClientError as head_exc:
            raise exc from head_exc
        if current.get("ETag") != if_match:
            raise
        _ = client.delete_object(**kwargs)


def delete_source_objects(
    client: S3Client,
    bucket: str,
    objects: Sequence[SourceDeleteRequest],
) -> tuple[SourceDeleteFailure, ...]:
    """Delete source objects in S3 DeleteObjects batches."""

    if any(item.if_match is not None for item in objects):
        raise NotImplementedError("batch conditional delete is not supported")
    failures: list[SourceDeleteFailure] = []
    for batch in _chunks(objects, MAX_DELETE_OBJECTS):
        identifiers = [_delete_identifier(item) for item in batch]
        try:
            response = _delete_objects(
                client,
                Bucket=bucket,
                Delete={"Objects": identifiers, "Quiet": True},
            )
        except ClientError as exc:
            if is_not_implemented_error(exc):
                raise NotImplementedError("batch delete is not supported") from exc
            failures.extend(_batch_exception_failures(batch, exc))
            continue
        failures.extend(_response_failures(response))
    return tuple(failures)


def _head_object(
    client: S3Client, bucket: str, key: str, version_id: str | None
) -> Mapping[str, object]:
    return client.head_object(**versioned_kwargs(bucket, key, version_id))


def _delete_objects(client: S3Client, **kwargs: object) -> Mapping[str, object]:
    candidate = cast(object, getattr(client, "delete_objects", None))
    if not callable(candidate):
        raise NotImplementedError("batch delete is not supported")
    delete_objects = cast(Callable[..., Mapping[str, object]], candidate)
    return delete_objects(**kwargs)


def _delete_identifier(item: SourceDeleteRequest) -> dict[str, str]:
    identifier = {"Key": item.key}
    if item.version_id is not None:
        identifier["VersionId"] = item.version_id
    return identifier


def _chunks(
    objects: Sequence[SourceDeleteRequest], size: int
) -> Iterator[Sequence[SourceDeleteRequest]]:
    for start in range(0, len(objects), size):
        yield objects[start : start + size]


def _batch_exception_failures(
    objects: Sequence[SourceDeleteRequest], exc: Exception
) -> tuple[SourceDeleteFailure, ...]:
    return tuple(
        SourceDeleteFailure(item.key, item.version_id, f"delete failed: {exc}") for item in objects
    )


def _response_failures(response: Mapping[str, object]) -> tuple[SourceDeleteFailure, ...]:
    raw_errors = response.get("Errors", [])
    if not isinstance(raw_errors, list):
        return ()
    failures: list[SourceDeleteFailure] = []
    for raw_error in cast(list[object], raw_errors):
        if not isinstance(raw_error, dict):
            continue
        error = cast(Mapping[object, object], raw_error)
        key = optional_string(error.get("Key"))
        if key is None:
            continue
        failures.append(
            SourceDeleteFailure(
                key,
                optional_string(error.get("VersionId")),
                f"delete failed: {_delete_error_detail(error)}",
            )
        )
    return tuple(failures)


def _delete_error_detail(error: Mapping[object, object]) -> str:
    code = optional_string(error.get("Code")) or "Unknown"
    message = optional_string(error.get("Message"))
    if message is None or message == code:
        return code
    return f"{code}: {message}"
