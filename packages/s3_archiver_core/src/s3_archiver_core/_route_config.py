"""ARCHIVER_CONFIG_JSON parsing for route settings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from s3_archiver_core._archive_manifest_paths import route_paths_overlap
from s3_archiver_core._route_config_fields import addressing_style as _addressing_style
from s3_archiver_core._route_config_fields import endpoint as _endpoint
from s3_archiver_core._route_config_fields import normalize_s3_prefix as _normalize_s3_prefix
from s3_archiver_core._route_config_fields import object_config as _object_config
from s3_archiver_core._route_config_fields import optional_string as _optional_string
from s3_archiver_core._route_config_fields import provider as _provider
from s3_archiver_core._route_config_fields import required_string as _required_string
from s3_archiver_core._route_config_fields import (
    validate_localstack_endpoint_host as _validate_localstack_endpoint_host,
)
from s3_archiver_core._settings_factory import AppSettingsFactory
from s3_archiver_core._settings_models import (
    CopyMode,
    RouteSettings,
    S3LocationSettings,
    S3Provider,
)
from s3_archiver_core._settings_parse import EnvDecoder
from s3_archiver_core._settings_parse import parse_runtime_duration_result as _duration_result
from s3_archiver_core.parsers import registry as _parser_registry
from s3_archiver_core.parsers.kinds import ParserKind
from s3_archiver_core.temp_files import default_temp_dir

type JsonObject = dict[str, object]


def load_app_settings_from_config_json[T](
    settings_type: AppSettingsFactory[T],
    decoder: EnvDecoder,
    raw_config: str,
    log_level: str,
) -> T:
    """Build app settings from the route JSON env value."""

    routes = _load_route_settings(decoder, raw_config)
    run_timeout = decoder.consume(
        _duration_result(decoder.env.get("ARCHIVER_RUN_TIMEOUT", "7d"), "ARCHIVER_RUN_TIMEOUT")
    )
    decoder.finish()
    assert routes is not None and run_timeout is not None
    return settings_type(
        run_timeout=run_timeout,
        temp_dir=Path(decoder.env.get("ARCHIVER_TEMP_DIR", str(default_temp_dir()))),
        log_level=log_level,
        log_dir=Path(decoder.env.get("LOG_DIR", "/var/log/s3-archiver")),
        routes=routes,
    )


def _load_route_settings(decoder: EnvDecoder, raw_config: str) -> tuple[RouteSettings, ...] | None:
    try:
        parsed = cast(object, json.loads(raw_config))
    except json.JSONDecodeError:
        decoder.fail("ARCHIVER_CONFIG_JSON", "ARCHIVER_CONFIG_JSON must be valid JSON")
        return None
    if not isinstance(parsed, list):
        _fail_array(decoder)
        return None
    parsed_routes = cast(list[object], parsed)
    if len(parsed_routes) == 0:
        _fail_array(decoder)
        return None
    routes: list[RouteSettings] = []
    names: set[str] = set()
    for index, item in enumerate(parsed_routes):
        route = _load_route(decoder, item, f"ARCHIVER_CONFIG_JSON[{index}]")
        if route is None:
            return None
        if route.name in names:
            decoder.fail("ARCHIVER_CONFIG_JSON", f"duplicate route name {route.name!r}")
            return None
        names.add(route.name)
        routes.append(route)
    _validate_route_storage(decoder, tuple(routes))
    return tuple(routes)


def _fail_array(decoder: EnvDecoder) -> None:
    decoder.fail(
        "ARCHIVER_CONFIG_JSON",
        "ARCHIVER_CONFIG_JSON must be a non-empty JSON array of route objects",
    )


def _load_route(decoder: EnvDecoder, item: object, field: str) -> RouteSettings | None:
    route = _object_config(decoder, item, field)
    if route is None:
        return None
    name = _required_string(decoder, route, "name", f"{field}.name")
    parser = _load_parser_kind(decoder, route, f"{field}.parser")
    copy_mode = _load_copy_mode(decoder, route, f"{field}.copy_mode")
    source = _load_location(decoder, route.get("source"), f"{field}.source", "SOURCE")
    destination = _load_location(
        decoder, route.get("destination"), f"{field}.destination", "DESTINATION"
    )
    if name is None or parser is None or copy_mode is None or source is None or destination is None:
        return None
    if not _validate_parser_copy_mode(decoder, parser, copy_mode, f"{field}.copy_mode"):
        return None
    _validate_localstack_endpoint_host(decoder, f"{field}.source.endpoint_url", source)
    _validate_localstack_endpoint_host(decoder, f"{field}.destination.endpoint_url", destination)
    return RouteSettings(name, parser, copy_mode, source, destination)


def _load_parser_kind(
    decoder: EnvDecoder, route: Mapping[str, object], field: str
) -> ParserKind | None:
    value = _required_string(decoder, route, "parser", field)
    valid = frozenset({kind.value for kind in registered_parser_kinds()})
    if value is None:
        return None
    if value not in valid:
        decoder.fail(field, f"{field} must be one of {valid}, got {value!r}")
        return None
    return ParserKind(value)


def registered_parser_kinds() -> frozenset[ParserKind]:
    """Return parser kinds accepted by route configuration."""

    return _parser_registry.registered_parser_kinds()


def _load_copy_mode(
    decoder: EnvDecoder, route: Mapping[str, object], field: str
) -> CopyMode | None:
    value = _required_string(decoder, route, "copy_mode", field)
    valid = frozenset({mode.value for mode in CopyMode})
    if value is None:
        return None
    if value not in valid:
        decoder.fail(field, f"{field} must be one of {valid}, got {value!r}")
        return None
    return CopyMode(value)


def _validate_parser_copy_mode(
    decoder: EnvDecoder, parser: ParserKind, copy_mode: CopyMode, field: str
) -> bool:
    folder_timestamp_child = ParserKind("folder_timestamp_child")
    if copy_mode is CopyMode.TIMESTAMP_CHILD_TAR_GZ and parser != folder_timestamp_child:
        decoder.fail(field, f"{field} requires parser=folder_timestamp_child")
        return False
    if parser == folder_timestamp_child and copy_mode is not CopyMode.TIMESTAMP_CHILD_TAR_GZ:
        decoder.fail(
            field, f"{field} must be timestamp_child_tar_gz for parser=folder_timestamp_child"
        )
        return False
    return True


def _load_location(
    decoder: EnvDecoder, item: object, field: str, side: str
) -> S3LocationSettings | None:
    location = _object_config(decoder, item, field)
    if location is None:
        return None
    provider_text = _location_string(
        decoder, location, "provider", f"{field}.provider", side, default="localstack"
    )
    if provider_text is None:
        return None
    provider = _provider(decoder, provider_text, f"{field}.provider")
    addressing_text = _location_string(
        decoder,
        location,
        "addressing_style",
        f"{field}.addressing_style",
        side,
        default="path",
    )
    if addressing_text is None:
        return None
    addressing = _addressing_style(
        decoder, {"addressing_style": addressing_text}, f"{field}.addressing_style"
    )
    if provider is None or addressing is None:
        return None
    namespace = _location_string(
        decoder, location, "namespace", f"{field}.namespace", side, required=False
    )
    iam_user_ocid = _location_string(
        decoder, location, "iam_user_ocid", f"{field}.iam_user_ocid", side, required=False
    )
    if provider is S3Provider.OCI and namespace is None:
        decoder.fail(f"{field}.namespace", f"{field}.namespace is required when provider=oci")
        return None
    if provider is S3Provider.OCI and iam_user_ocid is None:
        decoder.fail(
            f"{field}.iam_user_ocid", f"{field}.iam_user_ocid is required when provider=oci"
        )
        return None
    endpoint_text = _location_string(
        decoder, location, "endpoint_url", f"{field}.endpoint_url", side, required=False
    )
    endpoint_url = (
        None
        if endpoint_text is None
        else _endpoint(decoder, {"endpoint_url": endpoint_text}, f"{field}.endpoint_url")
    )
    access_key_id = _location_string(
        decoder, location, "access_key_id", f"{field}.access_key_id", side
    )
    secret_access_key = _location_string(
        decoder, location, "secret_access_key", f"{field}.secret_access_key", side
    )
    region = _location_string(
        decoder, location, "region", f"{field}.region", side, default="us-east-1"
    )
    bucket = _location_string(decoder, location, "bucket", f"{field}.bucket", side, shared=False)
    path = _location_string(
        decoder,
        location,
        "path",
        f"{field}.path",
        side,
        required=False,
        shared=False,
        default="",
    )
    if None in {access_key_id, secret_access_key, region, bucket, path}:
        return None
    assert access_key_id is not None and secret_access_key is not None
    assert region is not None and bucket is not None and path is not None
    return S3LocationSettings(
        provider,
        access_key_id,
        secret_access_key,
        region,
        bucket,
        namespace,
        iam_user_ocid,
        endpoint_url,
        addressing,
        _normalize_s3_prefix(path),
    )


def _location_string(
    decoder: EnvDecoder,
    location: Mapping[str, object],
    key: str,
    field: str,
    side: str,
    *,
    required: bool = True,
    shared: bool = True,
    default: str | None = None,
) -> str | None:
    if key in location:
        if required:
            return _required_string(decoder, location, key, field)
        return _optional_string(decoder, location, key, field, default=default)

    for env_key in _location_env_keys(side, key, shared=shared):
        env_value = decoder.env.get(env_key)
        if env_value is not None and env_value.strip() != "":
            return env_value.strip()
    if default is not None or not required:
        return default
    decoder.fail(field, f"{field} is required")
    return None


def _location_env_keys(side: str, key: str, *, shared: bool) -> tuple[str, ...]:
    suffix = key.upper()
    side_key = f"S3_{side}_{suffix}"
    if not shared:
        return (side_key,)
    return side_key, f"S3_{suffix}"


def _validate_route_storage(decoder: EnvDecoder, routes: tuple[RouteSettings, ...]) -> None:
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
