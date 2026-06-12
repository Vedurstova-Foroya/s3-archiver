"""Tests for S3 transfer capability derivation."""

from __future__ import annotations

from pathlib import Path

import pytest
import s3_archiver_core.s3 as s3_module
from s3_archiver_core.settings import AppSettings

from tests.unit.settings_fakes import dual_env


@pytest.mark.unit()
def test_transfer_capabilities_for_cross_provider_pair_disable_native_copy(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(dual_env(tmp_path))

    capabilities = s3_module.transfer_capabilities_for_locations(
        settings.routes[0].source,
        settings.routes[0].destination,
    )

    assert capabilities.native_copy is False
    assert capabilities.multipart_copy is False
    assert capabilities.streaming_upload is True
    assert capabilities.temp_file_backed is True


@pytest.mark.unit()
def test_transfer_capabilities_honor_provider_profile_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _same_backend_env(tmp_path)
    env["S3_DESTINATION_ACCESS_KEY"] = env["S3_SOURCE_ACCESS_KEY"]
    env["S3_DESTINATION_SECRET_KEY"] = env["S3_SOURCE_SECRET_KEY"]
    settings = AppSettings.from_env(env)
    monkeypatch.setattr(
        s3_module,
        "_TRANSFER_PROFILES",
        {
            "localstack": s3_module.S3TransferCapabilities(native_copy=False),
            "oci": s3_module.S3TransferCapabilities(),
        },
    )

    capabilities = s3_module.transfer_capabilities_for_locations(
        settings.routes[0].source,
        settings.routes[0].destination,
    )

    assert capabilities.native_copy is False
    assert capabilities.multipart_copy is False
    assert capabilities.streaming_upload is True


@pytest.mark.unit()
def test_transfer_capabilities_disable_native_copy_for_distinct_credentials(
    tmp_path: Path,
) -> None:
    env = _same_backend_env(tmp_path)
    settings = AppSettings.from_env(env)

    capabilities = s3_module.transfer_capabilities_for_locations(
        settings.routes[0].source,
        settings.routes[0].destination,
    )

    assert capabilities.native_copy is False
    assert capabilities.multipart_copy is False
    assert capabilities.streaming_upload is True
    assert capabilities.temp_file_backed is True


def _same_backend_env(tmp_path: Path) -> dict[str, str]:
    env = dual_env(tmp_path)
    env["S3_SOURCE_PROVIDER"] = "localstack"
    env["S3_SOURCE_REGION"] = "us-east-1"
    env["S3_SOURCE_ENDPOINT"] = "http://127.0.0.1:4566"
    env["S3_DESTINATION_PROVIDER"] = "localstack"
    env["S3_DESTINATION_REGION"] = "us-east-1"
    env["S3_DESTINATION_ENDPOINT"] = "http://127.0.0.1:4566"
    return env
