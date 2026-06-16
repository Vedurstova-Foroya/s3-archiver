from __future__ import annotations

import sqlite3
import tempfile
import threading
import weakref
from collections.abc import Iterable, Iterator
from contextlib import suppress
from pathlib import Path
from threading import RLock, get_ident
from typing import cast, final

from s3_archiver_core._archive_manifest_counts import count_entries, count_skipped
from s3_archiver_core._archive_manifest_destination_checks import (
    has_direct_archive_destination_collision,
    has_duplicate_archive_destination,
    has_duplicate_direct_destination,
)
from s3_archiver_core._archive_manifest_drop import drop_oversized_chunks
from s3_archiver_core._archive_manifest_group_queries import (
    chunk_row_at,
    group_from_chunk_row,
    iter_chunk_rows,
    rebuild_archive_chunks,
)
from s3_archiver_core._archive_manifest_models import ArchiveGroup, ManifestEntry, SkippedObject
from s3_archiver_core._archive_manifest_sequences import (
    ArchiveGroupSequence,
    EntrySequence,
    SkippedSequence,
)
from s3_archiver_core._archive_manifest_sqlite import (
    create_schema,
    iter_sql_rows,
    normalize_index,
    optional_row,
    pack,
    required_row,
    single_value,
    stable_key,
    unpack_entry,
    unpack_skipped,
)

_INSERT_ENTRY_SQL = (
    "INSERT INTO entries (route_name, copy_mode, source_identity, source_bucket, key, "
    + "version_id, destination_identity, destination_bucket, destination_key, "
    + "destination_archive_key, target_day, archive_root, size, payload) "
    + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_SKIPPED_SQL = "INSERT INTO skipped (payload) VALUES (?)"
_DUPLICATE_SOURCE_SQL = (
    "SELECT source_identity, source_bucket, key, version_id FROM entries "
    + "GROUP BY source_identity, source_bucket, key, version_id "
    + "HAVING COUNT(*) > 1 LIMIT 1"
)
_TARGET_DAYS_SQL = (
    "SELECT DISTINCT target_day FROM entries "
    + "WHERE copy_mode IN ('daily_tar_gz', 'timestamp_child_tar_gz') AND target_day != '' "
    + "ORDER BY target_day"
)


def _entry_row(entry: ManifestEntry) -> tuple[object, ...]:
    return (
        entry.route_name,
        entry.copy_mode,
        stable_key(entry.source_identity),
        entry.source_bucket,
        entry.key,
        entry.version_id or "",
        stable_key(entry.destination_identity),
        entry.destination_bucket,
        entry.destination_key,
        entry.destination_archive_key,
        "" if entry.target_day is None else entry.target_day.isoformat(),
        entry.archive_root,
        entry.size,
        pack(entry),
    )


@final
class SQLiteManifestStore:
    """Disk-backed archive manifest rows."""

    def __init__(self, path: Path) -> None:
        self.path: Path = path
        self._connection: sqlite3.Connection = sqlite3.connect(path, check_same_thread=False)
        self._connection_lock = RLock()
        self._reader_connections: dict[int, sqlite3.Connection] = {}
        self._group_count: int | None = None
        self._committed = False
        _ = self._connection.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF")
        self._create_schema()

    @classmethod
    def temporary(cls, directory: Path | None = None) -> SQLiteManifestStore:
        with tempfile.NamedTemporaryFile(
            prefix="s3-archiver-manifest-",
            suffix=".sqlite3",
            dir=None if directory is None else directory,
            delete=False,
        ) as temp:
            path = Path(temp.name)
        return cls(path)

    @property
    def entries(self) -> EntrySequence:
        return EntrySequence(self)

    @property
    def archive_groups(self) -> ArchiveGroupSequence:
        return ArchiveGroupSequence(self)

    @property
    def skipped_objects(self) -> SkippedSequence:
        return SkippedSequence(self)

    def add_entry(self, entry: ManifestEntry) -> None:
        with self._connection_lock:
            _ = self._connection.execute(_INSERT_ENTRY_SQL, _entry_row(entry))
        self._group_count = None

    def add_entries(self, entries: Iterable[ManifestEntry]) -> None:
        with self._connection_lock:
            _ = self._connection.executemany(
                _INSERT_ENTRY_SQL, [_entry_row(entry) for entry in entries]
            )
        self._group_count = None

    def add_skipped(self, skipped: SkippedObject) -> None:
        with self._connection_lock:
            _ = self._connection.execute(_INSERT_SKIPPED_SQL, (pack(skipped),))

    def add_skipped_objects(self, items: Iterable[SkippedObject]) -> None:
        with self._connection_lock:
            _ = self._connection.executemany(_INSERT_SKIPPED_SQL, [(pack(item),) for item in items])
        self._group_count = None

    def commit(self) -> None:
        with self._connection_lock:
            rebuild_archive_chunks(self._connection)
            self._connection.commit()
            self._committed = True
        self._group_count = self._calculate_group_count()

    def assert_no_duplicate_sources(self) -> None:
        with self._connection_lock:
            row = optional_row(
                cast(object, self._connection.execute(_DUPLICATE_SOURCE_SQL).fetchone())
            )
        if row is not None:
            raise ValueError("duplicate source object identity")

    def assert_no_duplicate_destinations(self) -> None:
        if (
            has_duplicate_direct_destination(self._connection)
            or has_duplicate_archive_destination(self._connection)
            or has_direct_archive_destination_collision(self._connection)
        ):
            raise ValueError("duplicate destination object identity")

    def drop_oversized_groups(self, limit: int) -> int:
        """Drop archive groups whose estimated size exceeds ``limit`` in place."""

        with self._connection_lock:
            dropped = drop_oversized_chunks(self._connection, limit)
            if dropped:
                self._group_count = None
        return dropped

    def entry_count(self) -> int:
        with self._connection_lock:
            return count_entries(self._connection)

    def entry_count_by_copy_mode(self, copy_mode: str) -> int:
        return self._scalar_int("SELECT COUNT(*) FROM entries WHERE copy_mode = ?", (copy_mode,))

    def skipped_count(self) -> int:
        with self._connection_lock:
            return count_skipped(self._connection)

    def group_count(self) -> int:
        if self._group_count is None:
            self._group_count = self._calculate_group_count()
        return self._group_count

    def _calculate_group_count(self) -> int:
        return self._scalar_int("SELECT COUNT(*) FROM archive_chunks")

    def _scalar_int(self, query: str, params: tuple[object, ...] = ()) -> int:
        with self._connection_lock:
            row = required_row(cast(object, self._connection.execute(query, params).fetchone()))
        return int(cast(int, row[0]))

    def target_days(self) -> tuple[str, ...]:
        with self._connection_lock:
            return tuple(
                str(row[0]) for row in iter_sql_rows(self._connection.execute(_TARGET_DAYS_SQL))
            )

    def entry_size_sum(self) -> int:
        return self._scalar_int("SELECT COALESCE(SUM(size), 0) FROM entries")

    def iter_entries(self) -> Iterator[ManifestEntry]:
        connection = self._reader_connection()
        for row in iter_sql_rows(connection.execute("SELECT payload FROM entries ORDER BY id")):
            yield unpack_entry(cast(bytes, row[0]))

    def iter_route_entries(
        self,
        route_name: str,
        copy_mode: str | None = None,
    ) -> Iterator[ManifestEntry]:
        query = "SELECT payload FROM entries WHERE route_name = ?"
        params: tuple[object, ...] = (route_name,)
        if copy_mode is not None:
            query += " AND copy_mode = ?"
            params = (route_name, copy_mode)
        query += " ORDER BY id"
        connection = self._reader_connection()
        for row in iter_sql_rows(connection.execute(query, params)):
            yield unpack_entry(cast(bytes, row[0]))

    def entry_at(self, index: int) -> ManifestEntry:
        return unpack_entry(
            single_value(
                self._reader_connection(),
                "SELECT payload FROM entries ORDER BY id LIMIT 1 OFFSET ?",
                (index,),
            )
        )

    def iter_skipped(self) -> Iterator[SkippedObject]:
        connection = self._reader_connection()
        for row in iter_sql_rows(connection.execute("SELECT payload FROM skipped ORDER BY id")):
            yield unpack_skipped(cast(bytes, row[0]))

    def skipped_at(self, index: int) -> SkippedObject:
        return unpack_skipped(
            single_value(
                self._reader_connection(),
                "SELECT payload FROM skipped ORDER BY id LIMIT 1 OFFSET ?",
                (index,),
            )
        )

    def iter_groups(self, route_name: str | None = None) -> Iterator[ArchiveGroup]:
        with self._connection_lock:
            rows = tuple(iter_chunk_rows(self._connection, route_name))
        for row in rows:
            yield group_from_chunk_row(self._reader_connection, row)

    def group_at(self, index: int) -> ArchiveGroup:
        normalized = normalize_index(index, self.group_count())
        with self._connection_lock:
            row = chunk_row_at(self._connection, normalized)
        return group_from_chunk_row(self._reader_connection, row)

    def _reader_connection(self) -> sqlite3.Connection:
        if not self._committed:
            return self._connection
        thread_id = get_ident()
        with self._connection_lock:
            connection = self._reader_connections.get(thread_id)
            if connection is None:
                connection = sqlite3.connect(self.path, check_same_thread=False)
                self._reader_connections[thread_id] = connection
                _ = weakref.finalize(
                    threading.current_thread(),
                    self._reap_reader_connection,
                    thread_id,
                )
            return connection

    def _reap_reader_connection(self, thread_id: int) -> None:
        with self._connection_lock:
            connection = self._reader_connections.pop(thread_id, None)
        if connection is not None:
            with suppress(Exception):
                connection.close()

    def _create_schema(self) -> None:
        create_schema(self._connection)

    def close(self) -> None:
        with self._connection_lock:
            self._connection.close()
            for connection in self._reader_connections.values():
                connection.close()
            self._reader_connections.clear()

    def cleanup(self) -> None:
        with suppress(Exception):
            self.close()
        self.path.unlink(missing_ok=True)

    def __del__(self) -> None:
        self.cleanup()
