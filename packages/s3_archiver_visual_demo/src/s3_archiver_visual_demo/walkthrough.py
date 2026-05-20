"""Human-readable archive demo output backed by real S3 state."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from s3_archiver_core.archive_manifest import build_route_archive_manifest
from s3_archiver_core.archive_payloads import (
    archive_manifest_payload,
)
from s3_archiver_core.archive_routes import archive_routes_from_settings
from s3_archiver_core.health import run_health_check
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.route_payloads import route_summary_payload
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings
from s3_archiver_core.temp_files import prepare_runtime_temp_dir

from s3_archiver_visual_demo import output as _output
from s3_archiver_visual_demo.snapshots import (
    manifest_destination_key_map as _manifest_destination_key_map,
)
from s3_archiver_visual_demo.snapshots import manifest_key_set as _manifest_key_set
from s3_archiver_visual_demo.snapshots import snapshot_payload as _snapshot_payload

type ArchiveRunner = Callable[[AppSettings, Path], dict[str, JsonValue]]
type Emitter = Callable[[str], None]


def run_visual_demo(
    settings: AppSettings,
    log_file: Path,
    *,
    archive_runner: ArchiveRunner,
    emit: Emitter,
    now: Callable[[], datetime] | None = None,
) -> dict[str, JsonValue]:
    """Run a human-readable archive walkthrough against the configured buckets."""

    clock = _utc_now if now is None else now
    started = clock()
    _output.emit_intro(emit, settings, log_file, started)
    prepare_runtime_temp_dir(settings.temp_dir)
    health = cast(dict[str, JsonValue], run_health_check(settings, log_file).as_dict())
    routes = archive_routes_from_settings(settings, build_s3_client)
    manifest = build_route_archive_manifest(routes, run_started_at_utc=started)
    eligible_keys = _manifest_key_set(manifest)
    planned_destinations = _manifest_destination_key_map(manifest)
    before_snapshot = _snapshot_payload(
        routes,
        eligible_keys=eligible_keys,
        planned_destinations=planned_destinations,
    )
    _output.emit_health(emit, health)
    _output.emit_snapshot(emit, "Before archive", before_snapshot)
    _output.emit_manifest(emit, manifest)
    emit("Running archive workflow against the configured buckets...")
    archive_payload = archive_runner(settings, log_file)
    _output.emit_archive_result(emit, archive_payload)
    after_archive_snapshot = _snapshot_payload(
        routes,
        eligible_keys=set(),
        planned_destinations=planned_destinations,
    )
    _output.emit_snapshot(emit, "After archive", after_archive_snapshot)
    archive_manifest = archive_manifest_payload(
        manifest,
        include_archive_days=True,
        include_entries=True,
    )

    snapshots: dict[str, JsonValue] = {
        "before_archive": before_snapshot,
        "after_archive": after_archive_snapshot,
    }
    summary: dict[str, JsonValue] = {
        "status": "ok" if archive_payload.get("status") == "ok" else "error",
        **route_summary_payload(settings),
        "log_file": str(log_file),
        "run_started_at_utc": started.isoformat(),
        "health": health,
        "archive_manifest": archive_manifest,
        "archive_result": archive_payload,
        "snapshots": snapshots,
    }
    emit("Demo summary JSON follows on the next line.")
    emit(json.dumps(summary, sort_keys=True))
    return summary


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
