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
