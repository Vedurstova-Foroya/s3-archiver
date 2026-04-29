"""Configuration loading and validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from s3_archiver_core.errors import ConfigError


class S3Provider(StrEnum):
    """Supported S3 backends."""

    OCI = "oci"
    LOCALSTACK = "localstack"


class S3AddressingStyle(StrEnum):
    """Supported S3 addressing styles."""

    PATH = "path"
    VIRTUAL = "virtual"


_VALID_PROVIDERS = frozenset({provider.value for provider in S3Provider})
_VALID_ADDRESSING_STYLES = frozenset({style.value for style in S3AddressingStyle})
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Runtime configuration loaded from environment variables."""

    provider: S3Provider
    access_key_id: str
    secret_access_key: str
    region: str
    bucket: str
    namespace: str | None
    iam_user_ocid: str | None
    endpoint_url: str | None
    addressing_style: S3AddressingStyle
    log_level: str
    log_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AppSettings:
        """Build validated settings from environment variables."""

        provider_value = _require(env, "S3_PROVIDER").lower()
        addressing_style_value = env.get("S3_ADDRESSING_STYLE", "path").lower()
        log_level = env.get("LOG_LEVEL", "INFO").upper()
        namespace = _optional(env, "S3_NAMESPACE")
        iam_user_ocid = _optional(env, "OCI_IAM_USER_OCID")

        if provider_value not in _VALID_PROVIDERS:
            raise ConfigError(
                f"S3_PROVIDER must be one of {_VALID_PROVIDERS}, got {provider_value!r}"
            )
        if addressing_style_value not in _VALID_ADDRESSING_STYLES:
            message = "S3_ADDRESSING_STYLE must be one of " + (
                f"{_VALID_ADDRESSING_STYLES}, got {addressing_style_value!r}"
            )
            raise ConfigError(message)
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {_VALID_LOG_LEVELS}, got {log_level!r}")
        provider = S3Provider(provider_value)
        if provider is S3Provider.OCI and namespace is None:
            raise ConfigError("S3_NAMESPACE is required when S3_PROVIDER=oci")
        if provider is S3Provider.OCI and iam_user_ocid is None:
            raise ConfigError("OCI_IAM_USER_OCID is required when S3_PROVIDER=oci")

        return cls(
            provider=provider,
            access_key_id=_require(env, "S3_ACCESS_KEY_ID"),
            secret_access_key=_require(env, "S3_SECRET_ACCESS_KEY"),
            region=_require(env, "S3_REGION"),
            bucket=_require(env, "S3_BUCKET"),
            namespace=namespace,
            iam_user_ocid=iam_user_ocid,
            endpoint_url=_optional(env, "S3_ENDPOINT_URL"),
            addressing_style=S3AddressingStyle(addressing_style_value),
            log_level=log_level,
            log_dir=Path(env.get("LOG_DIR", "/var/log/s3-archiver")),
        )

    def resolved_endpoint_url(self) -> str:
        """Return the effective endpoint URL for the configured provider."""

        if self.endpoint_url is not None:
            return self.endpoint_url
        if self.provider is S3Provider.LOCALSTACK:
            return "http://localstack:4566"
        namespace = self.namespace
        if namespace is None:
            raise ConfigError("OCI endpoints require S3_NAMESPACE to be set")
        return f"https://{namespace}.compat.objectstorage.{self.region}.oraclecloud.com"


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if value is None or value.strip() == "":
        raise ConfigError(f"{key} is required")
    return value


def _optional(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None or value.strip() == "":
        return None
    return value
