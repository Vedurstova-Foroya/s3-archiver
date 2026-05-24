from __future__ import annotations

import pickle
import sqlite3
from collections.abc import Iterator, Sequence
from typing import cast

from s3_archiver_core._archive_identity import stable_identity_value
from s3_archiver_core._archive_manifest_models import ManifestEntry, SkippedObject


def stable_key(value: object) -> str:
    return repr(stable_identity_value(value))


def pack(value: object) -> bytes:
    return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)


def unpack(value: bytes) -> object:
    return cast(object, pickle.loads(value))


def unpack_entry(value: bytes) -> ManifestEntry:
    return cast(ManifestEntry, pickle.loads(value))


def unpack_skipped(value: bytes) -> SkippedObject:
    return cast(SkippedObject, pickle.loads(value))


def create_schema(connection: sqlite3.Connection) -> None:
    _ = connection.executescript(
        """
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            route_name TEXT NOT NULL,
            copy_mode TEXT NOT NULL,
            source_identity TEXT NOT NULL,
            source_bucket TEXT NOT NULL,
            key TEXT NOT NULL,
            version_id TEXT NOT NULL,
            destination_identity TEXT NOT NULL,
            destination_bucket TEXT NOT NULL,
            destination_key TEXT NOT NULL,
            destination_archive_key TEXT NOT NULL,
            target_day TEXT NOT NULL,
            archive_root TEXT NOT NULL,
            size INTEGER NOT NULL,
            payload BLOB NOT NULL
        );
        CREATE TABLE skipped (
            id INTEGER PRIMARY KEY,
            payload BLOB NOT NULL
        );
        CREATE TABLE archive_chunks (
            id INTEGER PRIMARY KEY,
            route_name TEXT NOT NULL,
            copy_mode TEXT NOT NULL,
            archive_root TEXT NOT NULL,
            target_day TEXT NOT NULL,
            destination_bucket TEXT NOT NULL,
            destination_archive_key TEXT NOT NULL,
            destination_identity TEXT NOT NULL,
            parser_kind TEXT NOT NULL,
            source_bucket TEXT NOT NULL,
            source_identity_payload BLOB NOT NULL,
            destination_identity_payload BLOB NOT NULL,
            chunk_index INTEGER NOT NULL,
            entry_offset INTEGER NOT NULL,
            entry_count INTEGER NOT NULL,
            chunk_destination_key TEXT NOT NULL,
            source_count INTEGER NOT NULL,
            manifest_sha256 TEXT NOT NULL
        );
        CREATE INDEX entries_route_copy_idx ON entries(route_name, copy_mode);
        CREATE INDEX entries_group_idx ON entries(
            route_name, copy_mode, archive_root, target_day, destination_bucket,
            destination_archive_key, destination_identity, key, id
        );
        CREATE INDEX archive_chunks_route_idx ON archive_chunks(route_name);
        CREATE INDEX archive_chunks_destination_idx ON archive_chunks(
            destination_identity, destination_bucket, chunk_destination_key
        );
        """
    )


def single_value(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[object, ...],
) -> bytes:
    row = optional_row(cast(object, connection.execute(query, params).fetchone()))
    if row is None:
        raise IndexError(params[0])
    return cast(bytes, row[0])


def normalize_index(index: int, length: int) -> int:
    normalized = length + index if index < 0 else index
    if normalized < 0 or normalized >= length:
        raise IndexError(index)
    return normalized


def optional_row(row: object) -> tuple[object, ...] | None:
    return None if row is None else tuple(cast(Sequence[object], row))


def required_row(row: object) -> tuple[object, ...]:
    optional = optional_row(row)
    if optional is None:
        raise RuntimeError("sqlite query unexpectedly returned no rows")
    return optional


def iter_sql_rows(cursor: sqlite3.Cursor) -> Iterator[tuple[object, ...]]:
    while True:
        row = cast(object, cursor.fetchone())
        if row is None:
            return
        yield tuple(cast(Sequence[object], row))
