"""ARCHIVER_CONFIG_JSON parsing for route settings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

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
from s3_archiver_core._route_validation import (
    validate_bucket_whitelist as _validate_bucket_whitelist,
)
from s3_archiver_core._route_validation import validate_route_storage as _validate_route_storage
from s3_archiver_core._settings_factory import AppSettingsFactory
from s3_archiver_core._settings_models import (
    CopyMode,
    RouteSettings,
    S3LocationSettings,
    S3Provider,
)
from s3_archiver_core._settings_parse import (
    EnvDecoder,
    parse_bool_result,
    parse_string_array_result,
)
from s3_archiver_core._settings_parse import parse_runtime_duration_result as _duration_result
from s3_archiver_core.parsers import registry as _parser_registry
from s3_archiver_core.parsers.kinds import ParserKind
from s3_archiver_core.temp_files import default_temp_dir

type JsonObject = dict[str, object]
_ARRAY_ERROR = "ARCHIVER_CONFIG_JSON must be a non-empty JSON array of route objects"


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
    cleanup_enabled = decoder.consume(parse_bool_result(decoder.env, "CLEANUP", default=False))
    whitelist_enabled = decoder.consume(
        parse_bool_result(decoder.env, "ARCHIVER_BUCKET_WHITELIST_ENABLED", default=False)
    )
    bucket_whitelist = decoder.consume(
        parse_string_array_result(decoder.env, "ARCHIVER_BUCKET_WHITELIST")
    )
    if routes is not None and bucket_whitelist is not None and whitelist_enabled:
        _validate_bucket_whitelist(decoder, routes, bucket_whitelist)
    decoder.finish()
    assert routes is not None and run_timeout is not None and cleanup_enabled is not None
    assert whitelist_enabled is not None and bucket_whitelist is not None
    return settings_type(
        run_timeout=run_timeout,
        temp_dir=Path(decoder.env.get("ARCHIVER_TEMP_DIR", str(default_temp_dir()))),
        log_level=log_level,
        log_dir=Path(decoder.env.get("LOG_DIR", "/var/log/s3-archiver")),
        routes=routes,
        cleanup_enabled=cleanup_enabled,
        whitelist_enabled=whitelist_enabled,
        bucket_whitelist=bucket_whitelist,
    )


def _load_route_settings(decoder: EnvDecoder, raw_config: str) -> tuple[RouteSettings, ...] | None:
    try:
        parsed = cast(object, json.loads(raw_config))
    except json.JSONDecodeError:
        decoder.fail("ARCHIVER_CONFIG_JSON", "ARCHIVER_CONFIG_JSON must be valid JSON")
        return None
    if not isinstance(parsed, list) or len(cast(list[object], parsed)) == 0:
        decoder.fail("ARCHIVER_CONFIG_JSON", _ARRAY_ERROR)
        return None
    routes: list[RouteSettings] = []
    names: set[str] = set()
    for index, item in enumerate(cast(list[object], parsed)):
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


def _load_route(decoder: EnvDecoder, item: object, field: str) -> RouteSettings | None:
    route = _object_config(decoder, item, field)
    if route is None:
        return None
    name = _required_string(decoder, route, "name", f"{field}.name")
    parser = _load_parser_kind(decoder, route, f"{field}.parser")
    copy_mode = _load_copy_mode(decoder, route, f"{field}.copy_mode")
    source = _load_location(decoder, route.get("source"), f"{field}.source", "SOURCE")
    source_path = source.path if source is not None else ""
    destination = _load_location(
        decoder, route.get("destination"), f"{field}.destination", "DESTINATION", source_path
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
    decoder: EnvDecoder, item: object, field: str, side: str, path_default: str = ""
) -> S3LocationSettings | None:
    location = _object_config(decoder, item, field)
    if location is None:
        return None
    provider_text = _location_string(
        decoder, location, "provider", f"{field}.provider", side, default="custom"
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
    if provider is S3Provider.CUSTOM and endpoint_url is None:
        decoder.fail(
            f"{field}.endpoint_url",
            f"S3_ENDPOINT (or {field}.endpoint_url) is required when provider=custom",
        )
        return None
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
        default=path_default,
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
    suffix = {
        "access_key_id": "ACCESS_KEY",
        "secret_access_key": "SECRET_KEY",
        "endpoint_url": "ENDPOINT",
    }.get(key, key.upper())
    side_key = f"S3_{side}_{suffix}"
    if not shared:
        return (side_key,)
    return side_key, f"S3_{suffix}"
