"""Tests for the ARCHIVER_BUCKET_WHITELIST startup check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import AppSettings


def _route(
    *,
    name: str = "fae-daily",
    source_bucket: str = "source-bucket",
    destination_bucket: str = "archive-bucket",
) -> dict[str, object]:
    return {
        "name": name,
        "parser": "filename_timestamp",
        "copy_mode": "daily_tar_gz",
        "source": {
            "provider": "localstack",
            "endpoint_url": "http://localstack:4566/",
            "region": "us-east-1",
            "bucket": source_bucket,
            "path": "data/fae/",
            "access_key_id": "source-access",
            "secret_access_key": "source-secret",
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


def _env(
    tmp_path: Path,
    routes: list[dict[str, object]],
    **overrides: str,
) -> dict[str, str]:
    return {
        "ARCHIVER_CONFIG_JSON": json.dumps(routes),
        "LOG_DIR": str(tmp_path / "logs"),
        **overrides,
    }


@pytest.mark.unit()
def test_defaults_to_disabled_with_empty_whitelist(tmp_path: Path) -> None:
    settings = AppSettings.from_env(_env(tmp_path, [_route()]))

    assert settings.whitelist_enabled is False
    assert settings.bucket_whitelist == ()


@pytest.mark.unit()
def test_disabled_whitelist_ignores_non_whitelisted_buckets(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        [_route()],
        ARCHIVER_BUCKET_WHITELIST_ENABLED="false",
        ARCHIVER_BUCKET_WHITELIST=json.dumps(["unrelated-bucket"]),
    )

    settings = AppSettings.from_env(env)

    assert settings.whitelist_enabled is False
    assert settings.bucket_whitelist == ("unrelated-bucket",)


@pytest.mark.unit()
def test_enabled_whitelist_allows_listed_buckets(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        [_route()],
        ARCHIVER_BUCKET_WHITELIST_ENABLED="true",
        ARCHIVER_BUCKET_WHITELIST=json.dumps(["source-bucket", "archive-bucket"]),
    )

    settings = AppSettings.from_env(env)

    assert settings.whitelist_enabled is True
    assert settings.bucket_whitelist == ("source-bucket", "archive-bucket")


@pytest.mark.unit()
def test_enabled_whitelist_rejects_non_whitelisted_source_bucket(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        [_route()],
        ARCHIVER_BUCKET_WHITELIST_ENABLED="true",
        ARCHIVER_BUCKET_WHITELIST=json.dumps(["archive-bucket"]),
    )

    with pytest.raises(ConfigError, match="source bucket 'source-bucket' is not in"):
        _ = AppSettings.from_env(env)


@pytest.mark.unit()
def test_enabled_whitelist_rejects_non_whitelisted_destination_bucket(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        [_route()],
        ARCHIVER_BUCKET_WHITELIST_ENABLED="true",
        ARCHIVER_BUCKET_WHITELIST=json.dumps(["source-bucket"]),
    )

    with pytest.raises(ConfigError, match="destination bucket 'archive-bucket' is not in"):
        _ = AppSettings.from_env(env)


@pytest.mark.unit()
def test_enabled_whitelist_checks_every_route(tmp_path: Path) -> None:
    routes = [
        _route(name="first", source_bucket="source-bucket"),
        _route(name="second", source_bucket="rogue-bucket", destination_bucket="other-archive"),
    ]
    env = _env(
        tmp_path,
        routes,
        ARCHIVER_BUCKET_WHITELIST_ENABLED="true",
        ARCHIVER_BUCKET_WHITELIST=json.dumps(["source-bucket", "archive-bucket", "other-archive"]),
    )

    with pytest.raises(ConfigError, match="route 'second' source bucket 'rogue-bucket'"):
        _ = AppSettings.from_env(env)


@pytest.mark.unit()
def test_enabled_whitelist_with_empty_list_rejects_everything(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        [_route()],
        ARCHIVER_BUCKET_WHITELIST_ENABLED="true",
        ARCHIVER_BUCKET_WHITELIST="[]",
    )

    with pytest.raises(ConfigError, match="is not in ARCHIVER_BUCKET_WHITELIST"):
        _ = AppSettings.from_env(env)


@pytest.mark.unit()
def test_rejects_malformed_whitelist_json(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        [_route()],
        ARCHIVER_BUCKET_WHITELIST_ENABLED="true",
        ARCHIVER_BUCKET_WHITELIST="not-json",
    )

    with pytest.raises(ConfigError, match="must be a JSON array of strings"):
        _ = AppSettings.from_env(env)


@pytest.mark.unit()
def test_rejects_invalid_whitelist_enabled_flag(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        [_route()],
        ARCHIVER_BUCKET_WHITELIST_ENABLED="maybe",
    )

    with pytest.raises(ConfigError, match="must be true or false"):
        _ = AppSettings.from_env(env)
