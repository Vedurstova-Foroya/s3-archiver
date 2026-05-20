"""Unit tests for visual demo output formatting."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.settings import AppSettings
from s3_archiver_visual_demo.output import emit_archive_result, emit_intro

from tests.unit.health_helpers import multi_route_env


@pytest.mark.unit()
def test_archive_result_uses_payload_archive_day_sample_for_long_ranges() -> None:
    lines: list[str] = []
    archive_days: list[JsonValue] = [f"2026-01-{day:02d}" for day in range(1, 8)]
    payload: dict[str, JsonValue] = {
        "status": "ok",
        "target_day": "2026-01-07",
        "archive_days": archive_days,
        "archive_count": 1,
        "archive_groups": [
            {
                "target_day": "2026-01-07",
                "archive_root": "demo",
                "destination_archive_key": "demo/2026-01-07.tar.gz",
                "source_object_count": 2,
                "skipped_object_count": 0,
            }
        ],
        "phases": {
            "list": {"status": "ok", "failure_count": 0},
            "copy": {"status": "ok", "failure_count": 0},
            "verify": {"status": "ok", "failure_count": 0},
        },
    }

    emit_archive_result(lines.append, payload)

    assert "archive day count: 7" in lines
    assert (
        "archive days sample: "
        + "2026-01-01, 2026-01-02, 2026-01-03, ..., 2026-01-05, 2026-01-06, 2026-01-07"
        in lines
    )


@pytest.mark.unit()
def test_archive_result_emits_direct_entries() -> None:
    lines: list[str] = []
    payload: dict[str, JsonValue] = {
        "status": "ok",
        "target_day": None,
        "archive_count": 0,
        "direct_copy_count": 1,
        "archive_groups": [],
        "direct_entries": [
            {
                "destination_key": "mirror/raw/live.txt",
                "source_object_count": 1,
            }
        ],
        "phases": {
            "list": {"status": "ok", "failure_count": 0},
            "copy": {"status": "ok", "failure_count": 0},
            "verify": {"status": "ok", "failure_count": 0},
        },
    }

    emit_archive_result(lines.append, payload)

    assert "DIRECT destination_key=mirror/raw/live.txt source_object_count=1" in lines


@pytest.mark.unit()
def test_intro_emits_multi_route_bucket_summary(base_env: dict[str, str]) -> None:
    lines: list[str] = []
    settings = AppSettings.from_env(multi_route_env(base_env))

    emit_intro(lines.append, settings, Path("demo.log"), datetime(2026, 5, 12, tzinfo=UTC))

    assert "source buckets: archive-bucket, second-source-bucket" in lines
    assert "destination buckets: destination-bucket, second-destination-bucket" in lines
    assert "== Working Set ==" in lines
    assert (
        "ROUTE  name=default parser=filename_timestamp copy_mode=daily_tar_gz "
        + "source_bucket=archive-bucket source_path=(root) "
        + "destination_bucket=destination-bucket destination_path=(root)"
    ) in lines
    assert (
        "ROUTE  name=secondary parser=direct copy_mode=direct "
        + "source_bucket=second-source-bucket source_path=raw/ "
        + "destination_bucket=second-destination-bucket destination_path=mirror/"
    ) in lines
