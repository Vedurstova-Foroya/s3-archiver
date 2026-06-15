"""Tests for minimal route config S3 defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import AppSettings, S3Provider


def _minimal_route(
    *,
    source_bucket: str = "source-bucket",
    destination_bucket: str = "archive-bucket",
) -> dict[str, object]:
    return {
        "name": "daily",
        "parser": "filename_timestamp",
        "copy_mode": "daily_tar_gz",
        "source": {"bucket": source_bucket},
        "destination": {"bucket": destination_bucket},
    }


def _shared_env(tmp_path: Path, routes: list[dict[str, object]]) -> dict[str, str]:
    return {
        "ARCHIVER_CONFIG_JSON": json.dumps(routes),
        "S3_PROVIDER": "localstack",
        "S3_REGION": "us-east-1",
        "S3_ACCESS_KEY": "shared-access",
        "S3_SECRET_KEY": "shared-secret",
        "LOG_DIR": str(tmp_path / "logs"),
    }


@pytest.mark.unit()
def test_from_env_decodes_minimal_route_with_shared_s3_defaults(tmp_path: Path) -> None:
    settings = AppSettings.from_env(_shared_env(tmp_path, [_minimal_route()]))

    assert settings.routes[0].source.provider is S3Provider.LOCALSTACK
    assert settings.routes[0].source.region == "us-east-1"
    assert settings.routes[0].source.bucket == "source-bucket"
    assert settings.routes[0].source.path == ""
    assert settings.routes[0].source.access_key_id == "shared-access"
    assert settings.routes[0].destination.bucket == "archive-bucket"
    assert settings.routes[0].destination.path == ""
    assert settings.routes[0].destination.secret_access_key == "shared-secret"


@pytest.mark.unit()
def test_from_env_prefers_side_specific_defaults_over_shared_defaults(tmp_path: Path) -> None:
    env = _shared_env(tmp_path, [_minimal_route()])
    env["S3_SOURCE_ACCESS_KEY"] = "source-access"
    env["S3_DESTINATION_SECRET_KEY"] = "destination-secret"
    env["S3_DESTINATION_REGION"] = "eu-frankfurt-1"

    settings = AppSettings.from_env(env)

    assert settings.routes[0].source.access_key_id == "source-access"
    assert settings.routes[0].source.secret_access_key == "shared-secret"
    assert settings.routes[0].destination.secret_access_key == "destination-secret"
    assert settings.routes[0].destination.region == "eu-frankfurt-1"


@pytest.mark.unit()
def test_from_env_prefers_explicit_route_location_over_env_defaults(tmp_path: Path) -> None:
    route = _minimal_route()
    source = cast(dict[str, object], route["source"])
    source["region"] = "explicit-region"
    source["access_key_id"] = "explicit-access"
    source["endpoint_url"] = "http://localstack-alt:4566"
    env = _shared_env(tmp_path, [route])
    env["S3_SOURCE_REGION"] = "env-region"
    env["S3_SOURCE_ACCESS_KEY"] = "env-access"
    env["S3_SOURCE_ENDPOINT"] = "http://localstack:4566"

    settings = AppSettings.from_env(env)

    assert settings.routes[0].source.region == "explicit-region"
    assert settings.routes[0].source.access_key_id == "explicit-access"
    assert settings.routes[0].source.endpoint_url == "http://localstack-alt:4566"


@pytest.mark.unit()
def test_from_env_rejects_minimal_route_missing_required_fallback(tmp_path: Path) -> None:
    env = _shared_env(tmp_path, [_minimal_route()])
    _ = env.pop("S3_ACCESS_KEY")

    with pytest.raises(ConfigError, match="access_key_id"):
        _ = AppSettings.from_env(env)


@pytest.mark.unit()
def test_from_env_rejects_oci_defaults_without_required_oci_fields(tmp_path: Path) -> None:
    env = _shared_env(tmp_path, [_minimal_route()])
    env["S3_PROVIDER"] = "oci"
    env["S3_NAMESPACE"] = "tenant"
    _ = env.pop("S3_IAM_USER_OCID", None)

    with pytest.raises(ConfigError, match="iam_user_ocid"):
        _ = AppSettings.from_env(env)


@pytest.mark.unit()
def test_from_env_rejects_same_storage_after_shared_defaults(tmp_path: Path) -> None:
    route = _minimal_route(source_bucket="same-bucket", destination_bucket="same-bucket")

    with pytest.raises(ConfigError, match="source and destination storage locations"):
        _ = AppSettings.from_env(_shared_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_destination_path_defaults_to_source_path(tmp_path: Path) -> None:
    route = _minimal_route()
    source = cast(dict[str, object], route["source"])
    source["path"] = "data/fae/"

    settings = AppSettings.from_env(_shared_env(tmp_path, [route]))

    assert settings.routes[0].source.path == "data/fae/"
    assert settings.routes[0].destination.path == "data/fae/"


@pytest.mark.unit()
def test_from_env_empty_destination_object_is_minimal_archive(tmp_path: Path) -> None:
    route = _minimal_route()
    route["source"] = {"path": "data/fae/"}
    route["destination"] = {}
    env = _shared_env(tmp_path, [route])
    env["S3_SOURCE_BUCKET"] = "source-bucket"
    env["S3_DESTINATION_BUCKET"] = "archive-bucket"

    settings = AppSettings.from_env(env)

    assert settings.routes[0].destination.bucket == "archive-bucket"
    assert settings.routes[0].destination.path == "data/fae/"


@pytest.mark.unit()
def test_from_env_explicit_destination_path_overrides_source_path(tmp_path: Path) -> None:
    route = _minimal_route()
    source = cast(dict[str, object], route["source"])
    source["path"] = "data/fae/"
    destination = cast(dict[str, object], route["destination"])
    destination["path"] = "archives/fae/"

    settings = AppSettings.from_env(_shared_env(tmp_path, [route]))

    assert settings.routes[0].destination.path == "archives/fae/"


@pytest.mark.unit()
def test_from_env_explicit_empty_destination_path_stays_root(tmp_path: Path) -> None:
    route = _minimal_route()
    source = cast(dict[str, object], route["source"])
    source["path"] = "data/fae/"
    destination = cast(dict[str, object], route["destination"])
    destination["path"] = ""

    settings = AppSettings.from_env(_shared_env(tmp_path, [route]))

    assert settings.routes[0].destination.path == ""


@pytest.mark.unit()
def test_from_env_destination_path_env_overrides_source_path(tmp_path: Path) -> None:
    route = _minimal_route()
    source = cast(dict[str, object], route["source"])
    source["path"] = "data/fae/"
    env = _shared_env(tmp_path, [route])
    env["S3_DESTINATION_PATH"] = "archives/fae/"

    settings = AppSettings.from_env(env)

    assert settings.routes[0].destination.path == "archives/fae/"
