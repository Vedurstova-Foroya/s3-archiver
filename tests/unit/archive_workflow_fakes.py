"""Shared fakes for archive workflow unit tests."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path

from s3_archiver_core.archive_transfer import TransferStrategy
from s3_archiver_core.s3 import S3ListedObject, S3ObjectProperties, VersioningState
from s3_archiver_core.temp_files import default_temp_dir


class FakeReadableBody:
    """Readable byte stream for archive tests."""

    _body: io.BytesIO

    def __init__(self, payload: bytes) -> None:
        self._body = io.BytesIO(payload)

    def read(self, amt: int = -1) -> bytes:
        return self._body.read(amt)

    def close(self) -> None:
        self._body.close()


def object_properties(
    *,
    size: int = 10,
    metadata: Mapping[str, str] | None = None,
    tags: Mapping[str, str] | None = None,
    last_modified: datetime | None = None,
    checksums: Mapping[str, str] | None = None,
    checksum_type: str | None = None,
) -> S3ObjectProperties:
    """Build portable S3 object properties for tests."""

    return S3ObjectProperties(
        size=size,
        etag='"etag"',
        content_type="text/plain",
        content_encoding="gzip",
        content_language="en",
        content_disposition="inline",
        cache_control="max-age=60",
        expires=datetime(2024, 1, 1, tzinfo=UTC),
        metadata=metadata or {"owner": "archive"},
        tags=tags or {"kind": "source"},
        last_modified=last_modified,
        checksums=checksums or {},
        checksum_type=checksum_type,
    )


def listed_object(key: str, age_days: int, version_id: str | None = "v1") -> S3ListedObject:
    """Build a listed source object relative to the fixed archive clock."""

    size = 10
    last_modified = datetime(2024, 4, 20, tzinfo=UTC) - timedelta(days=age_days)
    return S3ListedObject(
        key=key,
        size=size,
        last_modified=last_modified,
        etag='"etag"',
        version_id=version_id,
        properties=object_properties(size=size, last_modified=last_modified),
    )


class FakeBucket:
    """In-memory archive bucket test double."""

    bucket: str
    temp_dir: Path
    copied: list[str]
    uploaded: list[str]
    deleted: list[tuple[str, str | None]]
    fail_copy: bool
    _objects: dict[str, S3ListedObject]
    _versions: dict[tuple[str, str | None], S3ListedObject]
    _destination: dict[str, S3ObjectProperties]
    _payloads: dict[str, bytes]
    _version_payloads: dict[tuple[str, str | None], bytes]
    _destination_payloads: dict[str, bytes]
    _versioning_state: VersioningState
    content_sha256_calls: list[tuple[str, str | None]]

    def __init__(
        self,
        bucket: str,
        objects: Iterable[S3ListedObject] = (),
        versions: Iterable[S3ListedObject] = (),
        destination: Mapping[str, S3ObjectProperties] | None = None,
        payloads: Mapping[str, bytes] | None = None,
        version_payloads: Mapping[tuple[str, str | None], bytes] | None = None,
        versioning_state: VersioningState = "Enabled",
        temp_dir: Path | None = None,
    ) -> None:
        self.bucket = bucket
        self.temp_dir = temp_dir or default_temp_dir()
        self.copied = []
        self.uploaded = []
        self.deleted = []
        self.fail_copy = False
        self._objects = {item.key: item for item in objects}
        self._versions = {(item.key, item.version_id): item for item in chain(objects, versions)}
        self._destination = dict(destination or {})
        self._payloads = {
            key: (payloads or {}).get(key, f"payload:{key}".encode()) for key in self._objects
        }
        self._version_payloads = {
            (key, version_id): (version_payloads or {}).get(
                (key, version_id),
                self._payloads.get(key, f"payload:{key}".encode()),
            )
            for key, version_id in self._versions
        }
        self._destination_payloads = {
            key: (payloads or {}).get(key, f"payload:{key}".encode()) for key in self._destination
        }
        self._versioning_state = versioning_state
        self.content_sha256_calls = []

    def versioning_state(self) -> VersioningState:
        return self._versioning_state

    def list_source_objects(self, versioning_state: VersioningState) -> Iterable[S3ListedObject]:
        assert versioning_state == self._versioning_state
        return tuple(self._objects.values())

    def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        if version_id is not None and (item := self._versions.get((key, version_id))) is not None:
            return item.properties
        return self._destination.get(key)

    def content_sha256(self, key: str, version_id: str | None = None) -> str | None:
        self.content_sha256_calls.append((key, version_id))
        payload = (
            self._version_payloads.get((key, version_id))
            if version_id is not None
            else self._payloads.get(key) or self._destination_payloads.get(key)
        )
        return None if payload is None else hashlib.sha256(payload).hexdigest()

    def read_source_bytes(self, key: str, version_id: str | None = None) -> bytes:
        payload = (
            self._version_payloads.get((key, version_id))
            if version_id is not None
            else self._payloads.get(key)
        )
        if payload is None:
            raise FileNotFoundError(key)
        return payload

    def read_source_stream(self, key: str, version_id: str | None = None) -> FakeReadableBody:
        return FakeReadableBody(self.read_source_bytes(key, version_id))

    def upload_archive_file(
        self, destination_key: str, archive_path: Path, metadata: Mapping[str, str]
    ) -> None:
        if self.fail_copy:
            raise RuntimeError("copy failed")
        payload = archive_path.read_bytes()
        self.uploaded.append(destination_key)
        self._destination[destination_key] = object_properties(
            size=len(payload), metadata=metadata, last_modified=datetime(2024, 4, 20, tzinfo=UTC)
        )
        self._destination_payloads[destination_key] = payload

    def destination_payload(self, key: str) -> bytes:
        payload = self._destination_payloads.get(key)
        if payload is None:
            raise FileNotFoundError(key)
        return payload

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
        assert isinstance(source, FakeBucket)
        assert source.bucket == source_bucket
        _ = (source_version_id, strategy)
        if self.fail_copy:
            raise RuntimeError("copy failed")
        self.copied.append(source_key)
        self._destination[destination_key] = replace(properties, metadata=destination_metadata)
        payload = (
            source._version_payloads.get((source_key, source_version_id))
            if source_version_id is not None
            else source._payloads[source_key]
        )
        assert payload is not None
        self._destination_payloads[destination_key] = payload

    def delete_source(self, key: str, version_id: str | None) -> None:
        self.deleted.append((key, version_id))
