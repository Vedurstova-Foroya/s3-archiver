"""Tests for scheduled archive lock reconciliation variants."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from s3_archiver_cli import scheduled_archive
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_reconcile_archive_lock_can_recover_unknown_host(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    recover_values: list[bool] = []
    releases: list[str] = []

    class RecordingLock:
        def __init__(self, path: Path, **_kwargs: object) -> None:
            assert path == settings.archive_lock_path

        def acquire(
            self,
            *,
            run_id: str,
            run_started_at_utc: datetime,
            timeout: object,
            recover_unknown_host: bool = False,
        ) -> bool:
            assert run_id
            assert run_started_at_utc.tzinfo == UTC
            _ = timeout
            recover_values.append(recover_unknown_host)
            return True

        def release(self, *, run_id: str) -> None:
            releases.append(run_id)

    monkeypatch.setattr(scheduled_archive, "FileArchiveRunLock", RecordingLock)

    assert scheduled_archive.reconcile_archive_lock(settings, recover_unknown_host=True) is True
    assert recover_values == [True]
    assert len(releases) == 1
