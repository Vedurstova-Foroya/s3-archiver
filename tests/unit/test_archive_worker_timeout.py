"""Tests for archive worker timeout behavior."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override

import pytest
from s3_archiver_core.archive import run_archive
from s3_archiver_core.archive_options import ArchiveOptions

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed


@pytest.mark.unit()
def test_run_archive_reports_timeout_without_waiting_for_stuck_cleanup_worker() -> None:
    class SlowDeleteBucket(FakeBucket):
        @override
        def delete_source(self, key: str, version_id: str | None) -> None:
            _ = (key, version_id)
            time.sleep(0.1)
            self.deleted.append((key, version_id))

    started = datetime.now(tz=UTC)
    target_day = started.date() - timedelta(days=60)
    source_key = f"data/fae/{target_day.isoformat()}T00-00-00.txt"
    source = SlowDeleteBucket("source", (_listed(source_key, 90),))
    destination = FakeBucket("destination")

    def clock() -> datetime:
        return datetime.now(tz=UTC)

    began = time.monotonic()

    result = run_archive(
        source,
        destination,
        ArchiveOptions(
            retention_days=60,
            cleanup_enabled=True,
            run_timeout=timedelta(milliseconds=50),
        ),
        run_started_at_utc=started,
        clock=clock,
    )

    assert result.cleanup.failures == ("archive run timed out",)
    assert time.monotonic() - began < 0.1


@pytest.mark.unit()
def test_run_archive_reports_timeout_without_waiting_for_stuck_copy_worker() -> None:
    class SlowUploadBucket(FakeBucket):
        @override
        def upload_archive_file(
            self, destination_key: str, archive_path: Path, metadata: Mapping[str, str]
        ) -> None:
            time.sleep(0.2)
            super().upload_archive_file(destination_key, archive_path, metadata)

    started = datetime.now(tz=UTC)
    target_day = started.date() - timedelta(days=60)
    source_key = f"data/fae/{target_day.isoformat()}T00-00-00.txt"
    source = FakeBucket("source", (_listed(source_key, 90),))
    destination = SlowUploadBucket("destination")

    began = time.monotonic()

    result = run_archive(
        source,
        destination,
        ArchiveOptions(
            retention_days=60,
            cleanup_enabled=False,
            run_timeout=timedelta(milliseconds=50),
        ),
        run_started_at_utc=started,
        clock=lambda: datetime.now(tz=UTC),
    )

    assert result.copy.failures == ("archive run timed out",)
    assert result.verify.skipped is True
    assert time.monotonic() - began < 0.15


@pytest.mark.unit()
def test_timed_out_worker_does_not_keep_python_process_alive() -> None:
    script = textwrap.dedent(
        """
        import time
        from datetime import UTC, datetime, timedelta
        from tests.unit.archive_workflow_fakes import FakeBucket, listed_object
        from s3_archiver_core.archive import run_archive
        from s3_archiver_core.archive_options import ArchiveOptions

        class SlowDeleteBucket(FakeBucket):
            def delete_source(self, *args, **kwargs):
                time.sleep(0.1)

        started = datetime.now(tz=UTC)
        target_day = started.date() - timedelta(days=60)
        source_key = f"data/fae/{target_day.isoformat()}T00-00-00.txt"
        run_archive(
            SlowDeleteBucket("source", (listed_object(source_key, 90),)),
            FakeBucket("destination"),
            ArchiveOptions(
                retention_days=60,
                cleanup_enabled=True,
                run_timeout=timedelta(milliseconds=50),
            ),
            run_started_at_utc=started,
            clock=lambda: datetime.now(tz=UTC),
        )
        """
    )

    _ = subprocess.run([sys.executable, "-c", script], check=True, timeout=1.0)
