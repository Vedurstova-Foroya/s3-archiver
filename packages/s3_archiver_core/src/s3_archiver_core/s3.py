"""Typed S3 client construction and archive adapter helpers.

``S3Client`` and ``S3ClientFactory`` are PEP 544 ``Protocol`` classes: the
``...`` method bodies are interface stubs, not abstract methods. The real
boto3 client (and the fakes used in tests) satisfy them structurally by
matching the shape — no subclassing required.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, cast

from boto3.session import Session
from botocore.config import Config

from s3_archiver_core.settings import S3LocationSettings


class S3Client(Protocol):
    """Runtime-safe structural type for the S3 methods used by this package."""

    def head_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        """Check bucket access."""
        ...

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Upload one object."""
        ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Return object data."""
        ...

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        """List unversioned objects."""
        ...

    def list_buckets(self) -> Mapping[str, object]:
        """List buckets visible to the configured credentials."""
        ...

    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        """List object versions and delete markers."""
        ...

    def get_bucket_versioning(self, *, Bucket: str) -> Mapping[str, object]:  # noqa: N803
        """Return bucket versioning metadata."""
        ...

    def put_bucket_versioning(self, **kwargs: object) -> Mapping[str, object]:
        """Set bucket versioning metadata."""
        ...

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        """Return object metadata."""
        ...

    def get_object_tagging(self, **kwargs: object) -> Mapping[str, object]:
        """Return object tags."""
        ...

    def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        """Copy one object server-side."""
        ...

    def create_multipart_upload(self, **kwargs: object) -> Mapping[str, object]:
        """Create a multipart upload."""
        ...

    def upload_part_copy(self, **kwargs: object) -> Mapping[str, object]:
        """Copy one multipart part server-side."""
        ...

    def upload_part(self, **kwargs: object) -> Mapping[str, object]:
        """Upload one multipart part."""
        ...

    def complete_multipart_upload(self, **kwargs: object) -> Mapping[str, object]:
        """Complete a multipart upload."""
        ...

    def abort_multipart_upload(self, **kwargs: object) -> Mapping[str, object]:
        """Abort a multipart upload."""
        ...

    def put_object_tagging(self, **kwargs: object) -> Mapping[str, object]:
        """Write object tags."""
        ...


VersioningState = Literal["Disabled", "Enabled", "Suspended"]
TransferStrategy = Literal[
    "simple_native_copy",
    "multipart_native_copy",
    "multipart_streaming",
    "temp_file_backed",
]
S3_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_SIMPLE_COPY_LIMIT_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_STREAMING_LIMIT_BYTES = 50 * 1024 * 1024 * 1024
LOCALSTACK_CONNECT_TIMEOUT_SECONDS = 1
LOCALSTACK_READ_TIMEOUT_SECONDS = 5
LOCALSTACK_MAX_ATTEMPTS = 2


def _empty_checksums() -> dict[str, str]:
    return {}


@dataclass(frozen=True, slots=True)
class S3ObjectProperties:
    """Portable object properties needed for archive verification."""

    size: int
    etag: str | None
    content_type: str | None
    content_encoding: str | None
    content_language: str | None
    content_disposition: str | None
    cache_control: str | None
    expires: datetime | None
    metadata: Mapping[str, str]
    tags: Mapping[str, str]
    last_modified: datetime | None = None
    checksums: Mapping[str, str] = field(default_factory=_empty_checksums)
    checksum_type: str | None = None


@dataclass(frozen=True, slots=True)
class S3ListedObject:
    """Source object discovered by an archive listing operation."""

    key: str
    size: int
    last_modified: datetime
    etag: str | None
    version_id: str | None
    properties: S3ObjectProperties


@dataclass(frozen=True, slots=True)
class S3TransferCapabilities:
    """Transfer capabilities exposed by a source/destination pair."""

    native_copy: bool = True
    multipart_copy: bool = True
    streaming_upload: bool = True
    temp_file_backed: bool = True
    simple_copy_limit_bytes: int = DEFAULT_SIMPLE_COPY_LIMIT_BYTES
    streaming_limit_bytes: int = DEFAULT_STREAMING_LIMIT_BYTES


S3ProviderTransferProfile = S3TransferCapabilities


_TRANSFER_PROFILES: Mapping[str, S3ProviderTransferProfile] = {
    "localstack": S3ProviderTransferProfile(),
    "oci": S3ProviderTransferProfile(),
    "custom": S3ProviderTransferProfile(),
}


class S3ClientFactory(Protocol):
    """Typed callable wrapper around the boto3 client factory boundary."""

    def __call__(
        self, *, service_name: Literal["s3"], endpoint_url: str, config: Config
    ) -> S3Client:
        """Build an S3 client."""
        ...


def build_s3_client(location: S3LocationSettings) -> S3Client:
    """Create a configured S3 client for one S3 location."""

    session = Session(
        aws_access_key_id=location.access_key_id,
        aws_secret_access_key=location.secret_access_key,
        region_name=location.region,
    )
    service_name: Literal["s3"] = "s3"
    client_factory = cast(S3ClientFactory, session.client)
    addressing_style: Literal["path", "virtual"] = (
        "path" if location.addressing_style.value == "path" else "virtual"
    )
    config = _build_client_config(addressing_style, location)
    return client_factory(
        service_name=service_name,
        endpoint_url=location.resolved_endpoint_url(),
        config=config,
    )


def _build_client_config(
    addressing_style: Literal["path", "virtual"],
    location: S3LocationSettings,
) -> Config:
    if location.provider.value == "localstack":
        return Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            connect_timeout=LOCALSTACK_CONNECT_TIMEOUT_SECONDS,
            read_timeout=LOCALSTACK_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": LOCALSTACK_MAX_ATTEMPTS, "mode": "standard"},
        )
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": addressing_style},
    )


def transfer_capabilities_for_locations(
    source: S3LocationSettings,
    destination: S3LocationSettings,
) -> S3TransferCapabilities:
    """Derive pair capabilities from provider profiles plus location compatibility."""

    source_profile = transfer_profile_for_location(source)
    destination_profile = transfer_profile_for_location(destination)
    same_native_backend = _native_copy_backend_matches(source, destination)
    same_native_credentials = _native_copy_credentials_match(source, destination)
    native_copy = (
        same_native_backend
        and same_native_credentials
        and source_profile.native_copy
        and destination_profile.native_copy
    )
    return S3TransferCapabilities(
        native_copy=native_copy,
        multipart_copy=(
            native_copy and source_profile.multipart_copy and destination_profile.multipart_copy
        ),
        streaming_upload=(source_profile.streaming_upload and destination_profile.streaming_upload),
        temp_file_backed=(source_profile.temp_file_backed and destination_profile.temp_file_backed),
        simple_copy_limit_bytes=min(
            source_profile.simple_copy_limit_bytes,
            destination_profile.simple_copy_limit_bytes,
        ),
        streaming_limit_bytes=min(
            source_profile.streaming_limit_bytes,
            destination_profile.streaming_limit_bytes,
        ),
    )


def transfer_profile_for_location(location: S3LocationSettings) -> S3ProviderTransferProfile:
    """Return the transfer profile for one configured provider location."""

    profile = _TRANSFER_PROFILES.get(location.provider.value)
    if profile is not None:
        return profile
    raise ValueError(f"unsupported provider {location.provider.value!r}")


def _native_copy_backend_matches(
    source: S3LocationSettings,
    destination: S3LocationSettings,
) -> bool:
    source_identity = source.storage_identity()
    destination_identity = destination.storage_identity()
    return (
        source_identity.provider == destination_identity.provider
        and source_identity.endpoint_url == destination_identity.endpoint_url
        and source_identity.region == destination_identity.region
        and source_identity.namespace == destination_identity.namespace
    )


def _native_copy_credentials_match(
    source: S3LocationSettings,
    destination: S3LocationSettings,
) -> bool:
    return (
        source.access_key_id == destination.access_key_id
        and source.secret_access_key == destination.secret_access_key
    )


def checksums_from_head_fields(head: Mapping[str, object]) -> Mapping[str, str]:
    """Collect provider checksum fields from a ``HeadObject`` response."""

    return {
        algorithm: str(value)
        for algorithm, value in {
            "crc32": head.get("ChecksumCRC32"),
            "crc32c": head.get("ChecksumCRC32C"),
            "crc64nvme": head.get("ChecksumCRC64NVME"),
            "sha1": head.get("ChecksumSHA1"),
            "sha256": head.get("ChecksumSHA256"),
        }.items()
        if value is not None
    }
