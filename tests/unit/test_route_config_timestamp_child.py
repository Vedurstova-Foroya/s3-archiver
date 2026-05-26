"""Route configuration tests for timestamp-child archive routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.parsers import ParserKind
from s3_archiver_core.settings import AppSettings, CopyMode


@pytest.mark.unit()
def test_from_env_decodes_folder_timestamp_child_archive_route(tmp_path: Path) -> None:
    settings = AppSettings.from_env(
        _env(
            tmp_path,
            _route(
                parser="folder_timestamp_child",
                copy_mode="timestamp_child_tar_gz",
                source_path="data/wrf/ecmwf/",
            ),
        )
    )

    route = settings.routes[0]
    assert route.parser == ParserKind("folder_timestamp_child")
    assert route.copy_mode is CopyMode.TIMESTAMP_CHILD_TAR_GZ
    assert route.source.path == "data/wrf/ecmwf/"


@pytest.mark.unit()
def test_from_env_rejects_timestamp_child_copy_mode_for_other_parsers(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="folder_timestamp_child"):
        _ = AppSettings.from_env(_env(tmp_path, _route(copy_mode="timestamp_child_tar_gz")))


@pytest.mark.unit()
def test_from_env_rejects_folder_timestamp_child_with_daily_tar_gz(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="timestamp_child_tar_gz"):
        _ = AppSettings.from_env(
            _env(
                tmp_path,
                _route(
                    parser="folder_timestamp_child",
                    copy_mode="daily_tar_gz",
                    source_path="data/wrf/ecmwf/",
                ),
            )
        )


@pytest.mark.unit()
def test_from_env_rejects_folder_timestamp_child_with_direct(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="timestamp_child_tar_gz"):
        _ = AppSettings.from_env(
            _env(
                tmp_path,
                _route(
                    parser="folder_timestamp_child",
                    copy_mode="direct",
                    source_path="data/wrf/ecmwf/",
                ),
            )
        )


@pytest.mark.unit()
def test_from_env_rejects_object_copy_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="copy_mode"):
        _ = AppSettings.from_env(_env(tmp_path, _route(copy_mode={"type": "daily_tar_gz"})))


def _route(
    *,
    parser: str = "filename_timestamp",
    copy_mode: object = "daily_tar_gz",
    source_path: str = "data/fae/",
) -> dict[str, object]:
    return {
        "name": "route",
        "parser": parser,
        "copy_mode": copy_mode,
        "source": {"bucket": "source-bucket", "path": source_path},
        "destination": {"bucket": "archive-bucket", "path": "archives/"},
    }


def _env(tmp_path: Path, route: dict[str, object]) -> dict[str, str]:
    return {
        "ARCHIVER_CONFIG_JSON": json.dumps([route]),
        "LOG_DIR": str(tmp_path / "logs"),
        "S3_SOURCE_ACCESS_KEY": "source-access",
        "S3_SOURCE_SECRET_KEY": "source-secret",
        "S3_SOURCE_ENDPOINT": "http://localhost:4566",
        "S3_DESTINATION_ACCESS_KEY": "destination-access",
        "S3_DESTINATION_SECRET_KEY": "destination-secret",
        "S3_DESTINATION_ENDPOINT": "http://localhost:4567",
    }
