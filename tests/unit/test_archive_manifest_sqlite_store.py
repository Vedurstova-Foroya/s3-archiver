"""SQLite manifest store edge coverage."""

from __future__ import annotations

import gc
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core._archive_copy_routes import (
    archive_groups_for_route,
    direct_entries_for_route,
    direct_entry_count,
)
from s3_archiver_core._archive_manifest_group_queries import (
    chunk_row_at,
    iter_group_entries,
    iter_group_rows,
)
from s3_archiver_core._archive_manifest_groups import (
    ArchiveChunkSizer,
    archive_chunk_key,
    archive_chunk_ranges,
    archive_groups,
)
from s3_archiver_core._archive_manifest_models import (
    CopyMode,
    ManifestEntry,
    SkippedObject,
)
from s3_archiver_core._archive_manifest_sqlite import (
    create_schema,
    normalize_index,
    optional_row,
    pack,
    required_row,
    single_value,
    stable_key,
    unpack,
    unpack_entry,
    unpack_skipped,
)
from s3_archiver_core._archive_manifest_store import SQLiteManifestStore

from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)
TARGET_DAY = date(2026, 4, 13)


@pytest.mark.unit()
def test_sqlite_manifest_store_streams_sequences_and_lazy_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVER_ARCHIVE_GROUP_MAX_OBJECTS", "1")
    store = SQLiteManifestStore(tmp_path / "manifest.sqlite3")
    direct = _entry("raw/a.txt", copy_mode="direct", route_name="raw", target_day=None)
    first = _entry("data/one.xml", route_name="daily")
    second = _entry("data/two.xml", route_name="daily")
    skipped = SkippedObject("data/future.xml", "future", route_name="daily")
    try:
        store.add_entry(direct)
        store.add_entry(second)
        store.add_entry(first)
        store.add_skipped(skipped)

        assert len(store.entries) == 3
        assert [entry.key for entry in store.entries[:2]] == ["raw/a.txt", "data/two.xml"]
        assert store.entries[-1].key == "data/one.xml"
        assert [entry.key for entry in store.entries.iter_route("daily")] == [
            "data/two.xml",
            "data/one.xml",
        ]
        assert [entry.key for entry in store.entries.iter_route("daily", "daily_tar_gz")] == [
            "data/two.xml",
            "data/one.xml",
        ]
        assert store.entries.count_copy_mode("direct") == 1
        assert len(store.skipped_objects) == 1
        assert store.skipped_objects[0].key == "data/future.xml"
        assert store.skipped_objects[:1] == (skipped,)
        assert [item.key for item in store.skipped_objects] == ["data/future.xml"]
        assert store.entries[::-1][0].key == "data/one.xml"
        assert store.group_count() == 0

        store.commit()

        assert store.target_days() == ("2026-04-13",)
        assert store.entry_size_sum() == 30
        assert store.group_count() == 2
        assert store.archive_groups[:1][0].destination_archive_key == "2026-04-13"
        assert len(store.archive_groups) == 2
        assert [group.destination_archive_key for group in store.archive_groups] == [
            "2026-04-13",
            "2026-04-13.part-00002",
        ]
        group = store.archive_groups[0]
        assert group.parser_kind == "filename_timestamp"
        assert group.source_bucket == "source"
        assert group.source_identity == ("source-id",)
        assert group.destination_identity == ("destination-id",)
        assert len(group.entries) == 1
        assert group.entries[0].destination_archive_key == "2026-04-13"
        assert group.entries[:1][0].key == "data/one.xml"
        assert store.archive_groups[-1].entries[0].key == "data/two.xml"
        assert [group.route_name for group in store.archive_groups.iter_route("daily")] == [
            "daily",
            "daily",
        ]
        assert direct_entry_count(store.entries) == 1
        assert [entry.key for entry in direct_entries_for_route(store.entries, "raw")] == [
            "raw/a.txt"
        ]
        assert [
            group.route_name for group in archive_groups_for_route(store.archive_groups, "daily")
        ]
        with pytest.raises(IndexError):
            _ = store.archive_groups[10]
    finally:
        store.cleanup()


@pytest.mark.unit()
def test_sqlite_manifest_store_duplicate_checks(tmp_path: Path) -> None:
    source_duplicate = SQLiteManifestStore(tmp_path / "source-duplicate.sqlite3")
    try:
        source_duplicate.add_entry(_entry("same.txt", route_name="one"))
        source_duplicate.add_entry(_entry("same.txt", route_name="two"))
        source_duplicate.commit()
        with pytest.raises(ValueError, match="duplicate source"):
            source_duplicate.assert_no_duplicate_sources()
    finally:
        source_duplicate.cleanup()

    destination_duplicate = SQLiteManifestStore(tmp_path / "destination-duplicate.sqlite3")
    try:
        destination_duplicate.add_entry(_entry("one.txt", copy_mode="direct", route_name="one"))
        destination_duplicate.add_entry(_entry("two.txt", copy_mode="direct", route_name="two"))
        destination_duplicate.commit()
        assert list(destination_duplicate.skipped_objects) == []
        with pytest.raises(ValueError, match="duplicate destination"):
            destination_duplicate.assert_no_duplicate_destinations()
    finally:
        destination_duplicate.cleanup()


@pytest.mark.unit()
def test_sqlite_helpers_cover_error_edges() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    assert stable_key("a") == repr("a")
    assert unpack(pack(("identity",))) == ("identity",)
    assert unpack_entry(pack(_entry("data/one.xml"))).key == "data/one.xml"
    assert unpack_skipped(pack(SkippedObject("key", "reason"))).reason == "reason"
    assert optional_row(None) is None
    assert required_row((1,)) == (1,)
    with pytest.raises(RuntimeError):
        _ = required_row(None)
    with pytest.raises(IndexError):
        _ = single_value(connection, "SELECT payload FROM entries LIMIT 1 OFFSET ?", (0,))
    with pytest.raises(IndexError):
        _ = normalize_index(-2, 1)
    with pytest.raises(IndexError):
        _ = normalize_index(1, 1)
    with pytest.raises(IndexError):
        _ = chunk_row_at(connection, 0)
    assert list(iter_group_rows(connection, "route")) == []
    assert (
        list(
            iter_group_entries(
                connection,
                ("route", "daily_tar_gz", "", "2026-04-13", "bucket", "key", ""),
            )
        )
        == []
    )


@pytest.mark.unit()
def test_archive_group_helpers_cover_chunk_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHIVER_ARCHIVE_GROUP_MAX_OBJECTS", "1")
    groups = archive_groups(
        (
            _entry("direct.txt", copy_mode="direct", target_day=None),
            _entry("missing-day.txt", target_day=None),
            _entry("data/b.xml"),
            _entry("data/a.xml"),
        )
    )

    assert [group.entries[0].key for group in groups] == ["data/a.xml", "data/b.xml"]
    assert list(archive_chunk_ranges([])) == []
    assert list(archive_chunk_ranges([1, 1])) == [(1, 0, 1), (2, 1, 1)]
    assert archive_chunk_key("archive.tar.gz", 2) == "archive.part-00002.tar.gz"
    assert archive_chunk_key("archive", 2) == "archive.part-00002"
    sizer = ArchiveChunkSizer()
    assert sizer.would_overflow(-1) is False
    sizer.add(-1)
    assert sizer.has_items is True


@pytest.mark.unit()
def test_sqlite_manifest_store_reaps_reader_connection_after_thread_exit(
    tmp_path: Path,
) -> None:
    store = SQLiteManifestStore(tmp_path / "reap.sqlite3")
    try:
        store.add_entry(_entry("data/one.xml"))
        store.commit()

        worker_ident: list[int] = []

        def worker() -> None:
            worker_ident.append(threading.get_ident())
            for _ in store.iter_entries():
                pass

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        worker_id = worker_ident[0]
        assert worker_id in store._reader_connections  # pyright: ignore[reportPrivateUsage]

        del thread
        _ = gc.collect()
        assert worker_id not in store._reader_connections  # pyright: ignore[reportPrivateUsage]

        store._reap_reader_connection(thread_id=-1)  # pyright: ignore[reportPrivateUsage]
    finally:
        store.cleanup()


@pytest.mark.unit()
def test_copy_route_helpers_fallback_without_sqlite_sequences() -> None:
    direct = _entry("raw/a.txt", copy_mode="direct", route_name="raw", target_day=None)
    daily = _entry("data/a.xml", route_name="daily")
    groups = archive_groups((daily,))

    assert [entry.key for entry in direct_entries_for_route((direct, daily), "raw")] == [
        "raw/a.txt"
    ]
    assert direct_entry_count((direct, daily)) == 1
    assert [group.route_name for group in archive_groups_for_route(groups, "daily")] == ["daily"]


def _entry(
    key: str,
    *,
    copy_mode: str = "daily_tar_gz",
    route_name: str = "daily",
    target_day: date | None = TARGET_DAY,
) -> ManifestEntry:
    listed = _listed(key, 1, "v1")
    destination_key = "copy/raw/a.txt" if copy_mode == "direct" else "2026-04-13"
    return ManifestEntry(
        source_bucket="source",
        key=key,
        size=listed.size,
        last_modified=listed.last_modified,
        etag=listed.etag,
        version_id=listed.version_id,
        object=listed,
        selected_timestamp=listed.last_modified,
        timestamp_source="last_modified",
        target_day=target_day,
        archive_root="",
        destination_archive_key=destination_key,
        route_name=route_name,
        parser_kind="filename_timestamp",
        copy_mode=cast(CopyMode, copy_mode),
        source_path="",
        destination_bucket="archive",
        destination_path="",
        destination_key=destination_key,
        source_identity=("source-id",),
        destination_identity=("destination-id",),
    )
