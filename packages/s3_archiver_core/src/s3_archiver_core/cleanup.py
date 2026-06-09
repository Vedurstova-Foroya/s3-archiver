"""Source-object cleanup engine.

``run_cleanup`` consumes one or more cleanup-input manifests, deletes each
referenced source object at the exact archived version, double-checks it is
actually gone, and records every verified deletion into a temporary cleaned
manifest. When the cleaned manifest matches the input exactly the input manifest
is retired; otherwise it is kept so a later run can retry the remainder.

The lock that guards archiving is owned by the caller, not this module, so a
cleanup run and an archive run can never overlap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

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

__all__ = ("CleanupResult", "run_cleanup")

_logger = logging.getLogger("s3_archiver.cleanup")


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
    for record in iter_cleanup_records(path):
        if timed_out():
            failures.append(f"{record.key}: cleanup run timed out")
            return
        failure = _delete_and_verify(record, routes_by_name)
        if failure is not None:
            failures.append(failure)
            continue
        yield record


def _delete_and_verify(
    record: CleanupRecord, routes_by_name: dict[str, ArchiveRoute]
) -> str | None:
    route = routes_by_name.get(record.route_name)
    if route is None:
        return f"{record.key}: no configured route named {record.route_name!r}"
    if stable_identity_value(route.source_identity) != record.source_identity:
        return f"{record.key}: source identity mismatch for route {record.route_name!r}"
    try:
        route.source.delete_source_object(record.key, record.version_id)
    except Exception as exc:
        return f"{record.key}: delete failed: {exc}"
    try:
        remaining = route.source.head_object(record.key, record.version_id)
    except Exception as exc:
        return f"{record.key}: delete verification failed: {exc}"
    if remaining is not None:
        return f"{record.key}: still present after delete"
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
    return None
