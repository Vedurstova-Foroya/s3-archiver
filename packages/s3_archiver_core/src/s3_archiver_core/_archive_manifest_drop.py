"""Bounded-memory remediation for oversized archive groups.

Drops archive chunks whose estimated staged tar size exceeds the destination
archive-size policy without ever materializing the manifest: each oversized
chunk's entries are recorded in ``skipped`` and deleted from ``entries`` one
chunk at a time. Chunks are processed from the highest entry offset downward so
a delete never shifts the slice of an unprocessed chunk in the same group.
"""

from __future__ import annotations

import sqlite3
from typing import cast

from s3_archiver_core._archive_manifest_group_queries import (
    GROUP_ENTRY_ORDER,
    GROUP_WHERE,
    group_from_chunk_row,
    iter_chunk_rows,
    rebuild_archive_chunks,
)
from s3_archiver_core._archive_manifest_sqlite import iter_sql_rows, pack, unpack_entry
from s3_archiver_core._archive_size_limits import (
    archive_group_skip_reason,
    estimated_archive_size_bytes,
    log_archive_group_skip,
    skipped_archive_entry,
)


def drop_oversized_chunks(connection: sqlite3.Connection, limit: int) -> int:
    """Drop archive chunks whose estimated size exceeds ``limit`` in place.

    Returns the number of dropped chunks, rebuilding the chunk table and
    committing only when at least one chunk was dropped.
    """

    chunk_rows = tuple(iter_chunk_rows(connection))
    dropped = sum(
        1 for row in reversed(chunk_rows) if _drop_chunk_if_oversized(connection, row, limit)
    )
    if dropped:
        rebuild_archive_chunks(connection)
        connection.commit()
    return dropped


def fetch_chunk_entry_id_payloads(
    connection: sqlite3.Connection, row: tuple[object, ...]
) -> list[tuple[object, ...]]:
    """Return the ``(id, payload)`` rows for one chunk's entry slice."""

    offset = int(cast(int, row[12]))
    count = int(cast(int, row[13]))
    query = (
        "SELECT id, payload FROM entries WHERE "
        + GROUP_WHERE
        + " ORDER BY "
        + GROUP_ENTRY_ORDER
        + " LIMIT ? OFFSET ?"
    )
    return list(iter_sql_rows(connection.execute(query, (*row[:7], count, offset))))


def _drop_chunk_if_oversized(
    connection: sqlite3.Connection, row: tuple[object, ...], limit: int
) -> bool:
    def provider() -> sqlite3.Connection:
        return connection

    group = group_from_chunk_row(provider, row)
    estimated_size = estimated_archive_size_bytes(group.entries)
    if estimated_size <= limit:
        return False
    reason = archive_group_skip_reason(estimated_size, limit)
    log_archive_group_skip(group, reason, estimated_size, limit)
    id_payloads = fetch_chunk_entry_id_payloads(connection, row)
    _ = connection.executemany(
        "INSERT INTO skipped (payload) VALUES (?)",
        (
            (pack(skipped_archive_entry(unpack_entry(cast(bytes, payload)), reason)),)
            for _, payload in id_payloads
        ),
    )
    _ = connection.executemany(
        "DELETE FROM entries WHERE id = ?",
        ((entry_id,) for entry_id, _ in id_payloads),
    )
    return True
