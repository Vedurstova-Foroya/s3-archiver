"""Shared helpers for CLI cleanup unit tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.archive_manifest import ArchiveManifest, ManifestEntry
from s3_archiver_core.cleanup_manifest import CleanupRecord, write_cleanup_manifest
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.settings import AppSettings

from tests.unit.archive_s3_fakes import FakeArchiveClient, client_error
from tests.unit.archive_workflow_fakes import listed_object as _listed

RUN_STARTED = "2026-04-20T00:00:00+00:00"


class FixedUuid:
    hex: str = "locked-run"


class RecordingLock:
    def __init__(self, _path: Path, **_kwargs: object) -> None:
        return

    def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
        _ = (run_id, run_started_at_utc, timeout)
        return True

    def release(self, *, run_id: str) -> None:
        _ = run_id


class RefusingLock:
    def __init__(self, _path: Path, **_kwargs: object) -> None:
        return

    def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
        _ = (run_id, run_started_at_utc, timeout)
        return False

    def release(self, *, run_id: str) -> None:
        raise AssertionError(f"unexpected release for {run_id}")


class DeletingClient(FakeArchiveClient):
    """Client that confirms deletion by reporting the object as gone."""

    deletes: list[dict[str, object]]

    def __init__(self, deletes: list[dict[str, object]]) -> None:
        super().__init__()
        self.deletes = deletes

    @override
    def head_object(self, **kwargs: object) -> dict[str, object]:
        _ = kwargs
        raise client_error("NoSuchKey")

    @override
    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.deletes.append(kwargs)
        return {}

    @override
    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        self.deletes.append(kwargs)
        return {}


def make_settings(tmp_path: Path, base_env: dict[str, str], *, cleanup: bool) -> AppSettings:
    env = {**base_env, "LOG_DIR": str(tmp_path / "logs")}
    if cleanup:
        env["CLEANUP"] = "true"
    return AppSettings.from_env(env)


def make_entry(settings: AppSettings) -> ManifestEntry:
    listed = _listed("data/a.xml", 1, "v1")
    route = settings.routes[0]
    return ManifestEntry(
        source_bucket=route.source.bucket,
        key="data/a.xml",
        size=10,
        last_modified=listed.last_modified,
        etag='"etag"',
        version_id="v1",
        object=listed,
        route_name=route.name,
        copy_mode="daily_tar_gz",
        destination_bucket=route.destination.bucket,
        destination_archive_key="data/2026-04-13.tar.gz",
        source_identity=route.source.storage_identity(),
    )


def make_result(
    settings: AppSettings, *, with_entry: bool, copy: ArchivePhaseResult | None = None
) -> ArchiveRunResult:
    entries = (make_entry(settings),) if with_entry else ()
    return ArchiveRunResult(
        run_id="locked-run",
        manifest=ArchiveManifest(
            run_started_at_utc=datetime(2026, 4, 20, tzinfo=UTC), entries=entries
        ),
        copy=copy or ArchivePhaseResult("copy"),
        verify=ArchivePhaseResult("verify"),
    )


def install_archive_mocks(
    monkeypatch: pytest.MonkeyPatch,
    result: ArchiveRunResult,
    build_client: Callable[[object], object],
    lock: type = RecordingLock,
) -> None:
    def run_health(_settings: AppSettings, _log_file: Path) -> object:
        return object()

    def run_core_archive(
        routes: tuple[object, ...], *, run_timeout: object, **_kwargs: object
    ) -> ArchiveRunResult:
        _ = (routes, run_timeout)
        return result

    monkeypatch.setattr(cli_module, "uuid4", FixedUuid)
    monkeypatch.setattr(cli_module, "FileArchiveRunLock", lock)
    monkeypatch.setattr(cli_module, "run_health_check", run_health)
    monkeypatch.setattr(cli_module, "build_s3_client", build_client)
    monkeypatch.setattr(cli_module, "run_archive", run_core_archive)


def record_status(log_dir: Path) -> str:
    record = cast(
        object, json.loads((log_dir / "archive-runs" / "locked-run.json").read_text("utf-8"))
    )
    assert isinstance(record, dict)
    return cast(str, cast(dict[str, object], record)["status"])


def build_deleting(deletes: list[dict[str, object]]) -> Callable[[object], object]:
    def build(_location: object) -> object:
        return DeletingClient(deletes)

    return build


def build_opaque(_location: object) -> object:
    return object()


def build_present(_location: object) -> object:
    client = FakeArchiveClient()
    client.delete_objects_error = client_error("NotImplemented", 501)
    return client


def ok_payload(
    _settings: AppSettings, _log_file: Path, _manifest: Path | None
) -> dict[str, JsonValue]:
    return {"status": "ok"}


def empty_payload(
    _settings: AppSettings, _log_file: Path, _manifest: Path | None
) -> dict[str, JsonValue]:
    return {"status": "empty", "message": "nothing"}


def write_pending(settings: AppSettings) -> None:
    record = CleanupRecord(
        route_name="default",
        source_identity=repr(settings.routes[0].source.storage_identity()),
        source_bucket="archive-bucket",
        key="data/a.xml",
        version_id="v1",
        size=10,
        etag='"etag"',
        destination_bucket="destination-bucket",
        destination_key="",
        destination_archive_key="data/2026-04-13.tar.gz",
        copy_mode="daily_tar_gz",
    )
    _ = write_cleanup_manifest(
        settings.cleanup_pending_dir / "run-1.jsonl",
        run_id="run-1",
        run_started_at_utc=RUN_STARTED,
        records=[record],
    )
