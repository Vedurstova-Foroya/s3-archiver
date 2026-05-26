from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from s3_archiver_core._settings_parse import normalize_endpoint_url as _normalize_endpoint_url
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.parsers.kinds import ParserKind


class S3Provider(StrEnum):
    """Supported S3-compatible storage providers."""

    OCI = "oci"
    LOCALSTACK = "localstack"
    CUSTOM = "custom"


class S3AddressingStyle(StrEnum):
    """Supported S3 addressing styles."""

    PATH = "path"
    VIRTUAL = "virtual"


class CopyMode(StrEnum):
    """Supported route copy modes."""

    DIRECT = "direct"
    DAILY_TAR_GZ = "daily_tar_gz"
    TIMESTAMP_CHILD_TAR_GZ = "timestamp_child_tar_gz"


@dataclass(frozen=True, slots=True)
class StorageLocationIdentity:
    """Normalized physical storage location for source/destination safety checks."""

    provider: S3Provider
    endpoint_url: str
    region: str | None
    namespace: str | None
    bucket: str


@dataclass(frozen=True, slots=True)
class S3LocationSettings:
    """Validated settings for one S3 source or destination location."""

    provider: S3Provider
    access_key_id: str
    secret_access_key: str
    region: str
    bucket: str
    namespace: str | None
    iam_user_ocid: str | None
    endpoint_url: str | None
    addressing_style: S3AddressingStyle
    path: str = ""

    def resolved_endpoint_url(self) -> str:
        """Return the explicit or provider-default endpoint URL."""

        if self.endpoint_url is not None:
            return self.endpoint_url
        if self.provider is S3Provider.LOCALSTACK:
            return "http://localstack:4566"
        if self.provider is S3Provider.CUSTOM:
            raise ConfigError("S3_ENDPOINT is required when provider=custom")
        if self.namespace is None:
            raise ConfigError("S3_NAMESPACE is required for OCI endpoint resolution")
        return f"https://{self.namespace}.compat.objectstorage.{self.region}.oraclecloud.com"

    def storage_identity(self) -> StorageLocationIdentity:
        """Return the normalized physical bucket identity."""

        return StorageLocationIdentity(
            provider=self.provider,
            endpoint_url=_normalize_endpoint_url(self.resolved_endpoint_url()),
            region=self.region if self.provider in {S3Provider.OCI, S3Provider.CUSTOM} else None,
            namespace=self.namespace if self.provider is S3Provider.OCI else None,
            bucket=self.bucket,
        )


@dataclass(frozen=True, slots=True)
class RouteSettings:
    """Validated archive route configuration."""

    name: str
    parser: ParserKind
    copy_mode: CopyMode
    source: S3LocationSettings
    destination: S3LocationSettings
