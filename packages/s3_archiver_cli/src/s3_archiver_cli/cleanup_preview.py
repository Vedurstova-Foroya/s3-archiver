"""Cleanup preview helpers for the CLI."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from s3_archiver_core.archive_manifest import ArchiveManifest, ManifestEntry, build_archive_manifest
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.errors import ArchiveRunError, S3ArchiverError
from s3_archiver_core.health import run_health_check
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings
from s3_archiver_core.temp_files import prepare_runtime_temp_dir

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]

_PREVIEW_PREFIX = "cleanup-preview-"
_CLEANUP_PREVIEW_GROUP_STATUS = "skipped"


def run_cleanup_preview(
    settings: AppSettings,
    log_file: Path,
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, JsonValue]:
    """Build and persist the cleanup manifest without deleting any source objects."""

    clock = _utc_now if now is None else now
    started = clock()
    try:
        prepare_runtime_temp_dir(settings.temp_dir)
        _ = run_health_check(settings, log_file)
        source = S3ArchiveBucket(
            build_s3_client(settings.source),
            settings.source.bucket,
            settings.temp_dir,
        )
        options = ArchiveOptions.from_settings(settings)
        manifest = build_archive_manifest(
            source,
            run_started_at_utc=started,
            retention_days=options.retention_days,
            versioning_state=source.versioning_state(),
            source_filter=options.source_filter,
        )
    except S3ArchiverError:
        raise
    except Exception as exc:
        raise ArchiveRunError(str(exc)) from exc
    payload = _cleanup_preview_payload(settings, log_file, manifest)
    try:
        _write_cleanup_preview_file(payload, settings.temp_dir)
        return payload
    except S3ArchiverError:
        raise
    except Exception as exc:
        raise ArchiveRunError(str(exc)) from exc


def _cleanup_preview_payload(
    settings: AppSettings,
    log_file: Path,
    manifest: ArchiveManifest,
) -> dict[str, JsonValue]:
    target_day = _manifest_target_day(manifest)
    archive_groups = _archive_group_payloads(manifest)
    destination_archive_keys = [group["destination_archive_key"] for group in archive_groups]
    skipped_objects = _skipped_object_payloads(manifest)
    archive_group_values = _json_list(archive_groups)
    skipped_object_values = _json_list(skipped_objects)
    preview: dict[str, JsonValue] = {
        "cleanup_enabled_in_settings": settings.cleanup_enabled,
        "object_count": len(manifest.entries),
        "target_day": target_day,
        "archive_count": len(archive_groups),
        "source_object_count": len(manifest.entries),
        "skipped_object_count": len(skipped_objects),
        "destination_archive_keys": destination_archive_keys,
        "archive_groups": archive_group_values,
        "skipped_objects": skipped_object_values,
        "run_started_at_utc": manifest.run_started_at_utc.isoformat(),
        "retention_cutoff_utc": manifest.retention_cutoff_utc.isoformat(),
        "entries": _json_list([_manifest_entry_payload(entry) for entry in manifest.entries]),
    }
    return {
        "status": "ok",
        "source_bucket": settings.source.bucket,
        "destination_bucket": settings.destination.bucket,
        "log_file": str(log_file),
        "cleanup_preview": preview,
    }


def _write_cleanup_preview_file(payload: dict[str, JsonValue], temp_dir: Path) -> None:
    preview_path = temp_dir / f"{_PREVIEW_PREFIX}{uuid4().hex}.json"
    preview = cast(dict[str, JsonValue], payload["cleanup_preview"]).copy()
    preview["manifest_file"] = str(preview_path)
    payload["cleanup_preview"] = preview
    _ = preview_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _manifest_entry_payload(entry: ManifestEntry) -> dict[str, JsonValue]:
    return {
        "source_bucket": entry.source_bucket,
        "key": entry.key,
        "version_id": entry.version_id,
        "size": entry.size,
        "etag": entry.etag,
        "last_modified_utc": entry.last_modified.isoformat(),
    }


def _manifest_target_day(manifest: object) -> str:
    value = _attr(manifest, "target_day", "target_date")
    if value is not None:
        return _date_text(value)
    cutoff = _attr(manifest, "retention_cutoff_utc")
    if isinstance(cutoff, datetime):
        return cutoff.date().isoformat()
    return ""


def _archive_group_payloads(manifest: object) -> list[dict[str, JsonValue]]:
    groups = _attr(manifest, "archive_groups", "groups", "grouped_archive_entries")
    if groups is not None:
        return [_archive_group_payload(group, manifest) for group in _object_list(groups)]
    grouped_entries: dict[str, list[object]] = {}
    for entry in cast(tuple[object, ...], _attr(manifest, "entries") or ()):
        grouped_entries.setdefault(_entry_destination_archive_key(entry), []).append(entry)
    return [
        _archive_group_payload_from_entries(destination_key, entries, manifest)
        for destination_key, entries in sorted(grouped_entries.items())
    ]


def _archive_group_payload(group: object, manifest: object) -> dict[str, JsonValue]:
    entries = _object_list(_attr(group, "entries", "source_entries", "objects"))
    skipped = _object_list(_attr(group, "skipped_objects", "skipped_entries"))
    target_day = _attr(group, "target_day", "target_date") or _manifest_target_day(manifest)
    return {
        "target_day": _date_text(target_day),
        "archive_root": _string_or_none(_attr(group, "archive_root", "root")),
        "destination_archive_key": _group_destination_archive_key(group, entries),
        "source_object_count": _count_from_attr(group, "source_object_count", entries),
        "skipped_object_count": _count_from_attr(group, "skipped_object_count", skipped),
        "cleanup_status": _CLEANUP_PREVIEW_GROUP_STATUS,
        "skipped_objects": _json_list([_skipped_object_payload(item) for item in skipped]),
        "source_objects": _json_list([_entry_reference_payload(entry) for entry in entries]),
    }


def _archive_group_payload_from_entries(
    destination_key: str,
    entries: list[object],
    manifest: object,
) -> dict[str, JsonValue]:
    return {
        "target_day": _manifest_target_day(manifest),
        "archive_root": _string_or_none(_attr(entries[0], "archive_root")) if entries else None,
        "destination_archive_key": destination_key,
        "source_object_count": len(entries),
        "skipped_object_count": 0,
        "cleanup_status": _CLEANUP_PREVIEW_GROUP_STATUS,
        "skipped_objects": [],
        "source_objects": _json_list([_entry_reference_payload(entry) for entry in entries]),
    }


def _skipped_object_payloads(manifest: object) -> list[dict[str, JsonValue]]:
    skipped = _attr(manifest, "skipped_objects", "skipped_entries", "skipped")
    if skipped is None:
        return []
    return [_skipped_object_payload(item) for item in _object_list(skipped)]


def _skipped_object_payload(item: object) -> dict[str, JsonValue]:
    return {
        "key": _string_or_none(_attr(item, "key", "source_key")),
        "reason": _string_or_none(_attr(item, "reason", "skip_reason")),
        "target_day": _string_or_none(_attr(item, "target_day", "target_date")),
        "archive_root": _string_or_none(_attr(item, "archive_root", "root")),
    }


def _entry_reference_payload(entry: object) -> dict[str, JsonValue]:
    return {
        "key": _string_or_none(_attr(entry, "key", "source_key")),
        "version_id": _string_or_none(_attr(entry, "version_id")),
        "size": _int_or_none(_attr(entry, "size")),
        "destination_archive_key": _entry_destination_archive_key(entry),
    }


def _group_destination_archive_key(group: object, entries: list[object]) -> str:
    value = _attr(group, "destination_archive_key", "archive_key", "key")
    if value is not None:
        return str(value)
    keys = _attr(group, "destination_archive_keys", "archive_keys")
    if keys is not None:
        first = next(iter(_object_list(keys)), None)
        if first is not None:
            return str(first)
    return _entry_destination_archive_key(entries[0]) if entries else ""


def _entry_destination_archive_key(entry: object) -> str:
    value = _attr(entry, "destination_archive_key", "archive_key")
    return str(value if value is not None else _attr(entry, "key", "source_key") or "")


def _count_from_attr(source: object, name: str, fallback_items: list[object]) -> int:
    value = _attr(source, name)
    return value if isinstance(value, int) else len(fallback_items)


def _date_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _attr(source: object, *names: str) -> object | None:
    for name in names:
        if hasattr(source, name):
            return cast(object, getattr(source, name))
    return None


def _object_list(value: object | None) -> list[object]:
    if value is None or isinstance(value, str):
        return []
    if isinstance(value, Iterable):
        return list(value)
    return []


def _string_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _json_list(items: list[dict[str, JsonValue]]) -> list[JsonValue]:
    return [cast(JsonValue, item) for item in items]


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(tz=UTC)
