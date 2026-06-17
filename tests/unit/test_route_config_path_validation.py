"""Symmetric source/destination path validation for ARCHIVER_CONFIG_JSON routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import AppSettings


def _route(
    *,
    name: str = "fae-daily",
    source_path: str = "data/fae/",
    destination_bucket: str = "archive-bucket",
    destination_path: str = "archives/fae/",
) -> dict[str, object]:
    return {
        "name": name,
        "parser": "filename_timestamp",
        "copy_mode": "daily_tar_gz",
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
            "path": destination_path,
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


@pytest.mark.unit()
def test_from_env_rejects_same_route_source_and_destination_storage(tmp_path: Path) -> None:
    route = _route(destination_bucket="source-bucket")
    destination = route["destination"]
    assert isinstance(destination, dict)
    destination["endpoint_url"] = "http://localstack:4566"

    with pytest.raises(ConfigError, match="source and destination storage locations"):
        _ = AppSettings.from_env(_env(tmp_path, [route]))


@pytest.mark.unit()
def test_from_env_allows_nested_route_source_paths(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/", destination_path="archives/fae/")
    second = _route(
        name="fae-hourly",
        source_path="/data/fae/hourly/",
        destination_path="archives/fae/hourly/",
    )

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.name for route in settings.routes] == ["fae", "fae-hourly"]
    assert [route.source.path for route in settings.routes] == ["data/fae/", "data/fae/hourly/"]


@pytest.mark.unit()
def test_from_env_rejects_identical_source_paths_same_storage(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/", destination_path="archives/one/")
    second = _route(name="fae-copy", source_path="data/fae/", destination_path="archives/two/")

    with pytest.raises(ConfigError, match="identical source paths"):
        _ = AppSettings.from_env(_env(tmp_path, [first, second]))


@pytest.mark.unit()
def test_from_env_allows_same_storage_sibling_source_path_names(tmp_path: Path) -> None:
    first = _route(name="data", source_path="data", destination_path="archives/data/")
    second = _route(name="database", source_path="database", destination_path="archives/database/")

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.source.path for route in settings.routes] == ["data", "database"]


@pytest.mark.unit()
def test_from_env_allows_same_source_path_on_different_storage(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/", destination_path="archives/one/")
    second = _route(name="other-storage", source_path="data/fae/", destination_path="archives/two/")
    source = second["source"]
    assert isinstance(source, dict)
    source["endpoint_url"] = "http://localstack-alt:4566"

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.name for route in settings.routes] == ["fae", "other-storage"]


@pytest.mark.unit()
def test_from_env_rejects_identical_destination_paths_same_storage(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/", destination_path="archives/shared/")
    second = _route(name="hav", source_path="data/hav/", destination_path="archives/shared/")

    with pytest.raises(ConfigError, match="identical destination paths"):
        _ = AppSettings.from_env(_env(tmp_path, [first, second]))


@pytest.mark.unit()
def test_from_env_allows_identical_destination_paths_on_different_storage(tmp_path: Path) -> None:
    first = _route(
        name="fae",
        source_path="data/fae/",
        destination_bucket="archive-one",
        destination_path="archives/shared/",
    )
    second = _route(
        name="hav",
        source_path="data/hav/",
        destination_bucket="archive-two",
        destination_path="archives/shared/",
    )

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.destination.bucket for route in settings.routes] == ["archive-one", "archive-two"]


@pytest.mark.unit()
def test_from_env_allows_nested_destination_paths_same_storage(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/", destination_path="archives/")
    second = _route(name="hav", source_path="data/hav/", destination_path="archives/fae/")

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.destination.path for route in settings.routes] == ["archives/", "archives/fae/"]


@pytest.mark.unit()
def test_from_env_allows_nested_routes_with_default_destinations(tmp_path: Path) -> None:
    first = _route(name="fae", source_path="data/fae/")
    second = _route(name="fae-hourly", source_path="data/fae/hourly/")
    for route in (first, second):
        destination = route["destination"]
        assert isinstance(destination, dict)
        del destination["path"]

    settings = AppSettings.from_env(_env(tmp_path, [first, second]))

    assert [route.destination.path for route in settings.routes] == [
        "data/fae/",
        "data/fae/hourly/",
    ]


@pytest.mark.unit()
def test_from_env_rejects_default_destination_collision_across_source_storage(
    tmp_path: Path,
) -> None:
    # Both routes share the same source path but live on different source storage,
    # so the source check passes. With destination paths omitted, each destination
    # defaults to its own source path "data/fae/" on the shared destination storage,
    # so the destination check must still reject the collision.
    first = _route(name="fae", source_path="data/fae/")
    second = _route(name="other-storage", source_path="data/fae/")
    source = second["source"]
    assert isinstance(source, dict)
    source["endpoint_url"] = "http://localstack-alt:4566"
    for route in (first, second):
        destination = route["destination"]
        assert isinstance(destination, dict)
        del destination["path"]

    with pytest.raises(ConfigError, match="identical destination paths"):
        _ = AppSettings.from_env(_env(tmp_path, [first, second]))
