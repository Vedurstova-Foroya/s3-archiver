"""Route-scoped payload helpers."""

from __future__ import annotations

from typing import cast

from s3_archiver_core.payload_utils import JsonValue, json_list
from s3_archiver_core.settings import AppSettings


def route_payloads(settings: AppSettings) -> list[dict[str, JsonValue]]:
    """Return route-scoped source and destination payload details."""

    return [
        {
            "name": route.name,
            "parser_kind": route.parser.value,
            "copy_mode": route.copy_mode.value,
            "source_bucket": route.source.bucket,
            "source_path": route.source.path,
            "destination_bucket": route.destination.bucket,
            "destination_path": route.destination.path,
        }
        for route in settings.routes
    ]


def route_summary_payload(settings: AppSettings | None) -> dict[str, JsonValue]:
    """Return route-aware bucket summary fields for user-facing payloads."""

    routes = route_payloads(settings) if settings is not None else []
    source_bucket, destination_bucket = _singular_route_buckets(routes)
    return {
        "source_bucket": source_bucket,
        "destination_bucket": destination_bucket,
        "source_buckets": _string_json_values(
            sorted({str(route["source_bucket"]) for route in routes})
        ),
        "destination_buckets": _string_json_values(
            sorted({str(route["destination_bucket"]) for route in routes})
        ),
        "routes": json_list(routes),
    }


def working_set_payload(settings: AppSettings) -> dict[str, JsonValue]:
    """Return the redacted startup working set for this invocation."""

    routes = route_payloads(settings)
    return {
        "route_count": len(routes),
        "routes": json_list(routes),
    }


def _string_json_values(items: list[str]) -> list[JsonValue]:
    return [cast(JsonValue, item) for item in items]


def _singular_route_buckets(routes: list[dict[str, JsonValue]]) -> tuple[str | None, str | None]:
    if len(routes) != 1:
        return None, None
    route = routes[0]
    source_bucket = route["source_bucket"]
    destination_bucket = route["destination_bucket"]
    return (
        source_bucket if isinstance(source_bucket, str) else None,
        destination_bucket if isinstance(destination_bucket, str) else None,
    )
