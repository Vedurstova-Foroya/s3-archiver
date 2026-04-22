"""Tests for settings loading and validation."""

from __future__ import annotations

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import AppSettings, S3Provider


@pytest.mark.unit()
def test_from_env_builds_oci_settings(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)

    assert settings.provider is S3Provider.OCI
    assert settings.resolved_endpoint_url() == (
        "https://tenant.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"
    )


@pytest.mark.unit()
def test_from_env_rejects_invalid_log_level(base_env: dict[str, str]) -> None:
    base_env["LOG_LEVEL"] = "TRACE"

    with pytest.raises(ConfigError):
        _ = AppSettings.from_env(base_env)


@pytest.mark.unit()
def test_localstack_defaults_without_namespace(base_env: dict[str, str]) -> None:
    base_env["S3_PROVIDER"] = "localstack"
    _ = base_env.pop("S3_NAMESPACE")
    _ = base_env.pop("OCI_IAM_USER_OCID")

    settings = AppSettings.from_env(base_env)

    assert settings.provider is S3Provider.LOCALSTACK
    assert settings.resolved_endpoint_url() == "http://localstack:4566"


@pytest.mark.unit()
def test_from_env_rejects_invalid_addressing_style(base_env: dict[str, str]) -> None:
    base_env["S3_ADDRESSING_STYLE"] = "broken"

    with pytest.raises(ConfigError, match="S3_ADDRESSING_STYLE"):
        _ = AppSettings.from_env(base_env)


@pytest.mark.unit()
def test_from_env_requires_namespace_for_oci(base_env: dict[str, str]) -> None:
    _ = base_env.pop("S3_NAMESPACE")

    with pytest.raises(ConfigError, match="S3_NAMESPACE"):
        _ = AppSettings.from_env(base_env)


@pytest.mark.unit()
def test_from_env_requires_iam_user_for_oci(base_env: dict[str, str]) -> None:
    _ = base_env.pop("OCI_IAM_USER_OCID")

    with pytest.raises(ConfigError, match="OCI_IAM_USER_OCID"):
        _ = AppSettings.from_env(base_env)


@pytest.mark.unit()
def test_from_env_requires_non_empty_required_values(base_env: dict[str, str]) -> None:
    base_env["S3_ACCESS_KEY_ID"] = " "

    with pytest.raises(ConfigError, match="S3_ACCESS_KEY_ID is required"):
        _ = AppSettings.from_env(base_env)


@pytest.mark.unit()
def test_resolved_endpoint_url_requires_namespace_when_missing(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)
    settings_without_namespace = AppSettings(
        provider=settings.provider,
        access_key_id=settings.access_key_id,
        secret_access_key=settings.secret_access_key,
        region=settings.region,
        bucket=settings.bucket,
        namespace=None,
        iam_user_ocid=settings.iam_user_ocid,
        endpoint_url=None,
        addressing_style=settings.addressing_style,
        log_level=settings.log_level,
        log_dir=settings.log_dir,
    )

    with pytest.raises(ConfigError, match="OCI endpoints require S3_NAMESPACE"):
        _ = settings_without_namespace.resolved_endpoint_url()
