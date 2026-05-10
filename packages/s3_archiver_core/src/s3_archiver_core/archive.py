from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import uuid4

from s3_archiver_core._archive_protocols import ArchiveBucket, ArchiveRunLock
from s3_archiver_core.archive_group_metadata import (
    ARCHIVE_SHA256_METADATA_KEY,
    MANIFEST_SHA256_METADATA_KEY,
    existing_archive_verified,
    group_metadata,
    uploaded_archive_verified,
)
from s3_archiver_core.archive_manifest import (
    ArchiveGroup,
    ArchiveManifest,
    ManifestEntry,
    build_archive_manifest,
)
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.archive_result import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.archive_tar import sha256_file, write_tar_gz_archive
from s3_archiver_core.archive_workers import run_archive_group_workers, run_archive_workers
from s3_archiver_core.temp_files import TRANSFER_TEMP_PREFIX

__all__ = (
    "ARCHIVE_SHA256_METADATA_KEY",
    "MANIFEST_SHA256_METADATA_KEY",
    "ArchivePhaseResult",
    "ArchiveRunResult",
    "group_metadata",
    "run_archive",
)


DebugLogger = Callable[[ManifestEntry, str], None]


def run_archive(
    source: ArchiveBucket,
    destination: ArchiveBucket,
    options: ArchiveOptions,
    *,
    run_started_at_utc: datetime | None = None,
    run_lock: ArchiveRunLock | None = None,
    debug_logger: DebugLogger | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ArchiveRunResult:
    """Run one archive pass from source objects into daily destination archives."""

    now = clock or (lambda: datetime.now(tz=UTC))
    started = _as_utc(run_started_at_utc or now())
    deadline = started + options.run_timeout
    run_id = uuid4().hex
    if run_lock is not None and not run_lock.acquire(
        run_id=run_id, run_started_at_utc=started, timeout=options.run_timeout
    ):
        raise RuntimeError("archive run lock is already held")
    try:
        try:
            manifest = build_archive_manifest(
                source,
                run_started_at_utc=started,
                retention_days=options.retention_days,
                versioning_state=source.versioning_state(),
                source_filter=options.source_filter,
            )
        except Exception as exc:
            return ArchiveRunResult(
                run_id,
                _empty_manifest(started, options),
                _skipped("copy"),
                _skipped("verify"),
                _skipped("cleanup"),
                ArchivePhaseResult("list", (str(exc),)),
            )
        if _timed_out(now, deadline):
            return _run_result(
                run_id, manifest, _timeout("copy"), _skipped("verify"), _skipped("cleanup")
            )

        def timed_out() -> bool:
            return _timed_out(now, deadline)

        def time_remaining() -> float:
            return max((deadline - now()).total_seconds(), 0.0)

        copy_result, verified_groups, skipped_groups = _copy_phase(
            source,
            destination,
            manifest.archive_groups,
            options.max_workers,
            debug_logger,
            timed_out,
            time_remaining,
        )
        if _timed_out(now, deadline):
            return _run_result(
                run_id, manifest, _timeout("copy"), _skipped("verify"), _skipped("cleanup")
            )
        verify_result = (
            _skipped("verify")
            if not copy_result.ok
            else _verify_phase(
                destination,
                verified_groups,
                options.max_workers,
                timed_out,
                time_remaining,
            )
        )
        if copy_result.ok and _timed_out(now, deadline):
            return _run_result(
                run_id, manifest, copy_result, _timeout("verify"), _skipped("cleanup")
            )
        cleanup_result = (
            _cleanup_phase(source, options, verified_groups, timed_out, time_remaining)
            if copy_result.ok and verify_result.ok
            else _skipped("cleanup")
        )
        if copy_result.ok and verify_result.ok and _timed_out(now, deadline):
            cleanup_result = _timeout("cleanup")
        return ArchiveRunResult(
            run_id,
            manifest,
            copy_result,
            verify_result,
            cleanup_result,
            verified_archive_keys=tuple(group.destination_archive_key for group in verified_groups),
            skipped_archive_keys=tuple(group.destination_archive_key for group in skipped_groups),
        )
    finally:
        if run_lock is not None:
            run_lock.release(run_id=run_id)


def _run_result(
    run_id: str,
    manifest: ArchiveManifest,
    copy: ArchivePhaseResult,
    verify: ArchivePhaseResult,
    cleanup: ArchivePhaseResult,
) -> ArchiveRunResult:
    return ArchiveRunResult(run_id, manifest, copy, verify, cleanup)


def _copy_phase(
    source: ArchiveBucket,
    destination: ArchiveBucket,
    groups: tuple[ArchiveGroup, ...],
    max_workers: int,
    debug_logger: DebugLogger | None,
    timed_out: Callable[[], bool],
    time_remaining: Callable[[], float],
) -> tuple[ArchivePhaseResult, tuple[ArchiveGroup, ...], tuple[ArchiveGroup, ...]]:
    verified: dict[str, ArchiveGroup] = {}
    skipped: dict[str, ArchiveGroup] = {}
    group_lock = Lock()

    def worker(group: ArchiveGroup) -> str | None:
        failure, verified_group = _copy_group(source, destination, group, debug_logger)
        with group_lock:
            if verified_group:
                verified[group.destination_archive_key] = group
            elif failure is None:
                skipped[group.destination_archive_key] = group
        return failure

    phase = ArchivePhaseResult(
        "copy",
        run_archive_group_workers(groups, max_workers, worker, timed_out, time_remaining),
    )
    with group_lock:
        verified_keys = frozenset(verified)
        skipped_keys = frozenset(skipped)
    verified_groups = (group for group in groups if group.destination_archive_key in verified_keys)
    skipped_groups = (group for group in groups if group.destination_archive_key in skipped_keys)
    return phase, tuple(verified_groups), tuple(skipped_groups)


def _copy_group(
    source: ArchiveBucket,
    destination: ArchiveBucket,
    group: ArchiveGroup,
    debug_logger: DebugLogger | None,
) -> tuple[str | None, bool]:
    destination_key = group.destination_archive_key
    metadata = group_metadata(group)
    existing = destination.head_object(destination_key)
    if existing is not None:
        return None, existing_archive_verified(
            destination, destination_key, existing.metadata, metadata
        )
    archive_path: Path | None = None
    try:
        for entry in group.entries:
            if debug_logger is not None:
                debug_logger(entry, "deterministic_tar_gzip")
        destination.temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=destination.temp_dir, prefix=TRANSFER_TEMP_PREFIX
        ) as archive_file:
            archive_path = Path(archive_file.name)
        write_tar_gz_archive(source, group, archive_path)
        upload_metadata = dict(metadata)
        upload_metadata[ARCHIVE_SHA256_METADATA_KEY] = sha256_file(archive_path)
        destination.upload_archive_file(
            destination_key,
            archive_path,
            upload_metadata,
        )
    except Exception as exc:
        return f"{destination_key}: {exc}", False
    finally:
        if archive_path is not None:  # pragma: no branch
            archive_path.unlink(missing_ok=True)
    verified = destination.head_object(destination_key)
    if verified is None:
        return f"{destination_key}: destination missing", False
    if uploaded_archive_verified(destination, destination_key, verified.metadata, upload_metadata):
        return None, True
    return f"{destination_key}: archive verification failed", False


def _verify_phase(
    destination: ArchiveBucket,
    groups: tuple[ArchiveGroup, ...],
    max_workers: int,
    timed_out: Callable[[], bool],
    time_remaining: Callable[[], float],
) -> ArchivePhaseResult:
    def worker(group: ArchiveGroup) -> str | None:
        metadata = group_metadata(group)
        existing = destination.head_object(group.destination_archive_key)
        if existing is None:
            return f"{group.destination_archive_key}: destination missing"
        if not existing_archive_verified(
            destination, group.destination_archive_key, existing.metadata, metadata
        ):
            return f"{group.destination_archive_key}: archive verification failed"
        return None

    return ArchivePhaseResult(
        "verify",
        run_archive_group_workers(groups, max_workers, worker, timed_out, time_remaining),
    )


def _cleanup_phase(
    source: ArchiveBucket,
    options: ArchiveOptions,
    verified_groups: tuple[ArchiveGroup, ...],
    timed_out: Callable[[], bool],
    time_remaining: Callable[[], float],
) -> ArchivePhaseResult:
    if not options.cleanup_enabled:
        return _skipped("cleanup")
    entries = tuple(entry for group in verified_groups for entry in group.entries)

    def worker(entry: ManifestEntry) -> str | None:
        source.delete_source(entry.key, entry.version_id)
        return None

    return ArchivePhaseResult(
        "cleanup",
        run_archive_workers(entries, options.max_workers, worker, timed_out, time_remaining),
    )


def _skipped(phase: str) -> ArchivePhaseResult:
    return ArchivePhaseResult(phase, skipped=True)


def _timed_out(clock: Callable[[], datetime], deadline: datetime) -> bool:
    return clock() > deadline


def _timeout(phase: str) -> ArchivePhaseResult:
    return ArchivePhaseResult(phase, ("archive run timed out",))


def _empty_manifest(started: datetime, options: ArchiveOptions) -> ArchiveManifest:
    target_day = started.date() - timedelta(days=options.retention_days)
    cutoff = datetime.combine(target_day, datetime.min.time(), UTC)
    return ArchiveManifest(started, cutoff, (), target_day, (), ())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
