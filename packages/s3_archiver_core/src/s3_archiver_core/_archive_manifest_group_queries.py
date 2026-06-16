from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from typing import cast, final, overload, override

from s3_archiver_core._archive_manifest_digest import ManifestDigestBuilder
from s3_archiver_core._archive_manifest_models import ArchiveGroup, CopyMode, ManifestEntry
from s3_archiver_core._archive_manifest_slices import slice_items
from s3_archiver_core._archive_manifest_sqlite import (
    iter_sql_rows,
    normalize_index,
    optional_row,
    pack,
    unpack,
    unpack_entry,
)

GROUP_WHERE = """
route_name = ? AND copy_mode = ? AND archive_root = ? AND target_day = ?
AND destination_bucket = ? AND destination_archive_key = ?
AND destination_identity = ?
"""
GROUP_ENTRY_ORDER = "key, id"
_CHUNK_COLUMNS = (
    "route_name, copy_mode, archive_root, target_day, destination_bucket, "
    "destination_archive_key, destination_identity, parser_kind, "
    "source_bucket, source_identity_payload, destination_identity_payload, "
    "chunk_index, entry_offset, entry_count, chunk_destination_key, "
    "source_count, manifest_sha256"
)
_CHUNK_ORDER = (
    "route_name, copy_mode, archive_root, target_day, destination_bucket, "
    "destination_archive_key, destination_identity, chunk_index"
)
type ConnectionProvider = Callable[[], sqlite3.Connection]


def iter_group_rows(
    connection: sqlite3.Connection,
    route_name: str | None = None,
) -> Iterator[tuple[object, ...]]:
    params: tuple[object, ...] = ()
    where = "copy_mode IN ('daily_tar_gz', 'timestamp_child_tar_gz') AND target_day != ''"
    if route_name is not None:
        where += " AND route_name = ?"
        params = (route_name,)
    rows = connection.execute(
        """
        SELECT route_name, copy_mode, archive_root, target_day, destination_bucket,
            destination_archive_key, destination_identity
        FROM entries
        WHERE """
        + where
        + """
        GROUP BY route_name, copy_mode, archive_root, target_day, destination_bucket,
            destination_archive_key, destination_identity
        ORDER BY route_name, copy_mode, archive_root, target_day, destination_bucket,
            destination_archive_key, destination_identity
        """,
        params,
    )
    yield from iter_sql_rows(rows)


def rebuild_archive_chunks(connection: sqlite3.Connection) -> None:
    from s3_archiver_core._archive_manifest_groups import archive_chunk_key

    _ = connection.execute("DELETE FROM archive_chunks")
    for row in iter_group_rows(connection):
        for chunk_index, offset, length, digest, first in _iter_group_chunk_metadata(
            connection, row
        ):
            _ = connection.execute(
                """
                INSERT INTO archive_chunks (
                    route_name, copy_mode, archive_root, target_day, destination_bucket,
                    destination_archive_key, destination_identity, parser_kind,
                    source_bucket, source_identity_payload, destination_identity_payload,
                    chunk_index, entry_offset, entry_count, chunk_destination_key,
                    source_count, manifest_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *row,
                    first.parser_kind,
                    first.source_bucket,
                    pack(first.source_identity),
                    pack(first.destination_identity),
                    chunk_index,
                    offset,
                    length,
                    archive_chunk_key(str(row[5]), chunk_index),
                    length,
                    digest,
                ),
            )


def iter_chunk_rows(
    connection: sqlite3.Connection,
    route_name: str | None = None,
) -> Iterator[tuple[object, ...]]:
    query = "SELECT " + _CHUNK_COLUMNS + " FROM archive_chunks"
    params: tuple[object, ...] = ()
    if route_name is not None:
        query += " WHERE route_name = ?"
        params = (route_name,)
    query += " ORDER BY " + _CHUNK_ORDER
    yield from iter_sql_rows(connection.execute(query, params))


def group_from_chunk_row(
    connection_provider: ConnectionProvider,
    row: tuple[object, ...],
) -> ArchiveGroup:
    chunk_index = int(cast(int, row[11]))
    offset = int(cast(int, row[12]))
    length = int(cast(int, row[13]))
    entries = SQLiteGroupEntrySequence(connection_provider, row[:7], chunk_index, offset, length)
    return ArchiveGroup(
        target_day=_target_day(row),
        archive_root=str(row[2]),
        destination_archive_key=str(row[14]),
        entries=entries,
        route_name=str(row[0]),
        parser_kind=str(row[7]),
        copy_mode=cast(CopyMode, row[1]),
        source_bucket=str(row[8]),
        source_identity=unpack(cast(bytes, row[9])),
        destination_bucket=str(row[4]),
        destination_identity=unpack(cast(bytes, row[10])),
        source_count=int(cast(int, row[15])),
        manifest_sha256=str(row[16]),
    )


def chunk_row_at(
    connection: sqlite3.Connection,
    index: int,
) -> tuple[object, ...]:
    row = optional_row(
        cast(
            object,
            connection.execute(
                """
                SELECT """
                + _CHUNK_COLUMNS
                + """
                FROM archive_chunks
                ORDER BY """
                + _CHUNK_ORDER
                + """
                LIMIT 1 OFFSET ?
                """,
                (index,),
            ).fetchone(),
        )
    )
    if row is None:
        raise IndexError(index)
    return row


def iter_group_entries(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
    *,
    offset: int = 0,
    limit: int | None = None,
    chunk_index: int = 1,
) -> Iterator[ManifestEntry]:
    from s3_archiver_core._archive_manifest_groups import archive_chunk_entry

    for result in _iter_group_entry_rows(connection, row, offset=offset, limit=limit):
        yield archive_chunk_entry(unpack_entry(cast(bytes, result[0])), chunk_index)


def _iter_group_chunk_metadata(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
) -> Iterator[tuple[int, int, int, str, ManifestEntry]]:
    from s3_archiver_core._archive_manifest_groups import ArchiveChunkSizer, archive_chunk_entry

    chunker = ArchiveChunkSizer()
    chunk_index = 1
    offset = 0
    length = 0
    digest = ManifestDigestBuilder()
    first_entry: ManifestEntry | None = None
    for entry in _iter_raw_group_entries(connection, row):
        if chunker.would_overflow(entry.size):
            assert first_entry is not None
            yield chunk_index, offset, length, digest.hexdigest(), first_entry
            chunk_index += 1
            offset += length
            length = 0
            chunker.reset()
            digest = ManifestDigestBuilder()
            first_entry = None
        if first_entry is None:
            first_entry = entry
        chunker.add(entry.size)
        length += 1
        digest.add(archive_chunk_entry(entry, chunk_index))
    if length:  # pragma: no branch - length implies a first entry was seen
        assert first_entry is not None
        yield chunk_index, offset, length, digest.hexdigest(), first_entry


def _iter_raw_group_entries(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
) -> Iterator[ManifestEntry]:
    for result in _iter_group_entry_rows(connection, row):
        yield unpack_entry(cast(bytes, result[0]))


def _iter_group_entry_rows(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
    *,
    offset: int = 0,
    limit: int | None = None,
) -> Iterator[tuple[object, ...]]:
    query = "SELECT payload FROM entries WHERE " + GROUP_WHERE + " ORDER BY " + GROUP_ENTRY_ORDER
    params = row
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params = (*row, limit, offset)
    yield from iter_sql_rows(connection.execute(query, params))


def _target_day(row: tuple[object, ...]) -> date:
    value = str(row[3])
    return date.fromisoformat(value)


@final
class SQLiteGroupEntrySequence(Sequence[ManifestEntry]):
    def __init__(
        self,
        connection_provider: ConnectionProvider,
        row: tuple[object, ...],
        chunk_index: int,
        offset: int,
        length: int,
    ) -> None:
        self._connection_provider = connection_provider
        self._row = row
        self._chunk_index = chunk_index
        self._offset = offset
        self._length = length

    @override
    def __len__(self) -> int:
        return self._length

    @overload
    def __getitem__(self, index: int) -> ManifestEntry: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[ManifestEntry, ...]: ...

    @override
    def __getitem__(self, index: int | slice) -> ManifestEntry | tuple[ManifestEntry, ...]:
        if isinstance(index, slice):
            return slice_items(self, index)
        normalized = normalize_index(index, self._length)
        return next(
            iter_group_entries(
                self._connection_provider(),
                self._row,
                offset=self._offset + normalized,
                limit=1,
                chunk_index=self._chunk_index,
            )
        )

    @override
    def __iter__(self) -> Iterator[ManifestEntry]:
        return iter_group_entries(
            self._connection_provider(),
            self._row,
            offset=self._offset,
            limit=self._length,
            chunk_index=self._chunk_index,
        )
