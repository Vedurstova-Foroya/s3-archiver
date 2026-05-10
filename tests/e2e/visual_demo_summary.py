"""Verified-result terminal summary for visual demo e2e tests."""

from __future__ import annotations

from typing import cast


def print_verified_summary(
    payload: dict[str, object],
    *,
    total_count: int,
    copied_count: int,
    remaining_source_count: int,
) -> None:
    raw_cleanup_preview = payload.get("cleanup_preview")
    cleanup_preview_count = (
        cast(dict[str, object], raw_cleanup_preview)["object_count"]
        if isinstance(raw_cleanup_preview, dict)
        else "not run"
    )
    print()
    print("=" * 78)
    print("VERIFIED RESULT")
    print("=" * 78)
    print(f"  status: {payload['status']}")
    print(f"  source objects seeded: {total_count}")
    print(f"  remaining in source after real cleanup: {remaining_source_count}")
    print(f"  archived to destination: {copied_count}")
    print(f"  cleanup preview objects: {cleanup_preview_count}")
    print(f"  source objects after real cleanup would be: {remaining_source_count}")
    print(f"  destination objects after real cleanup would be: {copied_count}")
    print(
        "  cleanup preview left buckets unchanged: "
        + str(payload["cleanup_preview_left_bucket_state_unchanged"])
    )
