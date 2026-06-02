"""Reusable health-check test helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import cast

from botocore.exceptions import ClientError, EndpointConnectionError

_DEFAULT_PARSER_SAMPLE_KEY = "2024-01-01T00:00:00Z_object.bin"


def _list_response(keys: tuple[str, ...]) -> dict[str, object]:
    contents = [
        {"Key": key, "Size": 1, "LastModified": datetime(2024, 1, 1, tzinfo=UTC)} for key in keys
    ]
    return {"Contents": contents, "IsTruncated": False}


class SuccessfulClient:
    """Minimal client double for successful requests."""

    called_bucket: str | None = None
    _versioning_status: str | None
    _sample_keys: tuple[str, ...]

    def __init__(
        self,
        versioning_status: str | None = "Enabled",
        *,
        sample_keys: tuple[str, ...] = (_DEFAULT_PARSER_SAMPLE_KEY,),
    ) -> None:
        self._versioning_status = versioning_status
        self._sample_keys = sample_keys

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        self.called_bucket = Bucket

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, str]:  # noqa: N803
        self.called_bucket = Bucket
        if self._versioning_status is None:
            return {}
        return {"Status": self._versioning_status}

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return _list_response(self._sample_keys)

    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        versions = [
            {
                "Key": key,
                "Size": 1,
                "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
                "IsLatest": True,
                "VersionId": "v1",
            }
            for key in self._sample_keys
        ]
        return {"Versions": versions, "IsTruncated": False}


class AuthFailingClient:
    """Minimal client double for authentication failures."""

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        _ = Bucket
        raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "HeadBucket")

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, str]:  # noqa: N803
        _ = Bucket
        return {"Status": "Enabled"}


class ConnectivityFailingClient:
    """Minimal client double for connectivity failures."""

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        _ = Bucket
        raise EndpointConnectionError(endpoint_url="http://localstack:4566")

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, str]:  # noqa: N803
        _ = Bucket
        return {"Status": "Enabled"}


class VersioningFailingClient:
    """Minimal client double for source versioning failures."""

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        _ = Bucket

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, str]:  # noqa: N803
        _ = Bucket
        raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "GetBucketVersioning")


def multi_route_env(env: dict[str, str]) -> dict[str, str]:
    updated = dict(env)
    routes = cast(list[dict[str, object]], json.loads(updated["ARCHIVER_CONFIG_JSON"]))
    first = routes[0]
    second = deepcopy(first)
    second["name"] = "secondary"
    second["parser"] = "direct"
    second["copy_mode"] = "direct"
    second_source = cast(dict[str, object], second["source"])
    second_source["bucket"] = "second-source-bucket"
    second_source["path"] = "raw/"
    second_destination = cast(dict[str, object], second["destination"])
    second_destination["bucket"] = "second-destination-bucket"
    second_destination["path"] = "mirror/"
    routes.append(second)
    updated["ARCHIVER_CONFIG_JSON"] = json.dumps(routes)
    return updated
