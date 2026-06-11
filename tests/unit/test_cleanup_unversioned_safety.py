"""Tests for unversioned cleanup source-object safety checks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from s3_archiver_core.archive_routes import ArchiveRoute
from s3_archiver_core.cleanup import run_cleanup
from s3_archiver_core.cleanup_manifest import CleanupRecord, write_cleanup_manifest
from s3_archiver_core.s3 import S3ObjectProperties

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes
from tests.unit.archive_workflow_fakes import listed_object as _listed

RUN_STARTED = "2026-04-20T00:00:00+00:00"


def _record(key: str, version_id: str | None = None) -> CleanupRecord:
    return CleanupRecord(
        route_name="default",
        source_identity=None,
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


def _manifest(path: Path, records: list[CleanupRecord]) -> Path:
    _ = write_cleanup_manifest(
        path, run_id="run-1", run_started_at_utc=RUN_STARTED, records=records
    )
    return path


def _routes(source: FakeBucket) -> tuple[ArchiveRoute, ...]:
    return archive_routes(source, FakeBucket("destination"))


def _never() -> bool:
    return False


@pytest.mark.unit()
def test_unversioned_delete_checks_current_object_before_delete(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, None),))
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is True
    assert result.cleaned_count == 1
    assert source.deleted == [("data/a.xml", None)]
    assert source.delete_conditions == ['"etag"']
    assert not manifest.exists()


@pytest.mark.unit()
def test_unversioned_cleanup_refuses_overwritten_source_object(tmp_path: Path) -> None:
    listed = _listed("data/a.xml", 1, None)
    overwritten = replace(
        listed,
        size=11,
        etag='"new-etag"',
        properties=replace(listed.properties, size=11, etag='"new-etag"'),
    )
    source = FakeBucket("source", (overwritten,))
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.cleaned_count == 0
    expected_failure = (
        "data/a.xml: current unversioned source object differs from cleanup manifest "
        + "(manifest etag='\"etag\"', size=10; current etag='\"new-etag\"', size=11)"
    )
    assert result.failures == (expected_failure,)
    assert source.deleted == []
    assert manifest.exists()


@pytest.mark.unit()
def test_unversioned_cleanup_treats_already_missing_object_as_cleaned(tmp_path: Path) -> None:
    source = FakeBucket("source")
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is True
    assert result.cleaned_count == 1
    assert source.deleted == []
    assert not manifest.exists()


@pytest.mark.unit()
def test_unversioned_source_verification_error_prevents_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, None),))

    def _boom(key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        _ = (key, version_id)
        raise RuntimeError("head exploded")

    monkeypatch.setattr(source, "head_object", _boom)
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.failures == (
        "data/a.xml: source verification failed before delete: head exploded",
    )
    assert source.deleted == []
