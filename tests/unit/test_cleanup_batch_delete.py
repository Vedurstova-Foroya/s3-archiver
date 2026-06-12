"""Cleanup batch-delete coverage."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast, override

import pytest
import s3_archiver_core.cleanup as cleanup_module
from s3_archiver_core.archive_routes import ArchiveRoute
from s3_archiver_core.cleanup import run_cleanup
from s3_archiver_core.cleanup_manifest import CleanupRecord, write_cleanup_manifest
from s3_archiver_core.s3 import S3ListedObject, S3ObjectProperties, VersioningState
from s3_archiver_core.source_deletes import SourceDeleteFailure, SourceDeleteRequest

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes
from tests.unit.archive_workflow_fakes import listed_object as _listed

RUN_STARTED = "2026-04-20T00:00:00+00:00"


class BatchDeleteBucket(FakeBucket):
    batch_error: Exception | None
    batch_failures: tuple[SourceDeleteFailure, ...]
    batch_not_supported: bool
    delete_batches: list[tuple[SourceDeleteRequest, ...]]
    head_calls: list[tuple[str, str | None]]

    def __init__(
        self,
        bucket: str,
        objects: Iterable[S3ListedObject] = (),
        versions: Iterable[S3ListedObject] = (),
        destination: Mapping[str, S3ObjectProperties] | None = None,
        payloads: Mapping[str, bytes] | None = None,
        version_payloads: Mapping[tuple[str, str | None], bytes] | None = None,
        versioning_state: VersioningState = "Enabled",
        temp_dir: Path | None = None,
    ) -> None:
        super().__init__(
            bucket,
            objects,
            versions,
            destination,
            payloads,
            version_payloads,
            versioning_state,
            temp_dir,
        )
        self.batch_error = None
        self.batch_failures = ()
        self.batch_not_supported = False
        self.delete_batches = []
        self.head_calls = []

    @override
    def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        self.head_calls.append((key, version_id))
        return super().head_object(key, version_id)

    def delete_source_objects(
        self, objects: Sequence[SourceDeleteRequest]
    ) -> Sequence[SourceDeleteFailure]:
        if self.batch_not_supported:
            raise NotImplementedError("batch delete is not supported")
        if self.batch_error is not None:
            raise self.batch_error
        batch = tuple(objects)
        self.delete_batches.append(batch)
        failed = {(failure.key, failure.version_id) for failure in self.batch_failures}
        for item in batch:
            if (item.key, item.version_id) not in failed:
                self.deleted.append((item.key, item.version_id))
                _ = self._versions.pop((item.key, item.version_id), None)
        return self.batch_failures


def _record(
    key: str,
    version_id: str | None = "v1",
    *,
    route_name: str = "default",
    source_bucket: str = "source",
) -> CleanupRecord:
    return CleanupRecord(
        route_name=route_name,
        source_identity=None,
        source_bucket=source_bucket,
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


def _route(name: str, source: FakeBucket) -> ArchiveRoute:
    return ArchiveRoute(
        name,
        source,
        FakeBucket("destination"),
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
        source_path="",
        destination_path="",
    )


def _never() -> bool:
    return False


@pytest.mark.unit()
def test_cleanup_batches_versioned_deletes_without_per_object_head(tmp_path: Path) -> None:
    source = BatchDeleteBucket(
        "source", (_listed("data/a.xml", 1, "v1"), _listed("data/b.xml", 1, "v2"))
    )
    manifest = _manifest(
        tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1"), _record("data/b.xml", "v2")]
    )

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is True
    assert result.cleaned_count == 2
    assert source.delete_batches == [
        (SourceDeleteRequest("data/a.xml", "v1"), SourceDeleteRequest("data/b.xml", "v2"))
    ]
    assert source.head_calls == []
    assert not manifest.exists()


@pytest.mark.unit()
def test_cleanup_batch_delete_reports_per_key_errors(tmp_path: Path) -> None:
    source = BatchDeleteBucket(
        "source", (_listed("data/a.xml", 1, "v1"), _listed("data/b.xml", 1, "v2"))
    )
    source.batch_failures = (
        SourceDeleteFailure("data/b.xml", None, "delete failed: AccessDenied: denied"),
    )
    manifest = _manifest(
        tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1"), _record("data/b.xml", "v2")]
    )

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.cleaned_count == 1
    assert result.failures == ("data/b.xml: delete failed: AccessDenied: denied",)
    assert manifest.exists()


@pytest.mark.unit()
def test_cleanup_falls_back_when_batch_delete_is_unsupported(tmp_path: Path) -> None:
    source = BatchDeleteBucket("source", (_listed("data/a.xml", 1, "v1"),))
    source.batch_not_supported = True
    manifest = _manifest(tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1")])

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is True
    assert result.cleaned_count == 1
    assert source.delete_batches == []
    assert source.deleted == [("data/a.xml", "v1")]
    assert source.head_calls == [("data/a.xml", "v1")]


@pytest.mark.unit()
def test_cleanup_flushes_batch_when_route_changes(tmp_path: Path) -> None:
    source_a = BatchDeleteBucket("source-a", (_listed("data/a.xml", 1, "v1"),))
    source_b = BatchDeleteBucket("source-b", (_listed("data/b.xml", 1, "v2"),))
    manifest = _manifest(
        tmp_path / "run-1.jsonl",
        [
            _record("data/a.xml", "v1", route_name="a", source_bucket="source-a"),
            _record("data/b.xml", "v2", route_name="b", source_bucket="source-b"),
        ],
    )

    result = run_cleanup(
        (_route("a", source_a), _route("b", source_b)),
        manifests=[manifest],
        cleaned_dir=tmp_path / "cleaned",
        timed_out=_never,
    )

    assert result.ok is True
    assert source_a.delete_batches == [(SourceDeleteRequest("data/a.xml", "v1"),)]
    assert source_b.delete_batches == [(SourceDeleteRequest("data/b.xml", "v2"),)]


@pytest.mark.unit()
def test_cleanup_flushes_batch_at_delete_objects_limit(tmp_path: Path) -> None:
    records = [_record(f"data/{index}.xml", f"v{index}") for index in range(1000)]
    source = BatchDeleteBucket(
        "source", (_listed(record.key, 1, record.version_id) for record in records)
    )
    manifest = _manifest(tmp_path / "run-1.jsonl", records)

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is True
    assert result.cleaned_count == 1000
    assert [len(batch) for batch in source.delete_batches] == [1000]


@pytest.mark.unit()
def test_cleanup_batch_failure_falls_back_to_serial_when_source_lacks_batch_api() -> None:
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    route = _route("default", source)
    batch_delete_failures = cast(
        Callable[
            [ArchiveRoute, Sequence[CleanupRecord]],
            dict[tuple[str, str | None], SourceDeleteFailure],
        ],
        vars(cleanup_module)["_batch_delete_failures"],
    )

    failures = batch_delete_failures(route, [_record("data/a.xml", "v1")])

    assert failures == {}
    assert source.deleted == [("data/a.xml", "v1")]


@pytest.mark.unit()
def test_cleanup_batch_delete_reports_unexpected_batch_exception(tmp_path: Path) -> None:
    source = BatchDeleteBucket(
        "source", (_listed("data/a.xml", 1, "v1"), _listed("data/b.xml", 1, "v2"))
    )
    source.batch_error = RuntimeError("service unavailable")
    manifest = _manifest(
        tmp_path / "run-1.jsonl", [_record("data/a.xml", "v1"), _record("data/b.xml", "v2")]
    )

    result = run_cleanup(
        _routes(source), manifests=[manifest], cleaned_dir=tmp_path / "cleaned", timed_out=_never
    )

    assert result.ok is False
    assert result.cleaned_count == 0
    assert result.failures == (
        "data/a.xml: delete failed: service unavailable",
        "data/b.xml: delete failed: service unavailable",
    )
