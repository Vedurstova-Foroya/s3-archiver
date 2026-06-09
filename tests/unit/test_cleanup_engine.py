"""Tests for the source-object cleanup engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from s3_archiver_core.archive_routes import ArchiveRoute
from s3_archiver_core.cleanup import run_cleanup
from s3_archiver_core.cleanup_manifest import CleanupRecord, write_cleanup_manifest
from s3_archiver_core.errors import CleanupManifestError
from s3_archiver_core.s3 import S3ObjectProperties

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes
from tests.unit.archive_workflow_fakes import listed_object as _listed

RUN_STARTED = "2026-04-20T00:00:00+00:00"


def _record(
    key: str,
    version_id: str | None = "v1",
    *,
    route_name: str = "default",
    source_identity: object = None,
) -> CleanupRecord:
    return CleanupRecord(
        route_name=route_name,
        source_identity=source_identity,
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


def _manifest(path: Path, records: list[CleanupRecord], *, run_id: str = "run-1") -> Path:
    _ = write_cleanup_manifest(path, run_id=run_id, run_started_at_utc=RUN_STARTED, records=records)
    return path


def _routes(source: FakeBucket) -> tuple[ArchiveRoute, ...]:
    return archive_routes(source, FakeBucket("destination"))


def _never() -> bool:
    return False


@pytest.mark.unit()
def test_full_success_deletes_verifies_and_retires_manifest(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"), _listed("data/b.xml", 1, "v2")))
    manifest = _manifest(
        tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1"), _record("data/b.xml", "v2")]
    )

    result = run_cleanup(
        _routes(source),
        manifests=[manifest],
        cleaned_dir=tmp_path / "cleaned",
        timed_out=_never,
    )

    assert result.empty is False
    assert result.ok is True
    assert result.cleaned_count == 2
    assert source.deleted == [("data/a.xml", "v1"), ("data/b.xml", "v2")]
    assert not manifest.exists()
    assert not (tmp_path / "cleaned" / "run-1.jsonl").exists()


@pytest.mark.unit()
def test_delete_failure_keeps_manifest_for_retry(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    source.fail_delete = True
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.cleaned_count == 0
    assert result.failures == ("data/a.xml: delete failed: delete failed",)
    assert manifest.exists()
    assert not (tmp_path / "cleaned" / "run-1.jsonl").exists()


@pytest.mark.unit()
def test_object_still_present_after_delete_is_failure(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    source.skip_actual_delete = True
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.failures == ("data/a.xml: still present after delete",)
    assert manifest.exists()


@pytest.mark.unit()
def test_verification_error_is_reported_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))

    def _boom(key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        _ = (key, version_id)
        raise RuntimeError("head exploded")

    monkeypatch.setattr(source, "head_object", _boom)
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.failures == ("data/a.xml: delete verification failed: head exploded",)


@pytest.mark.unit()
def test_unknown_route_is_failure(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    manifest = _manifest(
        tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1", route_name="ghost")]
    )

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.failures == ("data/a.xml: no configured route named 'ghost'",)
    assert source.deleted == []


@pytest.mark.unit()
def test_source_identity_mismatch_is_failure(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    manifest = _manifest(
        tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1", source_identity="other")]
    )

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.failures == ("data/a.xml: source identity mismatch for route 'default'",)
    assert source.deleted == []


@pytest.mark.unit()
def test_drains_every_pending_manifest(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"), _listed("data/b.xml", 1, "v2")))
    first = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1")], run_id="run-1")
    second = _manifest(tmp_path / "run-2.jsonl", [_record("data/b.xml", "v2")], run_id="run-2")

    result = run_cleanup(
        _routes(source),
        manifests=[first, second],
        cleaned_dir=tmp_path / "cleaned",
        timed_out=_never,
    )

    assert result.ok is True
    assert result.object_count == 2
    assert not first.exists()
    assert not second.exists()


@pytest.mark.unit()
def test_mangled_manifest_aborts_before_any_deletion(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    good = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1")], run_id="run-1")
    bad = tmp_path / "run-2.jsonl"
    _ = bad.write_text("{not a manifest", encoding="utf-8")

    with pytest.raises(CleanupManifestError):
        _ = run_cleanup(
            _routes(source),
            manifests=[good, bad],
            cleaned_dir=tmp_path / "cleaned",
            timed_out=_never,
        )

    assert source.deleted == []
    assert good.exists()


@pytest.mark.unit()
def test_no_manifests_is_empty(tmp_path: Path) -> None:
    source = FakeBucket("source")

    result = run_cleanup(
        _routes(source), manifests=[], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.empty is True
    assert result.outcomes == ()
    assert result.ok is True


@pytest.mark.unit()
def test_only_empty_manifest_is_empty(tmp_path: Path) -> None:
    source = FakeBucket("source")
    manifest = _manifest(tmp_path / "run-1.jsonl", [])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.empty is True


@pytest.mark.unit()
def test_timeout_stops_processing_and_keeps_manifest(tmp_path: Path) -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1")])

    result = run_cleanup(
        _routes(source),
        manifests=[manifest],
        cleaned_dir=tmp_path / "cleaned",
        timed_out=lambda: True,
    )

    assert result.ok is False
    assert result.failures == ("data/a.xml: cleanup run timed out",)
    assert source.deleted == []
    assert manifest.exists()
