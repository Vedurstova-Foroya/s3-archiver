"""Tests for the visual demo CLI workflow."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override

import pytest
import s3_archiver_visual_demo.walkthrough as demo_module
from s3_archiver_core.archive import ArchiveRoute
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.s3 import S3ListedObject
from s3_archiver_core.settings import AppSettings, S3LocationSettings

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed


def _demo_settings(base_env: dict[str, str], tmp_path: Path) -> AppSettings:
    settings = AppSettings.from_env(base_env)
    return replace(settings, temp_dir=tmp_path / "runtime-temp")


def _ok_archive_payload() -> dict[str, JsonValue]:
    return {
        "status": "ok",
        "phases": {
            "list": {"status": "ok", "failure_count": 0},
            "copy": {"status": "ok", "failure_count": 0},
            "verify": {"status": "ok", "failure_count": 0},
        },
    }


class FakeReport:
    def as_dict(self) -> dict[str, JsonValue]:
        return {"status": "ok", "checked_at": "2026-04-24T12:00:00+00:00"}


@pytest.mark.unit()
def test_run_visual_demo_reports_bucket_story(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    tmp_path: Path,
) -> None:
    settings = _demo_settings(base_env, tmp_path)
    source = FakeBucket(
        settings.routes[0].source.bucket,
        (
            _listed("demo/2024-02-20T00-00-00.txt", 61),
            _listed("demo/2024-04-21T00-00-00.txt", 1),
        ),
    )
    destination = SnapshotBucket(settings.routes[0].destination.bucket)

    def archive_runner(_settings: AppSettings, _log_file: Path) -> dict[str, JsonValue]:
        archived = _listed("demo/2024-02-20T00-00-00.txt", 61)
        metadata = {
            "s3-archiver-source-fingerprint": (
                '{"source_bucket":"source","source_key":"demo/2024-02-20T00-00-00.txt",'
                '"source_last_modified":"2024-02-19T00:00:00+00:00","source_size":10}'
            )
        }
        destination.objects = (
            replace(archived, properties=replace(archived.properties, metadata=metadata)),
        )
        return _ok_archive_payload()

    def fake_health_check(_settings: AppSettings, _log_file: Path) -> FakeReport:
        return FakeReport()

    def archive_routes(
        _settings: AppSettings,
        _build_client: Callable[[S3LocationSettings], object],
    ) -> tuple[ArchiveRoute, ...]:
        return (
            ArchiveRoute(
                "default",
                source,
                destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
            ),
        )

    lines: list[str] = []
    monkeypatch.setattr(demo_module, "run_health_check", fake_health_check)
    monkeypatch.setattr(demo_module, "archive_routes_from_settings", archive_routes)

    summary = demo_module.run_visual_demo(
        settings,
        settings.log_dir / "s3-archiver.log",
        archive_runner=archive_runner,
        emit=lines.append,
        now=lambda: datetime(2024, 4, 20, tzinfo=UTC),
    )

    assert summary["status"] == "ok"
    manifest = cast(dict[str, object], summary["archive_manifest"])
    assert manifest["source_object_count"] == 1
    snapshots = cast(dict[str, object], summary["snapshots"])
    before_archive = cast(dict[str, object], snapshots["before_archive"])
    after_archive = cast(dict[str, object], snapshots["after_archive"])
    assert before_archive["source_object_count"] == 2
    assert after_archive["destination_object_count"] == 1
    assert any("== Archive Candidates ==" in line for line in lines)
    assert lines.index("== Working Set ==") < lines.index("== Preflight ==")
    assert any("source_last_modified=2024-02-19T00:00:00+00:00" in line for line in lines)
    assert not any("== Cleanup Preview ==" in line for line in lines)
    assert json.loads(lines[-1])["status"] == "ok"


@pytest.mark.unit()
def test_run_visual_demo_uses_default_utc_clock(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    tmp_path: Path,
) -> None:
    settings = _demo_settings(base_env, tmp_path)

    class FrozenDateTime:
        @staticmethod
        def now(*, tz: object) -> datetime:
            _ = tz
            return datetime(2024, 4, 20, tzinfo=UTC)

    source = FakeBucket(settings.routes[0].source.bucket, (_listed("demo/old.txt", 61),))
    destination = SnapshotBucket(settings.routes[0].destination.bucket)

    def archive_runner(_settings: AppSettings, _log_file: Path) -> dict[str, JsonValue]:
        return _ok_archive_payload()

    def fake_health_check(_settings: AppSettings, _log_file: Path) -> FakeReport:
        return FakeReport()

    def archive_routes(
        _settings: AppSettings,
        _build_client: Callable[[S3LocationSettings], object],
    ) -> tuple[ArchiveRoute, ...]:
        return (
            ArchiveRoute(
                "default",
                source,
                destination,
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
            ),
        )

    monkeypatch.setattr(demo_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(demo_module, "run_health_check", fake_health_check)
    monkeypatch.setattr(demo_module, "archive_routes_from_settings", archive_routes)

    summary = demo_module.run_visual_demo(
        settings,
        settings.log_dir / "s3-archiver.log",
        archive_runner=archive_runner,
        emit=lambda _line: None,
    )

    assert summary["run_started_at_utc"] == "2024-04-20T00:00:00+00:00"


@pytest.mark.unit()
def test_run_visual_demo_reports_direct_entries(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    tmp_path: Path,
) -> None:
    settings = _demo_settings(base_env, tmp_path)
    source = FakeBucket(settings.routes[0].source.bucket, (_listed("raw/live.txt", 1),))
    destination = SnapshotBucket(settings.routes[0].destination.bucket)

    def archive_runner(_settings: AppSettings, _log_file: Path) -> dict[str, JsonValue]:
        return _ok_archive_payload()

    def fake_health_check(_settings: AppSettings, _log_file: Path) -> FakeReport:
        return FakeReport()

    def archive_routes(
        _settings: AppSettings,
        _build_client: Callable[[S3LocationSettings], object],
    ) -> tuple[ArchiveRoute, ...]:
        return (
            ArchiveRoute(
                "raw",
                source,
                destination,
                parser_kind="direct",
                copy_mode="direct",
            ),
        )

    lines: list[str] = []
    monkeypatch.setattr(demo_module, "run_health_check", fake_health_check)
    monkeypatch.setattr(demo_module, "archive_routes_from_settings", archive_routes)

    summary = demo_module.run_visual_demo(
        settings,
        settings.log_dir / "s3-archiver.log",
        archive_runner=archive_runner,
        emit=lines.append,
        now=lambda: datetime(2024, 4, 20, tzinfo=UTC),
    )

    manifest = cast(dict[str, object], summary["archive_manifest"])
    direct_entries = cast(list[dict[str, object]], manifest["direct_entries"])
    entries = cast(list[dict[str, object]], manifest["entries"])

    assert manifest["archive_count"] == 0
    assert manifest["direct_copy_count"] == 1
    assert manifest["destination_archive_keys"] == []
    assert manifest["destination_keys"] == ["raw/live.txt"]
    assert direct_entries[0]["destination_key"] == "raw/live.txt"
    assert entries[0]["parser_kind"] == "direct"
    assert entries[0]["copy_mode"] == "direct"
    assert any("DIRECT route=raw parser=direct copy_mode=direct" in line for line in lines)
    assert any("destination_key=raw/live.txt" in line for line in lines)
    assert any("parser=direct copy_mode=direct" in line for line in lines)


class SnapshotBucket(FakeBucket):
    """Archive bucket with mutable list output for visual demo snapshots."""

    objects: tuple[S3ListedObject, ...]

    def __init__(self, bucket: str, objects: tuple[S3ListedObject, ...] = ()) -> None:
        super().__init__(bucket)
        self.objects = objects

    @override
    def list_source_objects(
        self, versioning_state: str, *, prefix: str = ""
    ) -> tuple[S3ListedObject, ...]:
        assert versioning_state == self.versioning_state()
        return tuple(item for item in self.objects if item.key.startswith(prefix))
