"""Tests for cleanup-input manifest serialization and the empty/valid states."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from s3_archiver_core.archive_manifest import ManifestEntry
from s3_archiver_core.cleanup_manifest import (
    CleanupManifestStatus,
    CleanupRecord,
    cleanup_record_from_entry,
    iter_cleanup_records,
    validate_cleanup_manifest,
    write_cleanup_manifest,
)

from tests.unit.archive_workflow_fakes import listed_object as _listed

RUN_STARTED = "2026-04-20T00:00:00+00:00"


def _record(key: str = "data/a.xml", version_id: str | None = "v1") -> CleanupRecord:
    return CleanupRecord(
        route_name="default",
        source_identity="src-identity",
        source_bucket="source",
        key=key,
        version_id=version_id,
        size=10,
        etag='"etag"',
        destination_bucket="destination",
        destination_key="",
        destination_archive_key="data/2026-04-13.tar.gz",
        copy_mode="daily_tar_gz",
    )


@pytest.mark.unit()
def test_write_validate_and_iter_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    records = [_record("data/a.xml", "v1"), _record("data/b.xml", None)]

    summary = write_cleanup_manifest(
        path, run_id="run-1", run_started_at_utc=RUN_STARTED, records=records
    )

    assert summary.status is CleanupManifestStatus.VALID
    assert summary.object_count == 2
    assert validate_cleanup_manifest(path) == summary
    assert list(iter_cleanup_records(path)) == records


@pytest.mark.unit()
def test_validate_and_iter_stream_without_reading_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run-1.jsonl"
    records = [_record("data/a.xml", "v1"), _record("data/b.xml", None)]
    summary = write_cleanup_manifest(
        path, run_id="run-1", run_started_at_utc=RUN_STARTED, records=records
    )

    def fail_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        _ = (self, encoding, errors)
        raise AssertionError("cleanup manifests must be streamed")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    assert validate_cleanup_manifest(path) == summary
    assert list(iter_cleanup_records(path)) == records


@pytest.mark.unit()
def test_cleanup_record_from_entry_maps_source_coordinates() -> None:
    listed = _listed("data/fae/2026/04/13/2026-04-13T07-00-00.xml", 1, "v9")
    entry = ManifestEntry(
        source_bucket="source",
        key=listed.key,
        size=listed.size,
        last_modified=listed.last_modified,
        etag=listed.etag,
        version_id=listed.version_id,
        object=listed,
        route_name="daily",
        copy_mode="daily_tar_gz",
        destination_bucket="destination",
        destination_archive_key="data/fae/2026-04-13.tar.gz",
    )

    record = cleanup_record_from_entry(entry)

    assert record.key == listed.key
    assert record.version_id == "v9"
    assert record.route_name == "daily"
    assert record.destination_archive_key == "data/fae/2026-04-13.tar.gz"
    assert record.source_identity is None


@pytest.mark.unit()
def test_missing_manifest_is_empty(tmp_path: Path) -> None:
    summary = validate_cleanup_manifest(tmp_path / "absent.jsonl")

    assert summary.status is CleanupManifestStatus.EMPTY
    assert summary.object_count == 0


@pytest.mark.unit()
def test_zero_object_manifest_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    summary = write_cleanup_manifest(
        path, run_id="run-1", run_started_at_utc=RUN_STARTED, records=[]
    )

    assert summary.status is CleanupManifestStatus.EMPTY
    assert validate_cleanup_manifest(path).status is CleanupManifestStatus.EMPTY
    assert list(iter_cleanup_records(path)) == []


@pytest.mark.unit()
def test_empty_byte_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _ = path.write_text("", encoding="utf-8")

    assert validate_cleanup_manifest(path).status is CleanupManifestStatus.EMPTY


@pytest.mark.unit()
def test_write_cleans_up_temp_file_when_records_raise(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"

    def _exploding_records() -> Iterator[CleanupRecord]:
        yield _record("data/a.xml")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _ = write_cleanup_manifest(
            path, run_id="run-1", run_started_at_utc=RUN_STARTED, records=_exploding_records()
        )

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
