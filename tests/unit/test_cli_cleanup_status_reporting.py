"""Tests for archive cleanup status reporting edge cases."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from s3_archiver_cli import error_logging
from s3_archiver_cli.archive_cleanup_status import (
    apply_group_cleanup_statuses,
    failure_key,
    mismatch_payload,
    payload_cleanup_known_keys,
)
from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_archive_result_payload_scopes_partial_cleanup_failures_to_source_group(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    skipped_key = "data/skipped/2026-04-13.tar.gz"
    ok_key = "data/ok/2026-04-13.tar.gz"
    failed_key = "data/failed/2026-04-13.tar.gz"
    other_key = "data/other/2026-04-13.tar.gz"
    failed_source_key = "data/failed/2026/04/13/object.xml"
    result = _result(
        _manifest(
            _group(skipped_key, "data/skipped/2026/04/13/object.xml"),
            _group(ok_key, "data/ok/2026/04/13/object.xml"),
            _group(failed_key, failed_source_key),
            _group(other_key, "data/other/2026/04/13/object.xml"),
        ),
        cleanup=ArchivePhaseResult("cleanup", (f"{failed_source_key}: AccessDenied",)),
        skipped_archive_keys=(skipped_key,),
        verified_archive_keys=(ok_key, failed_key, other_key),
    )

    payload = error_logging.archive_result_payload("error", result, settings, Path("/tmp/log"))
    payload_groups = cast(list[dict[str, object]], payload["archive_groups"])

    assert [group["cleanup_status"] for group in payload_groups] == [
        "skipped",
        "ok",
        "error",
        "ok",
    ]


@pytest.mark.unit()
def test_archive_result_payload_keeps_unscoped_cleanup_failures_global(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    skipped_key = "data/skipped/2026-04-13.tar.gz"
    verified_key = "data/verified/2026-04-13.tar.gz"
    result = _result(
        _manifest(_group(skipped_key), _group(verified_key)),
        cleanup=ArchivePhaseResult("cleanup", ("archive run timed out",)),
        skipped_archive_keys=(skipped_key,),
        verified_archive_keys=(verified_key,),
    )

    payload = error_logging.archive_result_payload("error", result, settings, Path("/tmp/log"))
    payload_groups = cast(list[dict[str, object]], payload["archive_groups"])

    assert [group["cleanup_status"] for group in payload_groups] == ["skipped", "error"]


@pytest.mark.unit()
def test_archive_result_payload_keeps_mixed_unscoped_cleanup_failures_global(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    skipped_key = "data/skipped/2026-04-13.tar.gz"
    scoped_failed_key = "data/scoped/2026-04-13.tar.gz"
    global_failed_key = "data/global/2026-04-13.tar.gz"
    scoped_source_key = "data/scoped/2026/04/13/object.xml"
    result = _result(
        _manifest(
            _group(skipped_key, "data/skipped/2026/04/13/object.xml"),
            _group(scoped_failed_key, scoped_source_key),
            _group(global_failed_key, "data/global/2026/04/13/object.xml"),
        ),
        cleanup=ArchivePhaseResult(
            "cleanup",
            (f"{scoped_source_key}: AccessDenied", "archive run timed out"),
        ),
        skipped_archive_keys=(skipped_key,),
        verified_archive_keys=(scoped_failed_key, global_failed_key),
    )

    payload = error_logging.archive_result_payload("error", result, settings, Path("/tmp/log"))
    payload_groups = cast(list[dict[str, object]], payload["archive_groups"])

    assert [group["cleanup_status"] for group in payload_groups] == [
        "skipped",
        "error",
        "error",
    ]


@pytest.mark.unit()
def test_archive_result_payload_matches_cleanup_failures_for_source_keys_with_colons(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    ok_key = "data/ok/2026-04-13.tar.gz"
    failed_key = "data/fae/2026-04-13.tar.gz"
    failed_source_key = "data/fae/2026-04-14T00:30:00+01:00.xml"
    result = _result(
        _manifest(
            _group(ok_key, "data/ok/2026-04-13T23:00:00+01:00.xml"),
            _group(failed_key, failed_source_key),
        ),
        cleanup=ArchivePhaseResult("cleanup", (f"{failed_source_key}: AccessDenied",)),
        verified_archive_keys=(ok_key, failed_key),
    )

    payload = error_logging.archive_result_payload("error", result, settings, Path("/tmp/log"))
    payload_groups = cast(list[dict[str, object]], payload["archive_groups"])

    assert [group["cleanup_status"] for group in payload_groups] == ["ok", "error"]


@pytest.mark.unit()
def test_archive_failure_payload_preserves_cleanup_failure_source_keys_with_colons(
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    failed_key = "data/fae/2026-04-13.tar.gz"
    failed_source_key = "data/fae/2026-04-14T00:30:00+01:00.xml"
    result = _result(
        _manifest(_group(failed_key, failed_source_key)),
        cleanup=ArchivePhaseResult("cleanup", (f"{failed_source_key}: AccessDenied",)),
        verified_archive_keys=(failed_key,),
    )

    payload = error_logging.archive_failure_payload(result, settings, Path("/tmp/log"))
    mismatch = cast(dict[str, object], payload["mismatch"])

    assert payload["key"] == failed_source_key
    assert mismatch["key"] == failed_source_key
    assert mismatch["detail"] == "AccessDenied"


@pytest.mark.unit()
def test_cleanup_status_helpers_cover_payload_and_failure_fallback_edges() -> None:
    assert payload_cleanup_known_keys("copy", {"archive_groups": []}) == ()
    assert payload_cleanup_known_keys("cleanup", {"archive_groups": "bad"}) == ()
    assert payload_cleanup_known_keys(
        "cleanup",
        {
            "archive_groups": [
                "bad",
                {
                    "destination_archive_key": "",
                    "source_objects": [{"key": ""}, {"key": "source.txt"}, "bad"],
                },
            ]
        },
    ) == ("source.txt",)
    assert failure_key("broken: detail") == "broken"
    assert mismatch_payload("copy", "destination missing") == {
        "phase": "copy",
        "key": None,
        "category": "destination_missing",
        "detail": "destination missing",
    }
    assert mismatch_payload("copy", "archive run timed out") is None


@pytest.mark.unit()
def test_cleanup_status_helpers_cover_remaining_branch_edges() -> None:
    payload_group: dict[str, error_logging.JsonValue] = {
        "destination_archive_key": "data/fae/2026-04-13.tar.gz",
        "source_objects": [{"key": "source.txt"}],
    }
    result = _result(
        _manifest(),
        cleanup=ArchivePhaseResult("cleanup", skipped=True),
        verified_archive_keys=("data/fae/2026-04-13.tar.gz",),
    )
    apply_group_cleanup_statuses(result, [payload_group])
    assert "cleanup_status" not in payload_group

    payload_group_without_sources: dict[str, error_logging.JsonValue] = {
        "destination_archive_key": "data/fae/2026-04-13.tar.gz",
        "source_objects": "bad",
    }
    result = _result(
        _manifest(),
        cleanup=ArchivePhaseResult("cleanup", ("source.txt: AccessDenied",)),
        verified_archive_keys=("data/fae/2026-04-13.tar.gz",),
    )
    apply_group_cleanup_statuses(result, [payload_group_without_sources])
    assert payload_group_without_sources["cleanup_status"] == "error"

    failed_group: dict[str, error_logging.JsonValue] = {
        "destination_archive_key": "failed.tar.gz",
        "source_objects": [{"key": "failed.txt"}],
    }
    unmatched_group: dict[str, error_logging.JsonValue] = {
        "destination_archive_key": "unmatched.tar.gz",
        "source_objects": [{"key": "unmatched.txt"}],
    }
    result = _result(
        _manifest(),
        cleanup=ArchivePhaseResult("cleanup", ("failed.txt: AccessDenied",)),
    )
    apply_group_cleanup_statuses(result, [failed_group, unmatched_group])
    assert failed_group["cleanup_status"] == "error"
    assert "cleanup_status" not in unmatched_group

    assert failure_key("source.txt", ("source.txt",)) == "source.txt"
    assert failure_key("short: detail", ("longer-source.txt", "short")) == "short"
    assert mismatch_payload("cleanup", "source.txt", ("source.txt",)) == {
        "phase": "cleanup",
        "key": "source.txt",
        "category": "archive_failure",
        "detail": "",
    }


def _manifest(*groups: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        run_started_at_utc=datetime(2026, 4, 27, 2, tzinfo=UTC),
        retention_cutoff_utc=datetime(2026, 4, 13, 2, tzinfo=UTC),
        target_day=date(2026, 4, 13),
        entries=(),
        archive_groups=groups,
    )


def _result(
    manifest: SimpleNamespace,
    *,
    cleanup: ArchivePhaseResult,
    skipped_archive_keys: tuple[str, ...] = (),
    verified_archive_keys: tuple[str, ...] = (),
) -> ArchiveRunResult:
    return cast(
        ArchiveRunResult,
        cast(
            object,
            SimpleNamespace(
                run_id="run-id",
                manifest=manifest,
                list=ArchivePhaseResult("list"),
                copy=ArchivePhaseResult("copy"),
                verify=ArchivePhaseResult("verify"),
                cleanup=cleanup,
                skipped_archive_keys=skipped_archive_keys,
                verified_archive_keys=verified_archive_keys,
            ),
        ),
    )


def _group(destination_archive_key: str, source_key: str | None = None) -> SimpleNamespace:
    entries = ()
    if source_key is not None:
        entries = (
            SimpleNamespace(
                key=source_key,
                version_id="v1",
                size=123,
                destination_archive_key=destination_archive_key,
            ),
        )
    return SimpleNamespace(
        target_day=date(2026, 4, 13),
        archive_root="data",
        destination_archive_key=destination_archive_key,
        entries=entries,
        skipped_objects=(),
    )
