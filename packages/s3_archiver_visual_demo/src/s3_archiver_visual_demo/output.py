"""Human-readable visual demo emitters."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from s3_archiver_core.archive_manifest import ArchiveManifest
from s3_archiver_core.archive_payloads import (
    archive_group_payloads,
    direct_entry_payloads,
    manifest_target_day,
    skipped_object_payloads,
)
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.route_payloads import route_payloads, route_summary_payload
from s3_archiver_core.settings import AppSettings

type Emitter = Callable[[str], None]


def emit_intro(
    emit: Emitter,
    settings: AppSettings,
    log_file: Path,
    started: datetime,
    *,
    title: str = "== S3 Archiver Visual Demo ==",
) -> None:
    """Emit the visual demo heading and runtime context."""

    route_summary = route_summary_payload(settings)
    emit(title)
    emit_working_set(emit, settings)
    emit(_bucket_summary("source", route_summary))
    emit(_bucket_summary("destination", route_summary))
    emit(f"log file: {log_file}")
    emit(f"run started at utc: {started.isoformat()}")


def emit_working_set(emit: Emitter, settings: AppSettings) -> None:
    """Emit the redacted route working set for this invocation."""

    routes = route_payloads(settings)
    emit("== Working Set ==")
    emit(f"route count: {len(routes)}")
    for route in routes:
        emit(
            "ROUTE  "
            + f"name={route['name']} "
            + f"parser={route['parser_kind']} "
            + f"copy_mode={route['copy_mode']} "
            + f"source_bucket={route['source_bucket']} "
            + f"source_path={_path_text(route['source_path'])} "
            + f"destination_bucket={route['destination_bucket']} "
            + f"destination_path={_path_text(route['destination_path'])}"
        )


def emit_health(emit: Emitter, health: dict[str, JsonValue]) -> None:
    """Emit preflight health-check output."""

    emit("")
    emit("== Preflight ==")
    emit(f"status: {health['status']}")
    emit(f"checked_at: {health['checked_at']}")


def emit_snapshot(emit: Emitter, title: str, snapshot: dict[str, JsonValue]) -> None:
    """Emit source and destination bucket snapshot output."""

    emit("")
    emit(f"== {title} ==")
    emit(
        f"source objects: {snapshot['source_object_count']} "
        + f"(versioning={snapshot['source_versioning_state']})"
    )
    for row in cast(list[dict[str, JsonValue]], snapshot["source_objects"]):
        emit(
            "SOURCE "
            + f"key={row['key']} "
            + f"size={row['size']} "
            + f"last_modified={row['last_modified_utc']} "
            + f"eligible={row['eligible_for_follow_up']} "
            + f"present_in_destination={row['present_in_destination']}"
        )
    emit(
        f"destination objects: {snapshot['destination_object_count']} "
        + f"(versioning={snapshot['destination_versioning_state']})"
    )
    for row in cast(list[dict[str, JsonValue]], snapshot["destination_objects"]):
        source_modified = row["source_last_modified"]
        source_detail = f" source_last_modified={source_modified}" if source_modified else ""
        emit(
            f"DEST   key={row['key']} size={row['size']} "
            + f"last_modified={row['last_modified_utc']}{source_detail}"
        )


def emit_manifest(emit: Emitter, manifest: ArchiveManifest) -> None:
    """Emit manifest candidate and archive-group output."""

    emit("")
    emit("== Archive Candidates ==")
    emit(f"target day: {manifest_target_day(manifest)}")
    groups = archive_group_payloads(manifest)
    direct_entries = direct_entry_payloads(manifest)
    skipped = skipped_object_payloads(manifest)
    _emit_archive_coverage(emit, groups)
    emit(f"archive group count: {len(groups)}")
    emit(f"direct copy count: {len(direct_entries)}")
    emit(f"source object count: {len(manifest.entries)}")
    emit(f"skipped object count: {len(skipped)}")
    for group in groups:
        emit(
            "GROUP  "
            + _route_fields(group)
            + f"target_day={group['target_day']} "
            + f"archive_root={group['archive_root']} "
            + f"destination_archive_key={group['destination_archive_key']} "
            + f"source_object_count={group['source_object_count']} "
            + f"skipped_object_count={group['skipped_object_count']}"
        )
    for entry in direct_entries:
        emit(
            "DIRECT "
            + _route_fields(entry)
            + f"destination_key={entry['destination_key']} "
            + f"source_object_count={entry['source_object_count']}"
        )
    for item in skipped:
        emit(
            "SKIP   "
            + f"key={item['key']} "
            + f"reason={item['reason']} "
            + f"target_day={item['target_day']} "
            + f"archive_root={item['archive_root']}"
        )
    for entry in manifest.entries:
        emit(
            "SOURCE "
            + f"key={entry.key} "
            + f"size={entry.size} "
            + f"route={getattr(entry, 'route_name', None)} "
            + f"parser={getattr(entry, 'parser_kind', None)} "
            + f"copy_mode={getattr(entry, 'copy_mode', None)} "
            + f"last_modified={entry.last_modified.isoformat()} "
            + f"version_id={entry.version_id}"
        )


def emit_archive_result(emit: Emitter, payload: dict[str, JsonValue]) -> None:
    """Emit archive result output."""

    emit("")
    emit("== Archive Result ==")
    emit(f"status: {payload['status']}")
    emit(f"target day: {payload.get('target_day')}")
    groups = cast(list[dict[str, JsonValue]], payload.get("archive_groups", []))
    direct_entries = cast(list[dict[str, JsonValue]], payload.get("direct_entries", []))
    _emit_archive_coverage(emit, groups, days=_archive_days_from_payload(payload))
    emit(f"archive count: {payload.get('archive_count')}")
    emit(f"direct copy count: {payload.get('direct_copy_count', len(direct_entries))}")
    for group in groups:
        emit(
            "GROUP  "
            + _route_fields(group)
            + f"destination_archive_key={group['destination_archive_key']} "
            + f"source_object_count={group['source_object_count']} "
            + f"skipped_object_count={group['skipped_object_count']}"
        )
    for entry in direct_entries:
        emit(
            "DIRECT "
            + _route_fields(entry)
            + f"destination_key={entry['destination_key']} "
            + f"source_object_count={entry['source_object_count']}"
        )
    phases = cast(dict[str, dict[str, JsonValue]], payload["phases"])
    for phase_name in ("list", "copy", "verify"):
        phase = phases[phase_name]
        emit(f"{phase_name}: status={phase['status']} failure_count={phase['failure_count']}")


def _emit_archive_coverage(
    emit: Emitter,
    groups: list[dict[str, JsonValue]],
    *,
    days: list[str] | None = None,
) -> None:
    archive_days = _archive_days(groups) if days is None else days
    root_count = len({str(group["archive_root"]) for group in groups if group.get("archive_root")})
    files_per_archive = [
        int(count) for group in groups if isinstance(count := group.get("source_object_count"), int)
    ]
    archives_per_day = [
        sum(1 for group in groups if str(group.get("target_day")) == day) for day in archive_days
    ]
    emit(f"archive day count: {len(archive_days)}")
    if archive_days:
        emit(f"archive day range: {archive_days[0]} through {archive_days[-1]}")
        emit(f"archive days sample: {', '.join(_sample_text(archive_days))}")
    emit(f"archive root count: {root_count}")
    if archives_per_day:
        emit("archives per day: " + f"min={min(archives_per_day)} max={max(archives_per_day)}")
    if files_per_archive:
        emit(
            "source objects per archive: "
            + f"min={min(files_per_archive)} max={max(files_per_archive)}"
        )


def _route_fields(row: dict[str, JsonValue]) -> str:
    values = (row.get("route_name"), row.get("parser_kind"), row.get("copy_mode"))
    if all(value is None for value in values):
        return ""
    route, parser, copy_mode = values
    return f"route={route} parser={parser} copy_mode={copy_mode} "


def _bucket_summary(side: str, route_summary: dict[str, JsonValue]) -> str:
    singular = route_summary.get(f"{side}_bucket")
    if isinstance(singular, str):
        return f"{side} bucket: {singular}"
    buckets = route_summary.get(f"{side}_buckets")
    return f"{side} buckets: {', '.join(str(bucket) for bucket in cast(list[JsonValue], buckets))}"


def _path_text(value: JsonValue) -> str:
    text = str(value)
    return text if text else "(root)"


def _archive_days(groups: list[dict[str, JsonValue]]) -> list[str]:
    return sorted({str(group["target_day"]) for group in groups if group.get("target_day")})


def _archive_days_from_payload(payload: dict[str, JsonValue]) -> list[str]:
    days = payload.get("archive_days")
    if isinstance(days, list):
        return [str(day) for day in days]
    groups = cast(list[dict[str, JsonValue]], payload.get("archive_groups", []))
    return _archive_days(groups)


def _sample_text(values: list[str]) -> list[str]:
    if len(values) <= 6:
        return values
    return [*values[:3], "...", *values[-3:]]
