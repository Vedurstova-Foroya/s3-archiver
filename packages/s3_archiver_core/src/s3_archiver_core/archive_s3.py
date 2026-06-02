from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from botocore.exceptions import ClientError

from s3_archiver_core._archive_s3_helpers import (
    ReadableBody,
    close_body,
    is_not_found_error,
    is_not_implemented_error,
    object_list,
    optional_string,
    parse_versioning_state,
    properties_from_head,
    required_int,
    supports_checksum_mode,
    version_id,
    versioned_kwargs,
)
from s3_archiver_core.archive_transfer import TransferStrategy
from s3_archiver_core.s3 import (
    S3_CHUNK_BYTES,
    S3Client,
    S3ListedObject,
    S3ObjectProperties,
    VersioningState,
)
from s3_archiver_core.s3_transfer import copy_s3_object, upload_s3_file
from s3_archiver_core.temp_files import default_temp_dir


@dataclass(frozen=True, slots=True)
class S3ArchiveBucket:
    """Archive bucket adapter backed by an S3-compatible client."""

    client: S3Client
    bucket: str
    temp_dir: Path = field(default_factory=default_temp_dir)

    def versioning_state(self) -> VersioningState:
        """Return the bucket versioning state.

        OCI Object Storage and similar providers may reject GetBucketVersioning
        with NotImplemented; treat that as Disabled so listing falls back to
        list_objects_v2 instead of list_object_versions.
        """
        try:
            response = self.client.get_bucket_versioning(Bucket=self.bucket)
        except ClientError as exc:
            if is_not_implemented_error(exc):
                return "Disabled"
            raise
        return parse_versioning_state(response.get("Status"))

    def list_source_objects(
        self, versioning_state: VersioningState, *, prefix: str = ""
    ) -> Iterator[S3ListedObject]:
        """List current source objects for the supplied versioning mode."""
        normalized_prefix = _normalize_list_prefix(prefix)
        if versioning_state == "Disabled":
            yield from self._list_unversioned(normalized_prefix)
            return
        yield from self._list_versioned(normalized_prefix)

    def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        """Return S3 object properties, or None when the object is missing."""
        kwargs = versioned_kwargs(self.bucket, key, version_id)
        try:
            head = self.client.head_object(**(kwargs | {"ChecksumMode": "ENABLED"}))
        except ClientError as exc:
            if is_not_found_error(exc):
                return None
            if not supports_checksum_mode(exc):
                raise
            try:
                head = self.client.head_object(**kwargs)
            except ClientError as retry_exc:
                if is_not_found_error(retry_exc):
                    return None
                raise
        tags = self.get_tags(key, version_id)
        return properties_from_head(head, tags)

    def content_sha256(self, key: str, version_id: str | None = None) -> str | None:
        """Return the SHA-256 digest of object content when present."""
        try:
            body = self.read_source_stream(key, version_id)
        except FileNotFoundError:
            return None
        digest = hashlib.sha256()
        try:
            while chunk := body.read(S3_CHUNK_BYTES):
                digest.update(chunk)
        finally:
            close_body(body)
        return digest.hexdigest()

    def read_source_bytes(self, key: str, version_id: str | None = None) -> bytes:
        """Return the complete source object payload."""
        body = self.read_source_stream(key, version_id)
        chunks: list[bytes] = []
        try:
            while chunk := body.read(S3_CHUNK_BYTES):
                chunks.append(chunk)
        finally:
            close_body(body)
        return b"".join(chunks)

    def read_source_stream(self, key: str, version_id: str | None = None) -> ReadableBody:
        """Return a streaming source object body."""
        try:
            response = self.client.get_object(**versioned_kwargs(self.bucket, key, version_id))
        except ClientError as exc:
            if is_not_found_error(exc):
                raise FileNotFoundError(key) from exc
            raise
        return cast(ReadableBody, response["Body"])

    def upload_archive_file(
        self, destination_key: str, archive_path: Path, metadata: Mapping[str, str]
    ) -> None:
        """Upload an archive file with deterministic metadata."""
        upload_s3_file(
            self.client,
            self.bucket,
            destination_key,
            archive_path,
            metadata,
            content_type="application/gzip",
        )

    def get_tags(self, key: str, version_id: str | None = None) -> Mapping[str, str]:
        """Return object tags as a string mapping.

        OCI Object Storage does not implement S3 object tagging; treat
        NotImplemented as an empty tag set so archiving can proceed.
        """
        try:
            response = self.client.get_object_tagging(
                **versioned_kwargs(self.bucket, key, version_id)
            )
        except ClientError as exc:
            if is_not_implemented_error(exc):
                return {}
            raise
        tag_set = response.get("TagSet", [])
        if not isinstance(tag_set, list):
            return {}
        raw_tags = cast(list[object], tag_set)
        tags: dict[str, str] = {}
        for raw_tag in raw_tags:
            if isinstance(raw_tag, dict):
                tag = cast(Mapping[object, object], raw_tag)
                tag_key = tag.get("Key")
                tag_value = tag.get("Value")
                if tag_key is not None and tag_value is not None:
                    tags[str(tag_key)] = str(tag_value)
        return tags

    def copy_from(
        self,
        source: object,
        source_bucket: str,
        source_key: str,
        source_version_id: str | None,
        properties: S3ObjectProperties,
        destination_key: str,
        destination_metadata: Mapping[str, str],
        strategy: TransferStrategy,
    ) -> None:
        """Copy one source object into this bucket."""
        if not isinstance(source, S3ArchiveBucket):
            raise TypeError("S3ArchiveBucket copy requires an S3ArchiveBucket source")
        copy_s3_object(
            self.client,
            source.client,
            source_bucket,
            source_key,
            source_version_id,
            properties,
            self.bucket,
            destination_key,
            destination_metadata,
            strategy,
            self.temp_dir,
        )

    def _list_unversioned(self, prefix: str) -> Iterator[S3ListedObject]:
        start_after: str | None = None
        while True:
            kwargs: dict[str, object] = {"Bucket": self.bucket, "MaxKeys": 1000}
            if prefix:
                kwargs["Prefix"] = prefix
            if start_after is not None:
                kwargs["StartAfter"] = start_after
            page = self.client.list_objects_v2(**kwargs)
            last_key: str | None = None
            for item in object_list(page.get("Contents")):
                last_key = str(item["Key"])
                yield self._listed_from_item(item, None)
            if page.get("IsTruncated") is not True:
                return
            if last_key is None:
                raise RuntimeError("S3 list_objects_v2 returned a truncated empty page")
            start_after = last_key

    def _list_versioned(self, prefix: str) -> Iterator[S3ListedObject]:
        key_marker: str | None = None
        version_marker: str | None = None
        while True:
            kwargs: dict[str, object] = {"Bucket": self.bucket, "MaxKeys": 1000}
            if prefix:
                kwargs["Prefix"] = prefix
            if key_marker is not None:
                kwargs["KeyMarker"] = key_marker
            if version_marker is not None:
                kwargs["VersionIdMarker"] = version_marker
            page = self.client.list_object_versions(**kwargs)
            for item in object_list(page.get("Versions")):
                if item.get("IsLatest") is True:
                    yield self._listed_from_item(item, version_id(item.get("VersionId")))
            if page.get("IsTruncated") is not True:
                return
            key_marker = optional_string(page.get("NextKeyMarker"))
            version_marker = optional_string(page.get("NextVersionIdMarker"))

    def _listed_from_item(
        self, item: Mapping[str, object], version_id: str | None
    ) -> S3ListedObject:
        key = str(item["Key"])
        size = required_int(item["Size"])
        last_modified = cast(datetime, item["LastModified"])
        etag = optional_string(item.get("ETag"))
        properties = _listed_properties(item, size, etag, last_modified)
        return S3ListedObject(key, size, last_modified, etag, version_id, properties)


def _normalize_list_prefix(prefix: str) -> str:
    stripped = prefix.strip("/")
    return "" if stripped == "" else f"{stripped}/"


def _listed_properties(
    item: Mapping[str, object],
    size: int,
    etag: str | None,
    last_modified: datetime,
) -> S3ObjectProperties:
    return S3ObjectProperties(
        size=size,
        etag=etag,
        content_type=optional_string(item.get("ContentType")),
        content_encoding=optional_string(item.get("ContentEncoding")),
        content_language=optional_string(item.get("ContentLanguage")),
        content_disposition=optional_string(item.get("ContentDisposition")),
        cache_control=optional_string(item.get("CacheControl")),
        expires=cast(datetime | None, item.get("Expires")),
        metadata={},
        tags={},
        last_modified=last_modified,
    )
