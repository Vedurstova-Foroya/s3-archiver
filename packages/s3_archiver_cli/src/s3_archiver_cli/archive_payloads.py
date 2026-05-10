"""Shared archive payload shaping helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import cast

from s3_archiver_core.archive import ArchivePhaseResult

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]


def phase_status(result: ArchivePhaseResult) -> str:
    """Return the structured status string for one archive phase."""

    return "error" if not result.ok else "skipped" if result.skipped else "ok"


def manifest_target_day(manifest: object) -> str:
    """Return the manifest target day as an ISO date string."""

    value = attr(manifest, "target_day", "target_date")
    if value is not None:
        return date_text(value)
    cutoff = attr(manifest, "retention_cutoff_utc")
    if isinstance(cutoff, datetime):
        return cutoff.date().isoformat()
    return ""


def archive_group_payloads(
    manifest: object, *, cleanup_status: str | None = None
) -> list[dict[str, JsonValue]]:
    """Return group payloads from either new group models or entry fallback data."""

    groups = attr(manifest, "archive_groups", "groups", "grouped_archive_entries")
    if groups is not None:
        return [
            archive_group_payload(group, manifest, cleanup_status=cleanup_status)
            for group in object_list(groups)
        ]
    grouped_entries: dict[str, list[object]] = {}
    for entry in cast(tuple[object, ...], attr(manifest, "entries") or ()):
        grouped_entries.setdefault(entry_destination_archive_key(entry), []).append(entry)
    return [
        archive_group_payload_from_entries(
            destination_key,
            entries,
            manifest,
            cleanup_status=cleanup_status,
        )
        for destination_key, entries in sorted(grouped_entries.items())
    ]


def archive_group_payload(
    group: object,
    manifest: object,
    *,
    cleanup_status: str | None = None,
) -> dict[str, JsonValue]:
    """Return one archive group payload."""

    entries = object_list(attr(group, "entries", "source_entries", "objects"))
    skipped = object_list(attr(group, "skipped_objects", "skipped_entries"))
    target_day = attr(group, "target_day", "target_date") or manifest_target_day(manifest)
    payload: dict[str, JsonValue] = {
        "target_day": date_text(target_day),
        "archive_root": string_or_none(attr(group, "archive_root", "root")),
        "destination_archive_key": group_destination_archive_key(group, entries),
        "source_object_count": count_from_attr(group, "source_object_count", entries),
        "skipped_object_count": count_from_attr(group, "skipped_object_count", skipped),
        "source_objects": json_list([entry_reference_payload(entry) for entry in entries]),
    }
    if cleanup_status is not None:
        payload["cleanup_status"] = string_or_none(attr(group, "cleanup_status")) or cleanup_status
    if skipped:
        payload["skipped_objects"] = json_list([skipped_object_payload(item) for item in skipped])
    return payload


def archive_group_payload_from_entries(
    destination_key: str,
    entries: list[object],
    manifest: object,
    *,
    cleanup_status: str | None = None,
) -> dict[str, JsonValue]:
    """Build a group payload from legacy flat manifest entries."""

    payload: dict[str, JsonValue] = {
        "target_day": manifest_target_day(manifest),
        "archive_root": string_or_none(attr(entries[0], "archive_root")) if entries else None,
        "destination_archive_key": destination_key,
        "source_object_count": len(entries),
        "skipped_object_count": 0,
        "source_objects": json_list([entry_reference_payload(entry) for entry in entries]),
    }
    if cleanup_status is not None:
        payload["cleanup_status"] = cleanup_status
    return payload


def skipped_object_payloads(manifest: object) -> list[dict[str, JsonValue]]:
    """Return skipped source object payloads."""

    skipped = attr(manifest, "skipped_objects", "skipped_entries", "skipped")
    if skipped is None:
        return []
    return [skipped_object_payload(item) for item in object_list(skipped)]


def skipped_object_payload(item: object) -> dict[str, JsonValue]:
    """Return one skipped object payload."""

    return {
        "key": string_or_none(attr(item, "key", "source_key")),
        "reason": string_or_none(attr(item, "reason", "skip_reason")),
        "target_day": string_or_none(attr(item, "target_day", "target_date")),
        "archive_root": string_or_none(attr(item, "archive_root", "root")),
    }


def entry_reference_payload(entry: object) -> dict[str, JsonValue]:
    """Return a compact source entry reference."""

    return {
        "key": string_or_none(attr(entry, "key", "source_key")),
        "version_id": string_or_none(attr(entry, "version_id")),
        "size": int_or_none(attr(entry, "size")),
        "destination_archive_key": entry_destination_archive_key(entry),
    }


def destination_archive_keys(groups: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Return destination archive keys from group payloads."""

    return [group["destination_archive_key"] for group in groups]


def group_destination_archive_key(group: object, entries: list[object]) -> str:
    """Return a group's destination archive key."""

    value = attr(group, "destination_archive_key", "archive_key", "key")
    if value is not None:
        return str(value)
    keys = attr(group, "destination_archive_keys", "archive_keys")
    if keys is not None:
        first = next(iter(object_list(keys)), None)
        if first is not None:
            return str(first)
    return entry_destination_archive_key(entries[0]) if entries else ""


def entry_destination_archive_key(entry: object) -> str:
    """Return the destination archive key for a source entry."""

    value = attr(entry, "destination_archive_key", "archive_key")
    return str(value if value is not None else attr(entry, "key", "source_key") or "")


def json_list(items: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Cast dictionaries into JSON-value lists for strict type checking."""

    return [cast(JsonValue, item) for item in items]


def attr(source: object, *names: str) -> object | None:
    """Read the first available attribute name from an object."""

    for name in names:
        if hasattr(source, name):
            return cast(object, getattr(source, name))
    return None


def object_list(value: object | None) -> list[object]:
    """Return iterable object values as a list, excluding strings."""

    if value is None or isinstance(value, str):
        return []
    if isinstance(value, Iterable):
        return list(value)
    return []


def count_from_attr(source: object, name: str, fallback_items: list[object]) -> int:
    """Return an integer count attribute or the fallback item count."""

    value = attr(source, name)
    return value if isinstance(value, int) else len(fallback_items)


def date_text(value: object) -> str:
    """Render dates and datetimes as ISO date strings."""

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def string_or_none(value: object) -> str | None:
    """Return a string value unless the input is None."""

    return None if value is None else str(value)


def int_or_none(value: object) -> int | None:
    """Return an integer value unless the input is not an integer."""

    return value if isinstance(value, int) else None
