"""Tests for daily archive CLI reporting payloads."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import s3_archiver_cli.cleanup_preview as preview_module
import s3_archiver_cli.error_logging as error_logging
import s3_archiver_cli.visual_demo as demo_module
from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.archive_manifest import ArchiveManifest
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_archive_result_payload_reports_daily_archive_groups(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    entry = SimpleNamespace(
        key="data/fae/2026/04/13/07/2026-04-13T07-00-00.xml",
        version_id="v1",
        size=123,
        destination_archive_key="data/fae/2026-04-13.tar.gz",
    )
    skipped = SimpleNamespace(
        key="data/fae/no-timestamp.xml",
        reason="no timestamp in key",
        target_day="2026-04-13",
        archive_root="data/fae",
    )
    group = SimpleNamespace(
        target_day=date(2026, 4, 13),
        archive_root="data/fae",
        destination_archive_key="data/fae/2026-04-13.tar.gz",
        entries=(entry,),
        skipped_objects=(skipped,),
        cleanup_status="verified_cleanup_skipped",
    )
    manifest = SimpleNamespace(
        run_started_at_utc=datetime(2026, 4, 27, 2, tzinfo=UTC),
        retention_cutoff_utc=datetime(2026, 4, 13, 2, tzinfo=UTC),
        target_day=date(2026, 4, 13),
        entries=(entry,),
        archive_groups=(group,),
        skipped_objects=(skipped,),
    )
    result = cast(
        ArchiveRunResult,
        cast(
            object,
            SimpleNamespace(
                run_id="run-id",
                manifest=manifest,
                list=ArchivePhaseResult("list"),
                copy=ArchivePhaseResult("copy"),
                verify=ArchivePhaseResult("verify"),
                cleanup=ArchivePhaseResult("cleanup", skipped=True),
            ),
        ),
    )

    payload = error_logging.archive_result_payload("ok", result, settings, Path("/tmp/log"))

    assert payload["target_day"] == "2026-04-13"
    assert payload["archive_count"] == 1
    assert payload["source_object_count"] == 1
    assert payload["skipped_object_count"] == 1
    assert payload["destination_archive_keys"] == ["data/fae/2026-04-13.tar.gz"]
    groups = cast(list[dict[str, object]], payload["archive_groups"])
    assert groups[0]["cleanup_status"] == "verified_cleanup_skipped"
    assert groups[0]["source_object_count"] == 1
    assert groups[0]["skipped_object_count"] == 1


@pytest.mark.unit()
def test_cleanup_preview_payload_reports_daily_group_and_skips(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    entry = SimpleNamespace(
        source_bucket=settings.source.bucket,
        key="data/harmonie/file-2026-04-13T000000Z.bz2",
        version_id=None,
        size=456,
        etag='"etag"',
        last_modified=datetime(2026, 4, 13, tzinfo=UTC),
        destination_archive_key="data/harmonie/2026-04-13.tar.gz",
    )
    skipped = SimpleNamespace(key="data/harmonie/latest.bz2", reason="no timestamp in key")
    manifest = SimpleNamespace(
        run_started_at_utc=datetime(2026, 4, 27, 2, tzinfo=UTC),
        retention_cutoff_utc=datetime(2026, 4, 13, 2, tzinfo=UTC),
        target_day="2026-04-13",
        entries=(entry,),
        skipped_objects=(skipped,),
    )

    cleanup_preview_payload = cast(
        Callable[[AppSettings, Path, ArchiveManifest], dict[str, preview_module.JsonValue]],
        _private_attr(preview_module, "_cleanup_preview_payload"),
    )

    payload = cleanup_preview_payload(
        settings,
        Path("/tmp/log"),
        cast(ArchiveManifest, cast(object, manifest)),
    )

    preview = cast(dict[str, object], payload["cleanup_preview"])
    assert preview["target_day"] == "2026-04-13"
    assert preview["archive_count"] == 1
    assert preview["source_object_count"] == 1
    assert preview["skipped_object_count"] == 1
    assert preview["destination_archive_keys"] == ["data/harmonie/2026-04-13.tar.gz"]
    groups = cast(list[dict[str, object]], preview["archive_groups"])
    assert groups[0]["destination_archive_key"] == "data/harmonie/2026-04-13.tar.gz"
    assert groups[0]["cleanup_status"] == "skipped"
    skipped_objects = cast(list[dict[str, object]], preview["skipped_objects"])
    assert skipped_objects[0]["reason"] == "no timestamp in key"


@pytest.mark.unit()
def test_visual_demo_describes_daily_candidates_and_result_groups() -> None:
    entry = SimpleNamespace(
        key="data/fae/2026-04-13T07-00-00.xml",
        version_id="v1",
        size=123,
        last_modified=datetime(2026, 4, 13, 7, tzinfo=UTC),
        etag='"etag"',
        source_bucket="source-bucket",
        destination_archive_key="data/fae/2026-04-13.tar.gz",
    )
    manifest = SimpleNamespace(
        run_started_at_utc=datetime(2026, 4, 27, 2, tzinfo=UTC),
        retention_cutoff_utc=datetime(2026, 4, 13, 2, tzinfo=UTC),
        target_day="2026-04-13",
        entries=(entry,),
        skipped_objects=(SimpleNamespace(key="bad.txt", reason="no timestamp"),),
    )
    archive_payload: dict[str, demo_module.JsonValue] = {
        "status": "ok",
        "target_day": "2026-04-13",
        "archive_count": 1,
        "archive_groups": [
            {
                "destination_archive_key": "data/fae/2026-04-13.tar.gz",
                "source_object_count": 1,
                "skipped_object_count": 1,
                "cleanup_status": "skipped",
            }
        ],
        "phases": {
            "list": {"status": "ok", "failure_count": 0},
            "copy": {"status": "ok", "failure_count": 0},
            "verify": {"status": "ok", "failure_count": 0},
            "cleanup": {"status": "skipped", "failure_count": 0},
        },
    }
    lines: list[str] = []

    emit_manifest = cast(
        Callable[[demo_module.Emitter, ArchiveManifest], None],
        _private_attr(demo_module, "_emit_manifest"),
    )
    emit_archive_result = cast(
        Callable[[demo_module.Emitter, dict[str, demo_module.JsonValue]], None],
        _private_attr(demo_module, "_emit_archive_result"),
    )
    emit_cleanup_preview = cast(
        Callable[[demo_module.Emitter, dict[str, demo_module.JsonValue]], None],
        _private_attr(demo_module, "_emit_cleanup_preview"),
    )

    emit_manifest(lines.append, cast(ArchiveManifest, cast(object, manifest)))
    emit_archive_result(lines.append, archive_payload)
    emit_cleanup_preview(
        lines.append,
        {
            "cleanup_enabled_in_settings": False,
            "manifest_file": "/tmp/cleanup-preview.json",
            "target_day": "2026-04-13",
            "archive_count": 1,
            "object_count": 1,
            "archive_groups": [
                {
                    "destination_archive_key": "data/fae/2026-04-13.tar.gz",
                    "source_object_count": 1,
                    "skipped_object_count": 0,
                    "cleanup_status": "skipped",
                }
            ],
            "skipped_objects": [],
            "entries": [],
        },
    )

    assert any(line == "target day: 2026-04-13" for line in lines)
    assert any("destination_archive_key=data/fae/2026-04-13.tar.gz" in line for line in lines)
    assert any("cleanup_status=skipped" in line for line in lines)
    assert any("skipped_object_count=0 cleanup_status=skipped" in line for line in lines)
    assert any("SKIP   key=bad.txt reason=no timestamp" in line for line in lines)


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))
