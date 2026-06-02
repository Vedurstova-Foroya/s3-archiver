"""Tests for ARCHIVER_CONFIG_JSON route settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_core.parsers.registry as parser_registry
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.parsers import ParserKind
from s3_archiver_core.settings import AppSettings, CopyMode, S3Provider


def _route(
    *,
    name: str = "fae-daily",
    parser: str = "filename_timestamp",
    copy_mode: str = "daily_tar_gz",
    source_path: str = "data/fae/",
    destination_bucket: str = "archive-bucket",
) -> dict[str, object]:
    return {
        "name": name,
        "parser": parser,
        "copy_mode": copy_mode,
        "source": {
            "provider": "localstack",
            "endpoint_url": "http://localstack:4566/",
            "region": "us-east-1",
            "bucket": "source-bucket",
            "path": source_path,
            "access_key_id": "${S3_SOURCE_ACCESS_KEY}",
            "secret_access_key": "${S3_SOURCE_SECRET_KEY}",
            "addressing_style": "path",
        },
        "destination": {
            "provider": "localstack",
            "endpoint_url": "http://localhost:4566",
            "region": "us-east-1",
            "bucket": destination_bucket,
            "path": "archives/fae/",
            "access_key_id": "destination-access",
            "secret_access_key": "destination-secret",
            "addressing_style": "path",
        },
    }


def _env(tmp_path: Path, routes: list[dict[str, object]]) -> dict[str, str]:
    return {
        "ARCHIVER_CONFIG_JSON": json.dumps(routes),
        "S3_SOURCE_ACCESS_KEY": "source-access",
        "S3_SOURCE_SECRET_KEY": "source-secret",
        "LOG_DIR": str(tmp_path / "logs"),
    }


def _env_with_raw_config(tmp_path: Path, raw_config: str) -> dict[str, str]:
    env = _env(tmp_path, [_route()])
    env["ARCHIVER_CONFIG_JSON"] = raw_config
    return env


@pytest.mark.unit()
def test_from_env_decodes_route_config_json(tmp_path: Path) -> None:
    settings = AppSettings.from_env(_env(tmp_path, [_route()]))

    route = settings.routes[0]
    assert route.name == "fae-daily"
    assert route.parser == ParserKind.FILENAME_TIMESTAMP
    assert route.copy_mode is CopyMode.DAILY_TAR_GZ
    assert route.source.provider is S3Provider.LOCALSTACK
    assert route.source.endpoint_url == "http://localstack:4566"
    assert route.source.path == "data/fae/"
    assert route.source.access_key_id == "source-access"
    assert settings.routes[0].source is route.source
    assert settings.routes[0].destination is route.destination


@pytest.mark.unit()
def test_from_env_expands_route_env_refs_with_defaults(tmp_path: Path) -> None:
    route = _route()
    source = route["source"]
    assert isinstance(source, dict)
    source["provider"] = "${S3_SOURCE_PROVIDER:-localstack}"
    source["bucket"] = "${S3_SOURCE_BUCKET:-source-default}"
    source["path"] = "${S3_SOURCE_PATH:-}"

    settings = AppSettings.from_env(_env(tmp_path, [route]))

    assert settings.routes[0].source.provider is S3Provider.LOCALSTACK
    assert settings.routes[0].source.bucket == "source-default"
    assert settings.routes[0].source.path == ""


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("route_update", "message"),
    [
        ({"parser": "unknown"}, "parser"),
        ({"parser": "DIRECT"}, "parser"),
        ({"copy_mode": "unknown"}, "copy_mode"),
        ({"copy_mode": "DIRECT"}, "copy_mode"),
    ],
)
def test_from_env_rejects_invalid_route_enums(
    tmp_path: Path,
    route_update: dict[str, object],
    message: str,
) -> None:
    route = _route()
    route.update(route_update)

    with pytest.raises(ConfigError, match=message):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_validates_parser_against_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        parser_registry,
        "registered_parser_kinds",
        lambda: frozenset({ParserKind("filename_timestamp")}),
    )

    with pytest.raises(ConfigError, match="parser"):
        _ = AppSettings.from_env(_env(tmp_path, [_route(parser="direct")]))


@pytest.mark.unit()
def test_from_env_rejects_malformed_route_config_json(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="valid JSON"):
        _ = AppSettings.from_env(_env_with_raw_config(tmp_path, "{"))


@pytest.mark.unit()
@pytest.mark.parametrize("raw_config", ["{}", "[]"])
def test_from_env_rejects_non_array_or_empty_route_config_json(
    tmp_path: Path, raw_config: str
) -> None:
    with pytest.raises(ConfigError, match="non-empty JSON array"):
        _ = AppSettings.from_env(_env_with_raw_config(tmp_path, raw_config))


@pytest.mark.unit()
def test_from_env_rejects_non_object_route_config_item(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="JSON object"):
        _ = AppSettings.from_env(_env_with_raw_config(tmp_path, "[3]"))


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("route_update", "message"),
    [
        ({"name": ""}, "name"),
        ({"parser": ""}, "parser"),
        ({"copy_mode": ""}, "copy_mode"),
        ({"source": None}, "source"),
        ({"destination": None}, "destination"),
    ],
)
def test_from_env_rejects_missing_route_fields(
    tmp_path: Path,
    route_update: dict[str, object],
    message: str,
) -> None:
    route = _route()
    route.update(route_update)

    with pytest.raises(ConfigError, match=message):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
@pytest.mark.parametrize(
    ("source_update", "message"),
    [
        ({"provider": 3}, "provider"),
        ({"provider": "aws"}, "provider"),
        ({"addressing_style": 3}, "addressing_style"),
        ({"addressing_style": "invalid"}, "addressing_style"),
        ({"endpoint_url": 4566}, "endpoint_url"),
        ({"access_key_id": 3}, "access_key_id"),
        ({"secret_access_key": ""}, "secret_access_key"),
        ({"region": ""}, "region"),
        ({"bucket": ""}, "bucket"),
        ({"path": 3}, "path"),
    ],
)
def test_from_env_rejects_invalid_route_source_fields(
    tmp_path: Path,
    source_update: dict[str, object],
    message: str,
) -> None:
    route = _route()
    source = cast(dict[str, object], route["source"])
    source.update(source_update)

    with pytest.raises(ConfigError, match=message):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_uses_default_provider_when_route_source_omits_provider(tmp_path: Path) -> None:
    route = _route()
    source = cast(dict[str, object], route["source"])
    _ = source.pop("provider")

    settings = AppSettings.from_env(_env(tmp_path, [route]))

    assert settings.routes[0].source.provider is S3Provider.CUSTOM


def test_from_env_rejects_localstack_endpoint_outside_allowlist(tmp_path: Path) -> None:
    route = _route()
    source = cast(dict[str, object], route["source"])
    source["endpoint_url"] = "http://example.com"

    with pytest.raises(ConfigError, match="not allowed"):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_rejects_oci_source_without_namespace(tmp_path: Path) -> None:
    route = _route()
    source = route["source"]
    assert isinstance(source, dict)
    source["provider"] = "oci"
    source["endpoint_url"] = None
    source["iam_user_ocid"] = "ocid1.user.oc1..example"

    with pytest.raises(ConfigError, match="namespace"):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_rejects_oci_source_without_iam_user_ocid(tmp_path: Path) -> None:
    route = _route()
    source = route["source"]
    assert isinstance(source, dict)
    source["provider"] = "oci"
    source["endpoint_url"] = None
    source["namespace"] = "namespace"

    with pytest.raises(ConfigError, match="iam_user_ocid"):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_rejects_duplicate_route_names(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="duplicate route name"):
        _ = AppSettings.from_env(_env(tmp_path, [_route(), _route()]))


@pytest.mark.unit()
def test_from_env_rejects_same_route_source_and_destination_storage(tmp_path: Path) -> None:
    route = _route(destination_bucket="source-bucket")
    destination = route["destination"]
    assert isinstance(destination, dict)
    destination["endpoint_url"] = "http://localstack:4566"

    with pytest.raises(ConfigError, match="source and destination storage locations"):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_rejects_overlapping_route_source_paths(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/")
    second = _route(name="fae-hourly", source_path="/data/fae/hourly/")

    with pytest.raises(ConfigError, match="source paths"):
        _ = AppSettings.from_env(_env(tmp_path, [first, second]))


@pytest.mark.unit()
def test_from_env_allows_same_storage_sibling_source_path_names(tmp_path: Path) -> None:
    first = _route(name="data", source_path="data")
    second = _route(name="database", source_path="database")

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.source.path for route in settings.routes] == ["data", "database"]


@pytest.mark.unit()
def test_from_env_allows_same_source_path_on_different_storage(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/")
    second = _route(name="other-storage", source_path="data/fae/")
    source = second["source"]
    assert isinstance(source, dict)
    source["endpoint_url"] = "http://localstack-alt:4566"

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.name for route in settings.routes] == ["fae", "other-storage"]
