"""Route archive manifest size-limit tests."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from s3_archiver_core.archive_manifest import ArchiveManifestRoute, build_route_archive_manifest
from s3_archiver_core.s3 import S3ListedObject

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


@pytest.mark.unit()
def test_route_manifest_skips_source_objects_above_configured_size_limit(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    monkeypatch.setenv("ARCHIVER_MAX_SOURCE_OBJECT_SIZE_MIB", "1")
    source = FakeBucket("source", (_large_listed("raw/large.bin", size=2 * 1024 * 1024),))

    with caplog.at_level(logging.WARNING, logger="s3_archiver.archive"):
        manifest = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "raw",
                    source,
                    FakeBucket("archive"),
                    parser_kind="direct",
                    copy_mode="direct",
                ),
            ),
            run_started_at_utc=STARTED,
        )

    assert len(manifest.entries) == 0
    assert len(manifest.skipped_objects) == 1
    assert manifest.skipped_objects[0].key == "raw/large.bin"
    assert manifest.skipped_objects[0].reason == (
        "source object size 2097152 exceeds max source object size 1048576"
    )
    assert any(
        getattr(record, "event", None) == "archive.object.skipped"
        and cast(dict[str, object], record.__dict__)["source_key"] == "raw/large.bin"
        for record in caplog.records
    )


@pytest.mark.unit()
def test_route_manifest_skips_archive_groups_above_configured_archive_size_limit(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    monkeypatch.setenv("ARCHIVER_MAX_DESTINATION_ARCHIVE_SIZE_MIB", "1")
    source = FakeBucket(
        "source", (_large_listed("data/fae/2026-04-13T03-00-00Z.xml", size=2 * 1024 * 1024),)
    )

    with caplog.at_level(logging.WARNING, logger="s3_archiver.archive"):
        manifest = build_route_archive_manifest(
            (
                ArchiveManifestRoute(
                    "fae",
                    source,
                    FakeBucket("archive"),
                    parser_kind="filename_timestamp",
                    copy_mode="daily_tar_gz",
                ),
            ),
            run_started_at_utc=STARTED,
        )

    assert len(manifest.entries) == 0
    assert len(manifest.archive_groups) == 0
    assert len(manifest.skipped_objects) == 1
    assert manifest.skipped_objects[0].key == "data/fae/2026-04-13T03-00-00Z.xml"
    assert manifest.skipped_objects[0].reason == (
        "estimated destination archive size 3147264 exceeds max destination archive size 1048576"
    )
    assert any(
        getattr(record, "event", None) == "archive.archive_group.skipped"
        and cast(dict[str, object], record.__dict__)["destination_key"]
        == "data/fae/2026-04-13.tar.gz"
        for record in caplog.records
    )


@pytest.mark.unit()
def test_route_manifest_uses_default_source_size_limit_for_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHIVER_MAX_SOURCE_OBJECT_SIZE_MIB", "invalid")

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "raw",
                FakeBucket("source", (_large_listed("raw/large.bin", size=2 * 1024 * 1024),)),
                FakeBucket("archive"),
                parser_kind="direct",
                copy_mode="direct",
            ),
        ),
        run_started_at_utc=STARTED,
    )

    assert len(manifest.entries) == 1
    assert len(manifest.skipped_objects) == 0


@pytest.mark.unit()
def test_route_manifest_keeps_in_policy_groups_when_others_exceed_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHIVER_MAX_DESTINATION_ARCHIVE_SIZE_MIB", "2")
    source = FakeBucket(
        "source",
        (
            _large_listed("data/fae/2026-04-12T03-00-00Z.xml", size=4 * 1024 * 1024),
            _listed("data/fae/2026-04-13T03-00-00Z.xml", 1, "v2"),
        ),
    )

    manifest = build_route_archive_manifest(
        (
            ArchiveManifestRoute(
                "fae",
                source,
                FakeBucket("archive"),
                parser_kind="filename_timestamp",
                copy_mode="daily_tar_gz",
            ),
        ),
        run_started_at_utc=STARTED,
    )

    assert manifest.manifest_storage == "sqlite"
    assert manifest.store is None
    assert [entry.key for entry in manifest.entries] == ["data/fae/2026-04-13T03-00-00Z.xml"]
    assert [group.destination_archive_key for group in manifest.archive_groups] == [
        "data/fae/2026-04-13.tar.gz"
    ]
    assert {item.key for item in manifest.skipped_objects} == {"data/fae/2026-04-12T03-00-00Z.xml"}


def _large_listed(key: str, *, size: int) -> S3ListedObject:
    listed = _listed(key, 1, "v1")
    return replace(listed, size=size, properties=replace(listed.properties, size=size))


def _isolate_archive_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("s3_archiver")
    for handler in logger.handlers:
        handler.close()
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
