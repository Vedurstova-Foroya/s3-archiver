from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from s3_archiver_core._archive_manifest_models import (
    CopyMode,
    DestinationLocator,
    ManifestEntry,
    ParserKind,
    SkippedObject,
    SourceLister,
)
from s3_archiver_core._archive_manifest_paths import (
    as_utc,
    join_key,
    normalize_prefix,
    relative_key,
    route_path_prefix,
    storage_identity,
)
from s3_archiver_core._archive_manifest_selection import select_object
from s3_archiver_core._archive_size_limits import (
    log_source_object_skip,
    max_source_object_size_bytes,
    source_object_skip_reason,
)
from s3_archiver_core.archive_date_range import NO_DATE_RANGE, ArchiveDateRange
from s3_archiver_core.parsers.filename_timestamp import (
    archive_root_for_key,
    destination_archive_key,
)
from s3_archiver_core.parsers.results import SkippedObject as ParserSkippedObject
from s3_archiver_core.parsers.results import TimestampSource
from s3_archiver_core.s3 import S3ListedObject, VersioningState


def iter_archive_manifest_items(
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
    exclude_prefixes: tuple[str, ...] = (),
) -> Iterator[ManifestEntry | SkippedObject]:
    """Yield selected and skipped manifest rows without retaining them in memory."""

    context = _ManifestBuildContext(
        source=source,
        destination=destination,
        route_name=route_name,
        parser_kind=parser_kind,
        copy_mode=copy_mode,
        source_path=normalize_prefix(source_path),
        destination_path=normalize_prefix(destination_path),
        source_identity=source_identity or storage_identity(source),
        destination_identity=destination_identity or storage_identity(destination),
        exclude_prefixes=tuple(route_path_prefix(prefix) for prefix in exclude_prefixes),
    )
    run_started = as_utc(run_started_at_utc)
    run_started_day = run_started.date()
    max_source_size = max_source_object_size_bytes()
    for listed in _list_source_objects(source, versioning_state, context.source_path):
        if context.source_path and not listed.key.startswith(context.source_path):
            continue
        if context.exclude_prefixes and any(
            listed.key.startswith(excluded) for excluded in context.exclude_prefixes
        ):
            continue
        if reason := source_object_skip_reason(listed.size):
            skipped = context.skipped_object(listed, reason)
            log_source_object_skip(skipped, max_size_bytes=max_source_size)
            yield skipped
            continue
        selected = select_object(parser_kind, listed, context.source_path)
        if isinstance(selected, SkippedObject | ParserSkippedObject):
            yield context.skipped_object(listed, selected.reason)
            continue
        timestamp = as_utc(selected.timestamp)
        target_day = timestamp.date()
        if target_day >= run_started_day:
            yield context.skipped_object(
                listed,
                "parser timestamp in incomplete UTC day",
                selected_timestamp=timestamp,
                timestamp_source=selected.timestamp_source,
                target_day=target_day,
                archive_root=selected.archive_root,
            )
            continue
        if not date_range.includes(target_day):
            yield context.skipped_object(
                listed,
                "parser timestamp outside configured date range",
                selected_timestamp=timestamp,
                timestamp_source=selected.timestamp_source,
                target_day=target_day,
                archive_root=selected.archive_root,
            )
            continue
        yield context.entry(
            listed,
            timestamp,
            selected.timestamp_source,
            target_day,
            archive_root=selected.archive_root,
        )


@dataclass(frozen=True, slots=True)
class _ManifestBuildContext:
    source: SourceLister
    destination: DestinationLocator | None
    route_name: str
    parser_kind: ParserKind
    copy_mode: CopyMode
    source_path: str
    destination_path: str
    source_identity: object | None
    destination_identity: object | None
    exclude_prefixes: tuple[str, ...]

    @property
    def destination_bucket(self) -> str:
        return "" if self.destination is None else self.destination.bucket

    def entry(
        self,
        listed: S3ListedObject,
        selected_timestamp: datetime,
        timestamp_source: TimestampSource,
        target_day: date,
        *,
        archive_root: str | None,
    ) -> ManifestEntry:
        root = (
            archive_root
            if archive_root is not None
            else archive_root_for_key(relative_key(listed.key, self.source_path))
        )
        destination_key = (
            join_key(self.destination_path, listed.key)
            if self.copy_mode == "direct"
            else join_key(
                self.destination_path,
                self._archive_destination_key(root, target_day, selected_timestamp),
            )
        )
        return ManifestEntry(
            source_bucket=self.source.bucket,
            key=listed.key,
            size=listed.size,
            last_modified=listed.last_modified,
            etag=listed.etag,
            version_id=listed.version_id,
            object=listed,
            selected_timestamp=selected_timestamp,
            timestamp_source=timestamp_source,
            target_day=target_day,
            archive_root=root,
            destination_archive_key=destination_key,
            route_name=self.route_name,
            parser_kind=self.parser_kind,
            copy_mode=self.copy_mode,
            source_path=self.source_path,
            destination_bucket=self.destination_bucket,
            destination_path=self.destination_path,
            destination_key=destination_key,
            source_identity=self.source_identity,
            destination_identity=self.destination_identity,
        )

    def _archive_destination_key(
        self, archive_root: str, target_day: date, selected_timestamp: datetime
    ) -> str:
        if self.copy_mode == "timestamp_child_tar_gz":
            return timestamp_child_archive_key(archive_root, selected_timestamp)
        return destination_archive_key(archive_root, target_day)

    def skipped_object(
        self,
        listed: S3ListedObject,
        reason: str,
        *,
        selected_timestamp: datetime | None = None,
        timestamp_source: TimestampSource | None = None,
        target_day: date | None = None,
        archive_root: str | None = None,
    ) -> SkippedObject:
        return SkippedObject(
            key=listed.key,
            reason=reason,
            route_name=self.route_name,
            parser_kind=self.parser_kind,
            copy_mode=self.copy_mode,
            size=listed.size,
            last_modified=listed.last_modified,
            etag=listed.etag,
            version_id=listed.version_id,
            selected_timestamp=selected_timestamp,
            timestamp_source=timestamp_source,
            target_day=target_day,
            archive_root=archive_root or "",
            source_bucket=self.source.bucket,
            source_path=self.source_path,
            destination_bucket=self.destination_bucket,
            destination_path=self.destination_path,
            source_identity=self.source_identity,
            destination_identity=self.destination_identity,
        )


def _list_source_objects(
    source: SourceLister,
    versioning_state: VersioningState,
    source_path: str,
) -> Iterable[S3ListedObject]:
    try:
        return source.list_source_objects(versioning_state, prefix=source_path)
    except TypeError:
        if source_path:
            raise
        legacy_lister = cast(
            Callable[[VersioningState], Iterable[S3ListedObject]], source.list_source_objects
        )
        return legacy_lister(versioning_state)


def timestamp_child_archive_key(archive_root: str, selected_timestamp: datetime) -> str:
    child = archive_root.rstrip("/").rsplit("/", maxsplit=1)[-1]
    if not child:
        child = "archive"
    timestamp = selected_timestamp.strftime("%Y-%m-%d-%H")
    return f"{timestamp}-{child}.tar.gz"
