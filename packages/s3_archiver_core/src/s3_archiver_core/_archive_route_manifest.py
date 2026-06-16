from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from s3_archiver_core._archive_env import positive_int_env
from s3_archiver_core._archive_identity import stable_identity_value
from s3_archiver_core._archive_manifest_builder import iter_archive_manifest_items
from s3_archiver_core._archive_manifest_models import (
    ArchiveGroup,
    ArchiveManifest,
    ArchiveManifestRoute,
    ArchiveManifestRouteSpec,
    CopyMode,
    DestinationLocator,
    ManifestEntry,
    ParserKind,
    SkippedObject,
    SourceLister,
)
from s3_archiver_core._archive_manifest_paths import (
    as_utc,
    route_paths_overlap,
    storage_identity,
)
from s3_archiver_core._archive_manifest_store import SQLiteManifestStore
from s3_archiver_core._archive_size_limits import (
    estimated_archive_size_bytes,
    max_destination_archive_size_bytes,
)
from s3_archiver_core.archive_date_range import NO_DATE_RANGE, ArchiveDateRange
from s3_archiver_core.archive_progress import ArchiveProgress, ProgressLogger
from s3_archiver_core.s3 import VersioningState
from s3_archiver_core.temp_files import default_temp_dir

_DEFAULT_LIST_PROGRESS_ESTIMATE = 2_000_000
_MANIFEST_INSERT_BATCH = 1000


def build_route_archive_manifest(
    routes: Iterable[ArchiveManifestRouteSpec],
    *,
    run_started_at_utc: datetime,
    progress_logger: ProgressLogger | None = None,
    date_range: ArchiveDateRange = NO_DATE_RANGE,
    temp_dir: Path | None = None,
) -> ArchiveManifest:
    """Build a deterministic global manifest for route-based archiving."""

    run_started = as_utc(run_started_at_utc)
    route_tuple = tuple(routes)
    _reject_overlapping_source_paths(route_tuple)
    store = SQLiteManifestStore.temporary(_resolve_store_dir(temp_dir))
    try:
        return _build_with_store(store, route_tuple, run_started, progress_logger, date_range)
    except Exception:
        store.cleanup()
        raise


def build_archive_manifest(
    source: SourceLister,
    *,
    run_started_at_utc: datetime,
    versioning_state: VersioningState,
    parser_kind: ParserKind,
    copy_mode: CopyMode,
    route_name: str = "default",
    source_path: str = "",
    destination: DestinationLocator | None = None,
    destination_path: str = "",
    source_identity: object | None = None,
    destination_identity: object | None = None,
    date_range: ArchiveDateRange = NO_DATE_RANGE,
    temp_dir: Path | None = None,
) -> ArchiveManifest:
    """Build an archive manifest for a single source via the route builder."""

    route = ArchiveManifestRoute(
        name=route_name,
        source=source,
        destination=destination,
        parser_kind=parser_kind,
        copy_mode=copy_mode,
        source_path=source_path,
        destination_path=destination_path,
        versioning_state=versioning_state,
        source_identity=source_identity,
        destination_identity=destination_identity,
    )
    return build_route_archive_manifest(
        [route],
        run_started_at_utc=run_started_at_utc,
        date_range=date_range,
        temp_dir=temp_dir,
    )


def _build_with_store(
    store: SQLiteManifestStore,
    route_tuple: tuple[ArchiveManifestRouteSpec, ...],
    run_started: datetime,
    progress_logger: ProgressLogger | None,
    date_range: ArchiveDateRange,
) -> ArchiveManifest:
    _stream_routes_into_store(store, route_tuple, run_started, progress_logger, date_range)
    store.commit()
    store.assert_no_duplicate_sources()
    if _archive_size_filter_needed(store.archive_groups):
        _ = store.drop_oversized_groups(max_destination_archive_size_bytes())
    store.assert_no_duplicate_destinations()
    return ArchiveManifest(
        run_started,
        store.entries,
        None,
        store.archive_groups,
        store.skipped_objects,
        "sqlite",
        store.entry_size_sum(),
        store=store,
    )


def _stream_routes_into_store(
    store: SQLiteManifestStore,
    route_tuple: tuple[ArchiveManifestRouteSpec, ...],
    run_started: datetime,
    progress_logger: ProgressLogger | None,
    date_range: ArchiveDateRange,
) -> None:
    entry_buffer: list[ManifestEntry] = []
    skipped_buffer: list[SkippedObject] = []
    listed_count = 0
    list_progress_total = _list_progress_total()
    for route in route_tuple:
        for item in iter_archive_manifest_items(
            route.source,
            run_started_at_utc=run_started,
            versioning_state=_route_versioning_state(route),
            route_name=route.name,
            parser_kind=route.parser_kind,
            copy_mode=route.copy_mode,
            source_path=route.source_path,
            destination=route.destination,
            destination_path=route.destination_path,
            source_identity=route.source_identity,
            destination_identity=route.destination_identity,
            date_range=date_range,
        ):
            listed_count += 1
            if progress_logger is not None:
                progress_logger(_list_progress(listed_count, list_progress_total))
            if isinstance(item, ManifestEntry):
                entry_buffer.append(item)
                if len(entry_buffer) >= _MANIFEST_INSERT_BATCH:
                    store.add_entries(entry_buffer)
                    entry_buffer.clear()
            else:
                skipped_buffer.append(item)
                if len(skipped_buffer) >= _MANIFEST_INSERT_BATCH:
                    store.add_skipped_objects(skipped_buffer)
                    skipped_buffer.clear()
    if entry_buffer:
        store.add_entries(entry_buffer)
    if skipped_buffer:
        store.add_skipped_objects(skipped_buffer)
    if progress_logger is not None:
        progress_logger(ArchiveProgress("list", listed_count, listed_count))


def _resolve_store_dir(temp_dir: Path | None) -> Path:
    store_dir = (
        temp_dir
        if temp_dir is not None
        else Path(os.getenv("ARCHIVER_TEMP_DIR") or str(default_temp_dir()))
    )
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


def _reject_overlapping_source_paths(routes: tuple[ArchiveManifestRouteSpec, ...]) -> None:
    seen: dict[str, list[tuple[str, str]]] = {}
    for route in routes:
        identity = repr(
            stable_identity_value(route.source_identity or storage_identity(route.source))
        )
        path = route.source_path
        for other_name, other_path in seen.setdefault(identity, []):
            if route_paths_overlap(path, other_path):
                message = (
                    f"overlapping source paths for storage location: {other_name!r} "
                    f"and {route.name!r}"
                )
                raise ValueError(message)
        seen[identity].append((route.name, path))


def _route_versioning_state(route: ArchiveManifestRouteSpec) -> VersioningState:
    return route.versioning_state or route.source.versioning_state()


def _archive_size_filter_needed(groups: Iterable[ArchiveGroup]) -> bool:
    limit = max_destination_archive_size_bytes()
    return any(estimated_archive_size_bytes(group.entries) > limit for group in groups)


def _list_progress_total() -> int:
    return positive_int_env("ARCHIVER_LIST_PROGRESS_ESTIMATE", _DEFAULT_LIST_PROGRESS_ESTIMATE)


def _list_progress(completed: int, estimated_total: int) -> ArchiveProgress:
    return ArchiveProgress("list", completed, max(estimated_total, completed + 1))
