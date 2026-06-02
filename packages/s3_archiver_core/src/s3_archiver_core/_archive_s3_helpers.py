"""Private helpers for the S3 archive bucket adapter."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, cast

from botocore.exceptions import ClientError

from s3_archiver_core.s3 import (
    S3Client,
    S3ObjectProperties,
    VersioningState,
    checksums_from_head_fields,
)


class ReadableBody(Protocol):
    """Readable streaming body returned by S3 get-object calls.

    PEP 544 structural type — the ``...`` method bodies are interface stubs,
    not abstract methods. ``botocore.StreamingBody`` and the in-memory test
    fakes both satisfy it by matching the shape.
    """

    def read(self, amt: int = -1) -> bytes:
        """Read up to ``amt`` bytes."""
        ...

    def close(self) -> None:
        """Close the body when supported."""
        ...


def versioned_kwargs(bucket: str, key: str, version_id: str | None) -> dict[str, object]:
    kwargs: dict[str, object] = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        kwargs["VersionId"] = version_id
    return kwargs


def copy_source_kwargs(bucket: str, key: str, version_id: str | None) -> dict[str, str]:
    source = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        source["VersionId"] = version_id
    return source


def close_body(body: ReadableBody) -> None:
    close = getattr(body, "close", None)
    if callable(close):
        _ = close()


def parse_versioning_state(status: object) -> VersioningState:
    if status == "Enabled":
        return "Enabled"
    if status == "Suspended":
        return "Suspended"
    return "Disabled"


def properties_from_head(head: Mapping[str, object], tags: Mapping[str, str]) -> S3ObjectProperties:
    return S3ObjectProperties(
        size=optional_int(head.get("ContentLength"), 0),
        etag=optional_string(head.get("ETag")),
        content_type=optional_string(head.get("ContentType")),
        content_encoding=optional_string(head.get("ContentEncoding")),
        content_language=optional_string(head.get("ContentLanguage")),
        content_disposition=optional_string(head.get("ContentDisposition")),
        cache_control=optional_string(head.get("CacheControl")),
        expires=cast(datetime | None, head.get("Expires")),
        metadata=string_mapping(head.get("Metadata")),
        tags=tags,
        last_modified=cast(datetime | None, head.get("LastModified")),
        checksums=checksums_from_head_fields(head),
        checksum_type=optional_string(head.get("ChecksumType")),
    )


def is_not_found_error(exc: ClientError) -> bool:
    response = cast(Mapping[str, object], exc.response)
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = None
    status = None
    if isinstance(error, dict):
        code = cast(Mapping[object, object], error).get("Code")
    if isinstance(metadata, dict):
        status = cast(Mapping[object, object], metadata).get("HTTPStatusCode")
    return str(code) in {"404", "NoSuchKey", "NotFound"} or status == 404


def supports_checksum_mode(exc: ClientError) -> bool:
    response = cast(Mapping[str, object], exc.response)
    metadata = response.get("ResponseMetadata")
    status = None
    if isinstance(metadata, dict):
        status = cast(Mapping[object, object], metadata).get("HTTPStatusCode")
    return status in {400, 403}


def is_not_implemented_error(exc: ClientError) -> bool:
    """Return True when the provider reports the operation is unsupported.

    OCI Object Storage returns this for tagging and may return it for other
    operations (e.g. GetBucketVersioning) that have no S3-compatible equivalent
    in its own data model.
    """

    response = cast(Mapping[str, object], exc.response)
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = None
    status = None
    if isinstance(error, dict):
        code = cast(Mapping[object, object], error).get("Code")
    if isinstance(metadata, dict):
        status = cast(Mapping[object, object], metadata).get("HTTPStatusCode")
    return str(code) == "NotImplemented" or status == 501


def put_object_tags(client: S3Client, bucket: str, key: str, tags: Mapping[str, str]) -> None:
    """Apply tags to an object, gracefully skipping providers that reject tagging."""

    if not tags:
        return
    tag_set = [{"Key": tag_key, "Value": value} for tag_key, value in sorted(tags.items())]
    try:
        _ = client.put_object_tagging(Bucket=bucket, Key=key, Tagging={"TagSet": tag_set})
    except ClientError as exc:
        if not is_not_implemented_error(exc):
            raise
        logging.getLogger("s3_archiver.archive").warning(
            "destination does not support object tagging; tags dropped",
            extra={"event": "archive.tagging.unsupported", "bucket": bucket, "key": key},
        )


def object_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    raw_items = cast(list[object], value)
    items: list[Mapping[str, object]] = []
    for raw_item in raw_items:
        if isinstance(raw_item, dict):
            items.append(cast(Mapping[str, object], raw_item))
    return items


def required_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"expected integer-compatible value, got {type(value).__name__}")


def optional_int(value: object, default: int) -> int:
    if value is None:
        return default
    return required_int(value)


def optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def version_id(value: object) -> str | None:
    version_id = optional_string(value)
    if version_id == "null":
        return None
    return version_id


def string_mapping(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict):
        return {}
    raw = cast(Mapping[object, object], value)
    return {str(key): str(item) for key, item in raw.items()}
