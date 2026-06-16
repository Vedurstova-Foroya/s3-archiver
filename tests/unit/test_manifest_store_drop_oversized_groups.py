"""Store-native oversized archive-group drop coverage.

Exercises ``SQLiteManifestStore.drop_oversized_groups`` directly so the
``dropped == 0`` short-circuit (unreachable through the builder, which only
calls the drop once a group is already known to exceed the limit) and the
multi-chunk reverse-order deletion are both covered. The kept/skipped sets
mirror the retired in-memory ``filter_archive_groups_by_size`` behavior.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core._archive_manifest_models import CopyMode, ManifestEntry
from s3_archiver_core._archive_manifest_store import SQLiteManifestStore

from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)
DAY_KEEP = date(2026, 4, 13)
DAY_DROP = date(2026, 4, 14)
_SMALL_SIZE = 1
_BIG_SIZE = 4 * 1024 * 1024
_LIMIT = 2 * 1024 * 1024
_BIG_REASON = (
    "estimated destination archive size 5244416 exceeds max destination archive size 2097152"
)


@pytest.mark.unit()
def test_drop_oversized_groups_keeps_every_under_limit_group(tmp_path: Path) -> None:
    store = SQLiteManifestStore(tmp_path / "under.sqlite3")
    try:
        store.add_entry(_entry("data/keep.xml", size=_SMALL_SIZE, target_day=DAY_KEEP))
        store.add_entry(_entry("data/other.xml", size=_SMALL_SIZE, target_day=DAY_DROP))
        store.commit()

        assert store.drop_oversized_groups(_LIMIT) == 0
        assert len(store.entries) == 2
        assert len(store.skipped_objects) == 0
        assert store.group_count() == 2
    finally:
        store.cleanup()


@pytest.mark.unit()
def test_drop_oversized_groups_removes_over_limit_group_and_keeps_others(tmp_path: Path) -> None:
    store = SQLiteManifestStore(tmp_path / "mixed.sqlite3")
    try:
        store.add_entry(_entry("data/keep.xml", size=_SMALL_SIZE, target_day=DAY_KEEP))
        store.add_entry(_entry("data/big.xml", size=_BIG_SIZE, target_day=DAY_DROP))
        store.commit()

        assert store.drop_oversized_groups(_LIMIT) == 1
        assert [entry.key for entry in store.entries] == ["data/keep.xml"]
        assert store.group_count() == 1
        assert store.archive_groups[0].destination_archive_key == "2026-04-13"
        assert [entry.key for entry in store.archive_groups[0].entries] == ["data/keep.xml"]

        skipped = list(store.skipped_objects)
        assert [item.key for item in skipped] == ["data/big.xml"]
        assert skipped[0].reason == _BIG_REASON
        assert skipped[0].size == _BIG_SIZE
        assert skipped[0].route_name == "daily"
        assert skipped[0].copy_mode == "daily_tar_gz"
    finally:
        store.cleanup()


@pytest.mark.unit()
def test_drop_oversized_groups_handles_multi_chunk_partial_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHIVER_ARCHIVE_GROUP_MAX_OBJECTS", "1")
    store = SQLiteManifestStore(tmp_path / "multi-chunk.sqlite3")
    try:
        # One logical group split into three single-entry chunks ordered by key:
        # [big, small, big]. Reverse-order deletion must drop both big chunks
        # without shifting the surviving small chunk's slice.
        store.add_entry(_entry("data/a-big.xml", size=_BIG_SIZE, target_day=DAY_KEEP))
        store.add_entry(_entry("data/b-small.xml", size=_SMALL_SIZE, target_day=DAY_KEEP))
        store.add_entry(_entry("data/c-big.xml", size=_BIG_SIZE, target_day=DAY_KEEP))
        store.commit()
        assert store.group_count() == 3

        assert store.drop_oversized_groups(_LIMIT) == 2
        assert [entry.key for entry in store.entries] == ["data/b-small.xml"]
        assert store.group_count() == 1
        assert [entry.key for entry in store.archive_groups[0].entries] == ["data/b-small.xml"]

        skipped = list(store.skipped_objects)
        assert {item.key for item in skipped} == {"data/a-big.xml", "data/c-big.xml"}
        assert {item.reason for item in skipped} == {_BIG_REASON}
    finally:
        store.cleanup()


def _entry(
    key: str,
    *,
    size: int,
    target_day: date,
    route_name: str = "daily",
    copy_mode: str = "daily_tar_gz",
) -> ManifestEntry:
    listed = _listed(key, 1, key)
    destination_key = target_day.isoformat()
    return ManifestEntry(
        source_bucket="source",
        key=key,
        size=size,
        last_modified=listed.last_modified,
        etag=listed.etag,
        version_id=key,
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
