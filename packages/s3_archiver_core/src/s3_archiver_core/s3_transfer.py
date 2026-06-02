"""S3 object transfer helpers."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from s3_archiver_core._archive_s3_helpers import (
    ReadableBody,
    close_body,
    copy_source_kwargs,
    put_object_tags,
    versioned_kwargs,
)
from s3_archiver_core.s3 import S3_CHUNK_BYTES, S3Client, S3ObjectProperties, TransferStrategy
from s3_archiver_core.temp_files import TRANSFER_TEMP_PREFIX, ensure_temp_storage_available

S3_MAX_MULTIPART_PARTS = 10_000


def copy_s3_object(
    destination_client: S3Client,
    source_client: S3Client,
    source_bucket: str,
    source_key: str,
    source_version_id: str | None,
    properties: S3ObjectProperties,
    destination_bucket: str,
    destination_key: str,
    metadata: Mapping[str, str],
    strategy: TransferStrategy,
    temp_dir: Path,
) -> None:
    """Copy an S3 object with the requested strategy."""

    source = copy_source_kwargs(source_bucket, source_key, source_version_id)
    if strategy == "simple_native_copy" or (
        strategy == "multipart_native_copy" and properties.size == 0
    ):
        _ = destination_client.copy_object(
            **_copy_kwargs(destination_bucket, destination_key, source, properties, metadata)
        )
        return
    if strategy == "multipart_native_copy":
        _multipart_copy(
            destination_client,
            destination_bucket,
            destination_key,
            source,
            properties,
            metadata,
        )
        return
    if strategy == "temp_file_backed":
        _ = ensure_temp_storage_available(
            temp_dir,
            required_bytes=properties.size,
            source_key=source_key,
            destination_key=destination_key,
            operation="temp_file_backed_transfer",
        )
    body: ReadableBody | None = None
    try:
        body = _source_body(source_client, source_bucket, source_key, source_version_id)
        if strategy == "multipart_streaming":
            _upload_stream(
                destination_client, destination_bucket, destination_key, properties, metadata, body
            )
            return
        path = _stage(body, temp_dir)
        try:
            with path.open("rb") as file:
                _upload_stream(
                    destination_client,
                    destination_bucket,
                    destination_key,
                    properties,
                    metadata,
                    cast(ReadableBody, cast(object, file)),
                )
        finally:
            path.unlink(missing_ok=True)
    finally:
        if body is not None:
            close_body(body)


def upload_s3_file(
    client: S3Client,
    bucket: str,
    key: str,
    path: Path,
    metadata: Mapping[str, str],
    *,
    content_type: str,
) -> None:
    """Upload a local file using S3 multipart upload for non-empty payloads."""

    kwargs: dict[str, object] = {
        "Bucket": bucket,
        "Key": key,
        "Metadata": dict(metadata),
        "ContentType": content_type,
    }
    size = path.stat().st_size
    if size == 0:
        _ = client.put_object(**kwargs, Body=b"")
        return
    upload_id = _upload_id(client.create_multipart_upload(**kwargs))
    try:
        parts: list[dict[str, object]] = []
        number = 1
        with path.open("rb") as file:
            while True:
                chunk = file.read(_multipart_chunk_size(size))
                if chunk == b"":
                    break
                response = client.upload_part(
                    Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=chunk
                )
                parts.append(_part(number, response))
                number += 1
        _complete(client, bucket, key, upload_id, parts)
    except Exception:
        _safe_abort_multipart(client, bucket, key, upload_id)
        raise


def _multipart_copy(
    client: S3Client,
    bucket: str,
    key: str,
    source: Mapping[str, str],
    properties: S3ObjectProperties,
    metadata: Mapping[str, str],
) -> None:
    upload_id = _upload_id(
        client.create_multipart_upload(**_object_kwargs(bucket, key, properties, metadata))
    )
    try:
        parts: list[dict[str, object]] = []
        for number, start in enumerate(range(0, properties.size, S3_CHUNK_BYTES), 1):
            end = min(start + S3_CHUNK_BYTES, properties.size) - 1
            response = client.upload_part_copy(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                CopySource=source,
                CopySourceRange=f"bytes={start}-{end}",
            )
            parts.append(_part(number, response))
        _complete(client, bucket, key, upload_id, parts)
    except Exception:
        _safe_abort_multipart(client, bucket, key, upload_id)
        raise
    put_object_tags(client, bucket, key, properties.tags)


def _upload_stream(
    client: S3Client,
    bucket: str,
    key: str,
    properties: S3ObjectProperties,
    metadata: Mapping[str, str],
    body: ReadableBody,
) -> None:
    if properties.size == 0:
        _ = client.put_object(**_object_kwargs(bucket, key, properties, metadata), Body=b"")
        put_object_tags(client, bucket, key, properties.tags)
        return
    upload_id = _upload_id(
        client.create_multipart_upload(**_object_kwargs(bucket, key, properties, metadata))
    )
    try:
        parts: list[dict[str, object]] = []
        number = 1
        while True:
            chunk = body.read(S3_CHUNK_BYTES)
            if chunk == b"":
                break
            response = client.upload_part(
                Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=chunk
            )
            parts.append(_part(number, response))
            number += 1
        _complete(client, bucket, key, upload_id, parts)
    except Exception:
        _safe_abort_multipart(client, bucket, key, upload_id)
        raise
    put_object_tags(client, bucket, key, properties.tags)


def _safe_abort_multipart(client: S3Client, bucket: str, key: str, upload_id: str) -> None:
    try:
        _ = client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    except Exception:
        logging.getLogger("s3_archiver.archive").warning(
            "multipart abort failed",
            extra={
                "event": "archive.multipart.abort_failed",
                "bucket": bucket,
                "key": key,
                "upload_id": upload_id,
            },
            exc_info=True,
        )


def _stage(body: ReadableBody, temp_dir: Path) -> Path:
    path: Path | None = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=temp_dir, prefix=TRANSFER_TEMP_PREFIX
        ) as file:
            path = Path(file.name)
            while True:
                chunk = body.read(S3_CHUNK_BYTES)
                if chunk == b"":
                    return path
                _ = file.write(chunk)
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def _copy_kwargs(
    bucket: str,
    key: str,
    source: Mapping[str, str],
    properties: S3ObjectProperties,
    metadata: Mapping[str, str],
) -> dict[str, object]:
    kwargs = _object_kwargs(bucket, key, properties, metadata)
    kwargs |= {"CopySource": source, "MetadataDirective": "REPLACE", "TaggingDirective": "COPY"}
    return kwargs


def _object_kwargs(
    bucket: str, key: str, properties: S3ObjectProperties, metadata: Mapping[str, str]
) -> dict[str, object]:
    kwargs: dict[str, object] = {"Bucket": bucket, "Key": key, "Metadata": dict(metadata)}
    for target, value in (
        ("ContentType", properties.content_type),
        ("ContentEncoding", properties.content_encoding),
        ("ContentLanguage", properties.content_language),
        ("ContentDisposition", properties.content_disposition),
        ("CacheControl", properties.cache_control),
        ("Expires", properties.expires),
    ):
        if value is not None:
            kwargs[target] = value
    return kwargs


def _source_body(client: S3Client, bucket: str, key: str, version_id: str | None) -> ReadableBody:
    body = client.get_object(**versioned_kwargs(bucket, key, version_id)).get("Body")
    if not callable(getattr(body, "read", None)):
        raise TypeError("S3 get_object response Body is not readable")
    return cast(ReadableBody, body)


def _multipart_chunk_size(size: int) -> int:
    return max(S3_CHUNK_BYTES, (size + S3_MAX_MULTIPART_PARTS - 1) // S3_MAX_MULTIPART_PARTS)


def _complete(
    client: S3Client, bucket: str, key: str, upload_id: str, parts: list[dict[str, object]]
) -> None:
    _ = client.complete_multipart_upload(
        Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
    )


def _upload_id(response: Mapping[str, object]) -> str:
    upload_id = response.get("UploadId")
    if upload_id is None:
        raise RuntimeError("S3 multipart upload response omitted UploadId")
    return str(upload_id)


def _part(part_number: int, response: Mapping[str, object]) -> dict[str, object]:
    result = response.get("CopyPartResult")
    source = cast(Mapping[str, object], result) if isinstance(result, dict) else response
    etag = source.get("ETag")
    if etag is None:
        raise RuntimeError("S3 multipart part response omitted ETag")
    return {"ETag": str(etag), "PartNumber": part_number}
