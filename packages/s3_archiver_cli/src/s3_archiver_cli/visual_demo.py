"""Human-readable archive demo output backed by real S3 state."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from s3_archiver_core.archive_fingerprint import fingerprint_from_metadata
from s3_archiver_core.archive_manifest import ArchiveManifest, ManifestEntry, build_archive_manifest
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.health import run_health_check
from s3_archiver_core.s3 import S3ListedObject, build_s3_client
from s3_archiver_core.settings import AppSettings
from s3_archiver_core.temp_files import prepare_runtime_temp_dir

from s3_archiver_cli.archive_payloads import (
    archive_group_payloads,
    json_list,
    manifest_target_day,
    skipped_object_payloads,
)
from s3_archiver_cli.visual_demo_output import emit_archive_result as _emit_archive_result
from s3_archiver_cli.visual_demo_output import emit_cleanup_preview as _emit_cleanup_preview
from s3_archiver_cli.visual_demo_output import emit_health as _emit_health
from s3_archiver_cli.visual_demo_output import emit_intro as _emit_intro
from s3_archiver_cli.visual_demo_output import emit_manifest as _emit_manifest
from s3_archiver_cli.visual_demo_output import emit_snapshot as _emit_snapshot

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
type ArchiveRunner = Callable[[AppSettings, Path], dict[str, JsonValue]]
type CleanupPreviewRunner = Callable[[AppSettings, Path], dict[str, JsonValue]]
type Emitter = Callable[[str], None]


def run_visual_demo(
    settings: AppSettings,
    log_file: Path,
    *,
    archive_runner: ArchiveRunner,
    cleanup_preview_runner: CleanupPreviewRunner,
    emit: Emitter,
    now: Callable[[], datetime] | None = None,
    perform_cleanup: bool = False,
) -> dict[str, JsonValue]:
    """Run a human-readable archive walkthrough against the configured buckets."""

    clock = _utc_now if now is None else now
    started = clock()
    settings = replace(settings, cleanup_enabled=perform_cleanup)
    prepare_runtime_temp_dir(settings.temp_dir)
    health = cast(dict[str, JsonValue], run_health_check(settings, log_file).as_dict())
    source = S3ArchiveBucket(
        build_s3_client(settings.source),
        settings.source.bucket,
        settings.temp_dir,
    )
    destination = S3ArchiveBucket(
        build_s3_client(settings.destination),
        settings.destination.bucket,
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
    eligible_keys = _manifest_key_set(manifest)
    before_snapshot = _snapshot_payload(source, destination, eligible_keys=eligible_keys)

    if perform_cleanup:
        _emit_intro(
            emit, settings, log_file, started, title="== S3 Archiver Cleanup Visual Demo =="
        )
    else:
        _emit_intro(emit, settings, log_file, started)
    _emit_health(emit, health)
    _emit_snapshot(emit, "Before archive", before_snapshot)
    _emit_manifest(emit, manifest)
    if perform_cleanup:
        emit("Running archive workflow with cleanup enabled against the configured buckets...")
    else:
        emit("Running archive workflow against the configured buckets...")
    archive_payload = archive_runner(settings, log_file)
    _emit_archive_result(emit, archive_payload)
    after_archive_snapshot = _snapshot_payload(source, destination, eligible_keys=set())
    cleanup_deleted_count = (
        _snapshot_source_object_count(before_snapshot)
        - _snapshot_source_object_count(after_archive_snapshot)
        if perform_cleanup
        else 0
    )
    if perform_cleanup:
        _emit_snapshot(emit, "After cleanup", after_archive_snapshot)
        emit(f"cleanup deleted source object count: {cleanup_deleted_count}")
    else:
        _emit_snapshot(emit, "After archive", after_archive_snapshot)

    cleanup_preview: dict[str, JsonValue] | None = None
    after_preview_snapshot: dict[str, JsonValue] | None = None
    if not perform_cleanup:
        emit("Running cleanup preview without deleting source objects...")
        cleanup_payload = cleanup_preview_runner(settings, log_file)
        cleanup_preview = cast(dict[str, JsonValue], cleanup_payload["cleanup_preview"])
        cleanup_keys = _payload_key_set(cleanup_preview)
        _emit_cleanup_preview(emit, cleanup_preview)
        after_preview_snapshot = _snapshot_payload(source, destination, eligible_keys=cleanup_keys)
        _emit_snapshot(emit, "After cleanup preview", after_preview_snapshot)
    archive_groups = archive_group_payloads(manifest)
    skipped_objects = skipped_object_payloads(manifest)
    archive_days = sorted({str(group["target_day"]) for group in archive_groups})
    archive_days_payload = [cast(JsonValue, day) for day in archive_days]
    archive_manifest: dict[str, JsonValue] = {
        "object_count": len(manifest.entries),
        "target_day": manifest_target_day(manifest),
        "archive_days": archive_days_payload,
        "archive_count": len(archive_groups),
        "source_object_count": len(manifest.entries),
        "skipped_object_count": len(skipped_objects),
        "destination_archive_keys": [group["destination_archive_key"] for group in archive_groups],
        "archive_groups": json_list(archive_groups),
        "skipped_objects": json_list(skipped_objects),
        "retention_cutoff_utc": manifest.retention_cutoff_utc.isoformat(),
        "entries": json_list([_manifest_entry_payload(entry) for entry in manifest.entries]),
    }

    snapshots: dict[str, JsonValue] = {
        "before_archive": before_snapshot,
        "after_archive": after_archive_snapshot,
    }
    if after_preview_snapshot is not None:
        snapshots["after_cleanup_preview"] = after_preview_snapshot
    else:
        snapshots["after_cleanup"] = after_archive_snapshot
    cleanup_preview_unchanged = after_preview_snapshot is not None and _snapshot_bucket_state(
        after_archive_snapshot
    ) == _snapshot_bucket_state(after_preview_snapshot)
    summary: dict[str, JsonValue] = {
        "status": "ok" if archive_payload.get("status") == "ok" else "error",
        "cleanup_mode": "cleanup" if perform_cleanup else "preview",
        "cleanup_performed": perform_cleanup,
        "source_bucket": settings.source.bucket,
        "destination_bucket": settings.destination.bucket,
        "log_file": str(log_file),
        "cleanup_enabled_in_settings": settings.cleanup_enabled,
        "run_started_at_utc": started.isoformat(),
        "health": health,
        "archive_manifest": archive_manifest,
        "archive_result": archive_payload,
        "cleanup_preview": cleanup_preview,
        "snapshots": snapshots,
        "cleanup_preview_left_bucket_state_unchanged": cleanup_preview_unchanged,
        "cleanup_deleted_source_object_count": cleanup_deleted_count,
    }
    emit("Demo summary JSON follows on the next line.")
    emit(json.dumps(summary, sort_keys=True))
    return summary


def _snapshot_payload(
    source: S3ArchiveBucket,
    destination: S3ArchiveBucket,
    *,
    eligible_keys: set[tuple[str, str | None]],
) -> dict[str, JsonValue]:
    source_state = source.versioning_state()
    destination_state = destination.versioning_state()
    source_objects = sorted(source.list_source_objects(source_state), key=_object_sort_key)
    destination_objects = sorted(
        destination.list_source_objects(destination_state),
        key=_object_sort_key,
    )
    destination_keys = {(item.key, item.version_id) for item in destination_objects}
    return {
        "source_versioning_state": source_state,
        "destination_versioning_state": destination_state,
        "source_object_count": len(source_objects),
        "destination_object_count": len(destination_objects),
        "source_objects": [
            _listed_object_payload(
                item,
                eligible_for_follow_up=(item.key, item.version_id) in eligible_keys,
                present_in_destination=(item.key, item.version_id) in destination_keys
                or (item.key, None) in destination_keys,
            )
            for item in source_objects
        ],
        "destination_objects": [
            _listed_object_payload(
                item,
                eligible_for_follow_up=False,
                present_in_destination=True,
            )
            for item in destination_objects
        ],
    }


def _listed_object_payload(
    item: S3ListedObject,
    *,
    eligible_for_follow_up: bool,
    present_in_destination: bool,
) -> dict[str, JsonValue]:
    fingerprint = fingerprint_from_metadata(item.properties.metadata)
    return {
        "key": item.key,
        "size": item.size,
        "last_modified_utc": item.last_modified.isoformat(),
        "etag": item.etag,
        "version_id": item.version_id,
        "source_last_modified": None if fingerprint is None else fingerprint.source_last_modified,
        "eligible_for_follow_up": eligible_for_follow_up,
        "present_in_destination": present_in_destination,
    }


def _manifest_entry_payload(entry: ManifestEntry) -> dict[str, JsonValue]:
    return {
        "key": entry.key,
        "size": entry.size,
        "last_modified_utc": entry.last_modified.isoformat(),
        "version_id": entry.version_id,
        "etag": entry.etag,
        "source_bucket": entry.source_bucket,
    }


def _manifest_key_set(manifest: ArchiveManifest) -> set[tuple[str, str | None]]:
    return {(entry.key, entry.version_id) for entry in manifest.entries}


def _payload_key_set(cleanup_preview: dict[str, JsonValue]) -> set[tuple[str, str | None]]:
    entries = list(cast(list[dict[str, JsonValue]], cleanup_preview.get("entries", [])))
    for group in cast(list[dict[str, JsonValue]], cleanup_preview.get("archive_groups", [])):
        entries.extend(cast(list[dict[str, JsonValue]], group.get("source_objects", [])))
    return {
        (
            str(entry["key"]),
            None if entry["version_id"] is None else str(entry["version_id"]),
        )
        for entry in entries
    }


def _object_sort_key(item: S3ListedObject) -> tuple[str, str]:
    return (item.key, "" if item.version_id is None else item.version_id)


def _snapshot_bucket_state(snapshot: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "source_versioning_state": snapshot["source_versioning_state"],
        "destination_versioning_state": snapshot["destination_versioning_state"],
        "source_object_count": snapshot["source_object_count"],
        "destination_object_count": snapshot["destination_object_count"],
        "source_objects": [
            {key: value for key, value in row.items() if key != "eligible_for_follow_up"}
            for row in cast(list[dict[str, JsonValue]], snapshot["source_objects"])
        ],
        "destination_objects": snapshot["destination_objects"],
    }


def _snapshot_source_object_count(snapshot: dict[str, JsonValue]) -> int:
    count = snapshot["source_object_count"]
    return count if isinstance(count, int) else 0


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
