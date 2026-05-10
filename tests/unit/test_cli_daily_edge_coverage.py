"""Focused coverage tests for daily archive CLI payload edge cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from s3_archiver_cli import archive_payloads, error_logging, visual_demo_output
from s3_archiver_cli import cleanup_preview as preview_module
from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_archive_payload_helpers_cover_legacy_and_empty_fallbacks() -> None:
    entries = (
        SimpleNamespace(
            key="data/fae/old.xml",
            archive_root="data/fae",
            destination_archive_key="data/fae/2026-04-13.tar.gz",
        ),
    )
    manifest = SimpleNamespace(
        retention_cutoff_utc=None,
        entries=entries,
    )
    group = SimpleNamespace(
        target_day=datetime(2026, 4, 13, 7, tzinfo=UTC),
        destination_archive_keys=("data/fae/2026-04-13.tar.gz",),
        entries=(),
        skipped_objects=(),
    )

    assert archive_payloads.manifest_target_day(SimpleNamespace()) == ""
    assert archive_payloads.skipped_object_payloads(SimpleNamespace()) == []
    assert archive_payloads.object_list(object()) == []
    assert archive_payloads.archive_group_payload(group, manifest)["target_day"] == "2026-04-13"
    assert archive_payloads.archive_group_payload(group, manifest)["destination_archive_key"] == (
        "data/fae/2026-04-13.tar.gz"
    )
    assert (
        archive_payloads.archive_group_payload(
            SimpleNamespace(destination_archive_keys=(None,), entries=()),
            manifest,
        )["destination_archive_key"]
        == ""
    )

    payloads = archive_payloads.archive_group_payloads(manifest, cleanup_status="ok")

    assert payloads[0]["cleanup_status"] == "ok"
    assert payloads[0]["archive_root"] == "data/fae"

    fallback = archive_payloads.archive_group_payload(
        SimpleNamespace(entries=entries),
        manifest,
    )
    assert fallback["destination_archive_key"] == "data/fae/2026-04-13.tar.gz"


@pytest.mark.unit()
def test_cleanup_preview_private_payload_helpers_cover_fallback_shapes() -> None:
    manifest_target_day = cast(
        Callable[[object], str],
        _private_attr(preview_module, "_manifest_target_day"),
    )
    archive_group_payload = cast(
        Callable[[object, object], dict[str, preview_module.JsonValue]],
        _private_attr(preview_module, "_archive_group_payload"),
    )
    skipped_object_payloads = cast(
        Callable[[object], list[dict[str, preview_module.JsonValue]]],
        _private_attr(preview_module, "_skipped_object_payloads"),
    )
    object_list = cast(
        Callable[[object | None], list[object]],
        _private_attr(preview_module, "_object_list"),
    )

    manifest = SimpleNamespace(retention_cutoff_utc=datetime(2026, 4, 13, 2, tzinfo=UTC))
    group = SimpleNamespace(
        target_day=datetime(2026, 4, 13, 7, tzinfo=UTC),
        archive_keys=("data/fae/2026-04-13.tar.gz",),
        source_entries=(),
        skipped_entries=(),
    )

    assert manifest_target_day(manifest) == "2026-04-13"
    assert manifest_target_day(SimpleNamespace()) == ""
    assert skipped_object_payloads(SimpleNamespace()) == []
    assert object_list(object()) == []
    assert archive_group_payload(group, manifest)["target_day"] == "2026-04-13"
    assert archive_group_payload(group, manifest)["destination_archive_key"] == (
        "data/fae/2026-04-13.tar.gz"
    )
    assert (
        archive_group_payload(SimpleNamespace(archive_keys=(None,), entries=()), manifest)[
            "destination_archive_key"
        ]
        == ""
    )
    fallback = archive_group_payload(
        SimpleNamespace(entries=(SimpleNamespace(destination_archive_key="fallback.tar.gz"),)),
        manifest,
    )
    assert fallback["destination_archive_key"] == "fallback.tar.gz"


@pytest.mark.unit()
def test_archive_result_payload_marks_skipped_verified_and_unmatched_cleanup_groups(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    groups = (
        _group("data/skipped/2026-04-13.tar.gz"),
        _group("data/verified/2026-04-13.tar.gz"),
        _group("data/other/2026-04-13.tar.gz"),
    )
    manifest = SimpleNamespace(
        run_started_at_utc=datetime(2026, 4, 27, 2, tzinfo=UTC),
        retention_cutoff_utc=datetime(2026, 4, 13, 2, tzinfo=UTC),
        target_day=date(2026, 4, 13),
        entries=(),
        archive_groups=groups,
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
                cleanup=ArchivePhaseResult("cleanup"),
                skipped_archive_keys=("data/skipped/2026-04-13.tar.gz",),
                verified_archive_keys=("data/verified/2026-04-13.tar.gz",),
            ),
        ),
    )

    payload = error_logging.archive_result_payload("ok", result, settings, Path("/tmp/log"))
    payload_groups = cast(list[dict[str, object]], payload["archive_groups"])

    assert [group["cleanup_status"] for group in payload_groups] == ["skipped", "ok", "ok"]


@pytest.mark.unit()
def test_visual_demo_output_emits_cleanup_preview_skipped_objects() -> None:
    lines: list[str] = []

    visual_demo_output.emit_cleanup_preview(
        lines.append,
        {
            "cleanup_enabled_in_settings": False,
            "manifest_file": "/tmp/cleanup-preview.json",
            "target_day": "2026-04-13",
            "archive_count": 0,
            "object_count": 0,
            "archive_groups": [],
            "skipped_objects": [{"key": "latest.txt", "reason": "no reliable key timestamp"}],
            "entries": [],
        },
    )

    assert "SKIP   key=latest.txt reason=no reliable key timestamp" in lines


def _group(destination_archive_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        target_day=date(2026, 4, 13),
        archive_root="data",
        destination_archive_key=destination_archive_key,
        entries=(),
        skipped_objects=(),
    )


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))
