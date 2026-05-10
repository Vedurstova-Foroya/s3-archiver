"""Focused coverage tests for archive phase edge paths."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest
from s3_archiver_core import archive as archive_module
from s3_archiver_core.archive import ArchivePhaseResult
from s3_archiver_core.archive_manifest import ArchiveGroup, SourcePathFilter, build_archive_manifest

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed
from tests.unit.archive_workflow_fakes import object_properties as _properties


@pytest.mark.unit()
def test_verify_phase_reports_archive_verification_failure() -> None:
    source = FakeBucket("source", (_listed("data/fae/2026-04-13T00-00-00Z.txt", 1),))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=datetime(2026, 4, 27, 12, tzinfo=UTC),
        retention_days=14,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )
    group = manifest.archive_groups[0]
    verify_phase = cast(
        Callable[
            [
                FakeBucket,
                tuple[ArchiveGroup, ...],
                int,
                Callable[[], bool],
                Callable[[], float],
            ],
            ArchivePhaseResult,
        ],
        _private_attr(archive_module, "_verify_phase"),
    )
    destination = FakeBucket(
        "destination",
        destination={group.destination_archive_key: _properties(metadata={})},
    )

    result = verify_phase(destination, (group,), 1, lambda: False, lambda: 1.0)

    assert result.failures == ("data/fae/2026-04-13.tar.gz: archive verification failed",)


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))
