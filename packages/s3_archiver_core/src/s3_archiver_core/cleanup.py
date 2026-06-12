"""Source-object cleanup engine.

``run_cleanup`` consumes one or more cleanup-input manifests, deletes each
referenced source object at the exact archived version, and records every
verified deletion into a temporary cleaned manifest. When the cleaned manifest
matches the input exactly the input manifest is retired; otherwise it is kept so
a later run can retry the remainder.

The lock that guards archiving is owned by the caller, not this module, so a
cleanup run and an archive run can never overlap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from s3_archiver_core._archive_identity import stable_identity_value
from s3_archiver_core._cleanup_models import (
    CleanupManifestOutcome,
    CleanupManifestStatus,
    CleanupManifestSummary,
    CleanupRecord,
    CleanupResult,
)
from s3_archiver_core.archive_routes import ArchiveRoute
from s3_archiver_core.cleanup_manifest import (
    iter_cleanup_records,
    validate_cleanup_manifest,
    write_cleanup_manifest,
)
from s3_archiver_core.s3 import S3ObjectProperties
from s3_archiver_core.source_deletes import SourceDeleteFailure, SourceDeleteRequest

__all__ = ("CleanupResult", "run_cleanup")

_logger = logging.getLogger("s3_archiver.cleanup")
_DELETE_BATCH_SIZE = 1000


class _BatchDeleteSource(Protocol):
    def delete_source_objects(
        self, objects: Sequence[SourceDeleteRequest]
    ) -> Sequence[SourceDeleteFailure]:
        """Delete source objects as a batch."""
        ...


@dataclass
class _PendingDeleteBatch:
    route: ArchiveRoute
    records: list[CleanupRecord]


def run_cleanup(
    routes: tuple[ArchiveRoute, ...],
    *,
    manifests: Sequence[Path],
    cleaned_dir: Path,
    timed_out: Callable[[], bool],
) -> CleanupResult:
    """Delete and verify every source object referenced by the given manifests.

    Every manifest is validated up front; a single mangled manifest raises
    :class:`~s3_archiver_core.errors.CleanupManifestError` before any deletion
    occurs, so cleanup never runs against a manifest in a bad state.
    """

    routes_by_name = {route.name: route for route in routes}
    validated = [(path, validate_cleanup_manifest(path)) for path in manifests]
    pending = [
        (path, summary)
        for path, summary in validated
        if summary.status is CleanupManifestStatus.VALID
    ]
    if not pending:
        return CleanupResult((), empty=True)
    outcomes = tuple(
        _clean_manifest(path, summary, routes_by_name, cleaned_dir, timed_out)
        for path, summary in pending
    )
    return CleanupResult(outcomes, empty=False)


def _clean_manifest(
    path: Path,
    summary: CleanupManifestSummary,
    routes_by_name: dict[str, ArchiveRoute],
    cleaned_dir: Path,
    timed_out: Callable[[], bool],
) -> CleanupManifestOutcome:
    cleaned_path = cleaned_dir / path.name
    failures: list[str] = []
    cleaned = write_cleanup_manifest(
        cleaned_path,
        run_id=summary.run_id,
        run_started_at_utc=summary.run_started_at_utc,
        records=_verified_deletions(path, routes_by_name, failures, timed_out),
    )
    identical = cleaned.sha256 == summary.sha256 and cleaned.object_count == summary.object_count
    cleaned_path.unlink(missing_ok=True)
    if identical:
        path.unlink(missing_ok=True)
    return CleanupManifestOutcome(
        path=path,
        object_count=summary.object_count,
        cleaned_count=cleaned.object_count,
        failures=tuple(failures),
        removed=identical,
    )


def _verified_deletions(
    path: Path,
    routes_by_name: dict[str, ArchiveRoute],
    failures: list[str],
    timed_out: Callable[[], bool],
) -> Iterator[CleanupRecord]:
    batch: _PendingDeleteBatch | None = None
    for record in iter_cleanup_records(path):
        if timed_out():
            failures.append(f"{record.key}: cleanup run timed out")
            return
        route, failure = _route_for_record(record, routes_by_name)
        if failure is not None:
            yield from _flush_batch(batch, failures)
            batch = None
            failures.append(failure)
            continue
        assert route is not None
        if _batch_delete_supported(record, route):
            if batch is not None and batch.route is not route:
                yield from _flush_batch(batch, failures)
                batch = None
            if batch is None:
                batch = _PendingDeleteBatch(route, [])
            batch.records.append(record)
            if len(batch.records) >= _DELETE_BATCH_SIZE:
                yield from _flush_batch(batch, failures)
                batch = None
            continue
        yield from _flush_batch(batch, failures)
        batch = None
        failure = _delete_and_verify_on_route(record, route)
        if failure is not None:
            failures.append(failure)
            continue
        yield record
    yield from _flush_batch(batch, failures)


def _route_for_record(
    record: CleanupRecord, routes_by_name: dict[str, ArchiveRoute]
) -> tuple[ArchiveRoute | None, str | None]:
    route = routes_by_name.get(record.route_name)
    if route is None:
        return None, f"{record.key}: no configured route named {record.route_name!r}"
    if stable_identity_value(route.source_identity) != record.source_identity:
        return None, f"{record.key}: source identity mismatch for route {record.route_name!r}"
    if route.source.bucket != record.source_bucket:
        message = (
            f"{record.key}: source bucket mismatch for route {record.route_name!r}"
            + f" (manifest {record.source_bucket!r}, route {route.source.bucket!r})"
        )
        return None, message
    return route, None


def _delete_and_verify_on_route(record: CleanupRecord, route: ArchiveRoute) -> str | None:
    if record.version_id is None:
        failure, current_exists = _verify_unversioned_source_matches(record, route)
        if failure is not None:
            return failure
        if not current_exists:
            return None
    if_match = record.etag if record.version_id is None else None
    try:
        route.source.delete_source_object(record.key, record.version_id, if_match=if_match)
    except Exception as exc:
        return f"{record.key}: delete failed: {exc}"
    try:
        remaining = route.source.head_object(record.key, record.version_id)
    except Exception as exc:
        return f"{record.key}: delete verification failed: {exc}"
    if remaining is not None:
        return f"{record.key}: still present after delete"
    _log_deleted(record)
    return None


def _batch_delete_supported(record: CleanupRecord, route: ArchiveRoute) -> bool:
    return record.version_id is not None and _batch_delete_source(route) is not None


def _flush_batch(batch: _PendingDeleteBatch | None, failures: list[str]) -> Iterator[CleanupRecord]:
    if batch is None:
        return
    failed = _batch_delete_failures(batch.route, batch.records)
    for record in batch.records:
        failure = failed.get((record.key, record.version_id)) or failed.get((record.key, None))
        if failure is not None:
            failures.append(f"{record.key}: {failure.detail}")
            continue
        _log_deleted(record)
        yield record


def _batch_delete_failures(
    route: ArchiveRoute, records: Sequence[CleanupRecord]
) -> dict[tuple[str, str | None], SourceDeleteFailure]:
    source = _batch_delete_source(route)
    if source is None:
        return _serial_delete_failures(route, records)
    requests = tuple(SourceDeleteRequest(record.key, record.version_id) for record in records)
    try:
        failures = source.delete_source_objects(requests)
    except NotImplementedError:
        return _serial_delete_failures(route, records)
    except Exception as exc:
        failures = tuple(
            SourceDeleteFailure(record.key, record.version_id, f"delete failed: {exc}")
            for record in records
        )
    return {(failure.key, failure.version_id): failure for failure in failures}


def _serial_delete_failures(
    route: ArchiveRoute, records: Sequence[CleanupRecord]
) -> dict[tuple[str, str | None], SourceDeleteFailure]:
    failures: dict[tuple[str, str | None], SourceDeleteFailure] = {}
    for record in records:
        failure = _delete_and_verify_on_route(record, route)
        if failure is not None:
            detail = failure.removeprefix(f"{record.key}: ")
            failures[(record.key, record.version_id)] = SourceDeleteFailure(
                record.key, record.version_id, detail
            )
    return failures


def _batch_delete_source(route: ArchiveRoute) -> _BatchDeleteSource | None:
    candidate = cast(object, getattr(route.source, "delete_source_objects", None))
    if callable(candidate):
        return cast(_BatchDeleteSource, cast(object, route.source))
    return None


def _log_deleted(record: CleanupRecord) -> None:
    _logger.debug(
        "cleanup deleted source object",
        extra={
            "event": "cleanup.object.deleted",
            "key": record.key,
            "source_bucket": record.source_bucket,
            "version_id": record.version_id,
            "route_name": record.route_name,
        },
    )


def _verify_unversioned_source_matches(
    record: CleanupRecord, route: ArchiveRoute
) -> tuple[str | None, bool]:
    try:
        current = route.source.head_object(record.key)
    except Exception as exc:
        return f"{record.key}: source verification failed before delete: {exc}", False
    if current is None:
        return None, False
    if _matches_cleanup_record(current, record):
        return None, True
    return (
        f"{record.key}: current unversioned source object differs from cleanup manifest "
        f"(manifest etag={record.etag!r}, size={record.size}; "
        f"current etag={current.etag!r}, size={current.size})"
    ), True


def _matches_cleanup_record(current: S3ObjectProperties, record: CleanupRecord) -> bool:
    return current.etag == record.etag and current.size == record.size
