from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from itertools import chain

from s3_archiver_core._archive_env import positive_int_env
from s3_archiver_core._archive_identity import stable_identity_value
from s3_archiver_core._archive_manifest_builder import iter_archive_manifest_items
from s3_archiver_core._archive_manifest_groups import archive_groups
from s3_archiver_core._archive_manifest_models import (
    ArchiveGroup,
    ArchiveManifest,
    ArchiveManifestRouteSpec,
    ManifestEntry,
    SkippedObject,
)
from s3_archiver_core._archive_manifest_paths import (
    as_utc,
    route_paths_overlap,
    storage_identity,
)
from s3_archiver_core._archive_manifest_store import SQLiteManifestStore
from s3_archiver_core._archive_size_limits import (
    estimated_archive_size_bytes,
    filter_archive_groups_by_size,
    max_destination_archive_size_bytes,
)
from s3_archiver_core.archive_progress import ArchiveProgress, ProgressLogger
from s3_archiver_core.s3 import VersioningState

_SQLITE_MANIFEST_ENTRY_THRESHOLD = 100_000
_DEFAULT_LIST_PROGRESS_ESTIMATE = 2_000_000


def build_route_archive_manifest(
    routes: Iterable[ArchiveManifestRouteSpec],
    *,
    run_started_at_utc: datetime,
    progress_logger: ProgressLogger | None = None,
) -> ArchiveManifest:
    """Build a deterministic global manifest for route-based archiving."""

    run_started = as_utc(run_started_at_utc)
    route_tuple = tuple(routes)
    _reject_overlapping_source_paths(route_tuple)
    entries: list[ManifestEntry] = []
    skipped: list[SkippedObject] = []
    store: SQLiteManifestStore | None = None
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
        ):
            listed_count += 1
            if progress_logger is not None:
                progress_logger(_list_progress(listed_count, list_progress_total))
            if store is None and len(entries) + len(skipped) >= _SQLITE_MANIFEST_ENTRY_THRESHOLD:
                store = SQLiteManifestStore.temporary()
                for entry in entries:
                    store.add_entry(entry)
                for skipped_object in skipped:
                    store.add_skipped(skipped_object)
                entries.clear()
                skipped.clear()
            if isinstance(item, ManifestEntry):
                if store is None:
                    entries.append(item)
                else:
                    store.add_entry(item)
            elif store is None:
                skipped.append(item)
            else:
                store.add_skipped(item)
    if progress_logger is not None:
        progress_logger(ArchiveProgress("list", listed_count, listed_count))
    if store is not None:
        store.commit()
        store.assert_no_duplicate_sources()
        if not _archive_size_filter_needed(store.archive_groups):
            store.assert_no_duplicate_destinations()
            return ArchiveManifest(
                run_started,
                store.entries,
                None,
                store.archive_groups,
                store.skipped_objects,
                "sqlite",
                store.entry_size_sum(),
            )
        entries_tuple, groups_tuple, skipped_tuple = filter_archive_groups_by_size(
            tuple(store.entries), tuple(store.archive_groups), tuple(store.skipped_objects)
        )
        _reject_duplicate_destinations(entries_tuple, groups_tuple)
        return ArchiveManifest(
            run_started,
            entries_tuple,
            None,
            groups_tuple,
            skipped_tuple,
            "sqlite",
            sum(entry.size for entry in entries_tuple),
        )
    entry_tuple = tuple(entries)
    _reject_duplicate_sources(entry_tuple)
    grouped = archive_groups(entry_tuple)
    entry_tuple, grouped, skipped_tuple = filter_archive_groups_by_size(
        entry_tuple, grouped, tuple(skipped)
    )
    _reject_duplicate_destinations(entry_tuple, grouped)
    return ArchiveManifest(
        run_started,
        entry_tuple,
        None,
        grouped,
        skipped_tuple,
        source_byte_count=sum(entry.size for entry in entry_tuple),
    )


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


def _reject_duplicate_sources(entries: tuple[ManifestEntry, ...]) -> None:
    _reject_duplicate_identities(
        (
            (entry.source_identity, entry.source_bucket, entry.key, entry.version_id)
            for entry in entries
        ),
        "duplicate source object identity",
    )


def _reject_duplicate_destinations(
    entries: Iterable[ManifestEntry], groups: Iterable[ArchiveGroup]
) -> None:
    _reject_duplicate_identities(
        chain(
            (
                (entry.destination_identity, entry.destination_bucket, entry.destination_key)
                for entry in entries
                if entry.copy_mode == "direct"
            ),
            (
                (
                    group.destination_identity,
                    group.destination_bucket,
                    group.destination_archive_key,
                )
                for group in groups
            ),
        ),
        "duplicate destination object identity",
    )


def _reject_duplicate_identities(keys: Iterable[object], message: str) -> None:
    seen: set[str] = set()
    for key in keys:
        stable_key = repr(stable_identity_value(key))
        if stable_key in seen:
            raise ValueError(message)
        seen.add(stable_key)
