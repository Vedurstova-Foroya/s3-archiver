from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from s3_archiver_core._archive_copy import copy_phase as _copy_phase_impl
from s3_archiver_core._archive_copy import verify_phase as _verify_phase_impl
from s3_archiver_core._archive_protocols import ArchiveRunLock
from s3_archiver_core._archive_size_limits import log_skipped_summary
from s3_archiver_core.archive_group_metadata import (
    ARCHIVE_SHA256_METADATA_KEY,
    MANIFEST_SHA256_METADATA_KEY,
    group_metadata,
)
from s3_archiver_core.archive_manifest import (
    ArchiveManifest,
    build_route_archive_manifest,
)
from s3_archiver_core.archive_progress import ProgressLogger
from s3_archiver_core.archive_result import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.archive_routes import ArchiveRoute, DebugLogger
from s3_archiver_core.temp_files import log_temp_storage

__all__ = (
    "ARCHIVE_SHA256_METADATA_KEY",
    "MANIFEST_SHA256_METADATA_KEY",
    "ArchivePhaseResult",
    "ArchiveRoute",
    "ArchiveRunResult",
    "group_metadata",
    "run_archive",
)


def run_archive(
    routes: tuple[ArchiveRoute, ...],
    *,
    run_timeout: timedelta,
    run_started_at_utc: datetime | None = None,
    run_lock: ArchiveRunLock | None = None,
    debug_logger: DebugLogger | None = None,
    progress_logger: ProgressLogger | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ArchiveRunResult:
    """Run one archive pass with one execution worker per route."""

    if not routes:
        raise ValueError("archive run requires at least one route")
    now = clock or (lambda: datetime.now(tz=UTC))
    started = _as_utc(run_started_at_utc or now())
    deadline = started + run_timeout
    run_id = uuid4().hex
    if run_lock is not None and not run_lock.acquire(
        run_id=run_id, run_started_at_utc=started, timeout=run_timeout
    ):
        raise RuntimeError("archive run lock is already held")
    try:
        _log_route_temp_storage(routes)
        try:
            manifest = _build_manifest(routes, started, progress_logger)
        except Exception as exc:
            return _finalize_result(
                ArchiveRunResult(
                    run_id,
                    _empty_manifest(started),
                    _skipped("copy"),
                    _skipped("verify"),
                    ArchivePhaseResult("list", (str(exc),)),
                )
            )
        if _timed_out(now, deadline):
            return _finalize_result(
                _run_result(run_id, manifest, _timeout("copy"), _skipped("verify"))
            )

        def timed_out() -> bool:
            return _timed_out(now, deadline)

        def time_remaining() -> float:
            return max((deadline - now()).total_seconds(), 0.0)

        routes_by_name = {route.name: route for route in routes}
        copy_result, verified_groups, verified_entries = _copy_phase_impl(
            manifest,
            routes_by_name,
            debug_logger,
            timed_out,
            time_remaining,
            progress_logger,
            collect_verified=False,
        )
        if _timed_out(now, deadline):
            return _finalize_result(
                _run_result(
                    run_id,
                    manifest,
                    copy_result if not copy_result.ok else _timeout("copy"),
                    _skipped("verify"),
                )
            )
        verify_result = (
            _skipped("verify")
            if not copy_result.ok
            else _verify_phase_impl(
                verified_groups,
                verified_entries,
                routes_by_name,
                timed_out,
                time_remaining,
                progress_logger,
            )
        )
        if copy_result.ok and _timed_out(now, deadline):
            return _finalize_result(
                _run_result(
                    run_id,
                    manifest,
                    copy_result,
                    verify_result if not verify_result.ok else _timeout("verify"),
                )
            )
        return _finalize_result(
            ArchiveRunResult(
                run_id,
                manifest,
                copy_result,
                verify_result,
            )
        )
    finally:
        if run_lock is not None:
            run_lock.release(run_id=run_id)


def _run_result(
    run_id: str,
    manifest: ArchiveManifest,
    copy: ArchivePhaseResult,
    verify: ArchivePhaseResult,
) -> ArchiveRunResult:
    return ArchiveRunResult(run_id, manifest, copy, verify)


def _finalize_result(result: ArchiveRunResult) -> ArchiveRunResult:
    log_skipped_summary(result.manifest.skipped_objects)
    return result


def _build_manifest(
    routes: tuple[ArchiveRoute, ...],
    started: datetime,
    progress_logger: ProgressLogger | None,
) -> ArchiveManifest:
    return build_route_archive_manifest(
        routes,
        run_started_at_utc=started,
        progress_logger=progress_logger,
    )


def _skipped(phase: str) -> ArchivePhaseResult:
    return ArchivePhaseResult(phase, skipped=True)


def _timed_out(clock: Callable[[], datetime], deadline: datetime) -> bool:
    return clock() > deadline


def _timeout(phase: str) -> ArchivePhaseResult:
    return ArchivePhaseResult(phase, ("archive run timed out",))


def _empty_manifest(started: datetime) -> ArchiveManifest:
    return ArchiveManifest(started, (), None, (), ())


def _log_route_temp_storage(routes: tuple[ArchiveRoute, ...]) -> None:
    temp_dirs = {route.source.temp_dir for route in routes} | {
        route.destination.temp_dir for route in routes
    }
    for temp_dir in sorted(temp_dirs, key=str):
        _ = log_temp_storage(temp_dir)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
