"""Destination-side route config validation edge tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import AppSettings


def _route() -> dict[str, object]:
    return {
        "name": "fae-daily",
        "parser": "filename_timestamp",
        "copy_mode": "daily_tar_gz",
        "source": {
            "provider": "localstack",
            "endpoint_url": "http://localstack:4566/",
            "region": "us-east-1",
            "bucket": "source-bucket",
            "path": "data/fae/",
            "access_key_id": "${S3_SOURCE_ACCESS_KEY}",
            "secret_access_key": "${S3_SOURCE_SECRET_KEY}",
            "addressing_style": "path",
        },
        "destination": {
            "provider": "localstack",
            "endpoint_url": "http://localhost:4566",
            "region": "us-east-1",
            "bucket": "archive-bucket",
            "path": "archives/fae/",
            "access_key_id": "destination-access",
            "secret_access_key": "destination-secret",
            "addressing_style": "path",
        },
    }


def _env(tmp_path: Path, route: dict[str, object]) -> dict[str, str]:
    return {
        "ARCHIVER_CONFIG_JSON": json.dumps([route]),
        "S3_SOURCE_ACCESS_KEY": "source-access",
        "S3_SOURCE_SECRET_KEY": "source-secret",
        "LOG_DIR": str(tmp_path / "logs"),
    }


@pytest.mark.unit()
def test_from_env_rejects_invalid_route_destination_fields(tmp_path: Path) -> None:
    route = _route()
    destination = cast(dict[str, object], route["destination"])
    destination["endpoint_url"] = 4566

    with pytest.raises(ConfigError, match="endpoint_url"):
        _ = AppSettings.from_env(_env(tmp_path, route))
