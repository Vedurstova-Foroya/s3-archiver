"""Unit tests for visual demo output formatting."""

from __future__ import annotations

import pytest
from s3_archiver_cli.visual_demo_output import JsonValue, emit_archive_result


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

    emit_archive_result(lines.append, payload)

    assert "archive day count: 7" in lines
    assert (
        "archive days sample: "
        + "2026-01-01, 2026-01-02, 2026-01-03, ..., 2026-01-05, 2026-01-06, 2026-01-07"
        in lines
    )
