"""Cross-route validation for ARCHIVER_CONFIG_JSON settings."""

from __future__ import annotations

from s3_archiver_core._archive_manifest_paths import route_paths_overlap
from s3_archiver_core._settings_models import RouteSettings
from s3_archiver_core._settings_parse import EnvDecoder


def validate_bucket_whitelist(
    decoder: EnvDecoder, routes: tuple[RouteSettings, ...], whitelist: tuple[str, ...]
) -> None:
    """Fail when any route source/destination bucket is missing from the whitelist."""

    allowed = frozenset(whitelist)
    for route in routes:
        for side, location in (("source", route.source), ("destination", route.destination)):
            if location.bucket not in allowed:
                message = (
                    f"route {route.name!r} {side} bucket {location.bucket!r}"
                    + " is not in ARCHIVER_BUCKET_WHITELIST"
                )
                decoder.fail("ARCHIVER_BUCKET_WHITELIST", message)
                return


def validate_route_storage(decoder: EnvDecoder, routes: tuple[RouteSettings, ...]) -> None:
    """Reject identical source/destination storage and overlapping source paths."""

    for route in routes:
        if route.source.storage_identity() == route.destination.storage_identity():
            decoder.fail(
                "ARCHIVER_CONFIG_JSON",
                f"route {route.name!r} source and destination storage locations must differ",
            )
            return
    for left_index, left in enumerate(routes):
        for right in routes[left_index + 1 :]:
            same_storage = left.source.storage_identity() == right.source.storage_identity()
            if same_storage and route_paths_overlap(left.source.path, right.source.path):
                decoder.fail(
                    "ARCHIVER_CONFIG_JSON",
                    f"source paths for routes {left.name!r} and {right.name!r} overlap",
                )
                return
