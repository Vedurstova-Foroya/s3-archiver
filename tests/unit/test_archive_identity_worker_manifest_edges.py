"""Focused coverage for route manifest identity and worker edges."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import cast, override

import pytest
from s3_archiver_core import _archive_manifest_paths as manifest_paths_module
from s3_archiver_core._archive_identity import stable_identity_value
from s3_archiver_core.archive_manifest import (
    build_archive_manifest,
)

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2026, 4, 27, 12, tzinfo=UTC)


@pytest.mark.unit()
def test_stable_identity_value_covers_supported_shapes() -> None:
    assert stable_identity_value(None) is None
    assert stable_identity_value(IdentityMode.daily) == "daily"
    assert stable_identity_value(IdentityRecord("source")) == "IdentityRecord(name='source')"
    assert stable_identity_value({"z": IdentityMode.direct, "a": (None, IdentityRecord("x"))}) == {
        "a": [None, "IdentityRecord(name='x')"],
        "z": "direct",
    }
    assert stable_identity_value([{"nested": IdentityMode.daily}]) == [{"nested": "daily"}]
    assert stable_identity_value(ReprOnly()) == "repr-only"


@pytest.mark.unit()
def test_route_manifest_skips_unselected_objects_and_records_storage_identity() -> None:
    skipped_source = FakeBucket("source", (_listed("data/no-date.txt", 1),))
    skipped_manifest = build_archive_manifest(
        skipped_source,
        run_started_at_utc=STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
    )

    assert len(skipped_manifest.entries) == 0
    assert skipped_manifest.skipped_objects[0].reason == "no reliable key timestamp"

    source = IdentityBucket(
        "source",
        (_listed("raw/prefix/data/fae/2026-04-13T00-00-00Z.txt", 1),),
    )
    destination = FakeBucket("destination")
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
        source_path="raw/prefix",
        destination=destination,
        destination_path="archives",
    )
    entry = manifest.entries[0]

    assert entry.archive_root == "data/fae"
    assert entry.destination_archive_key == "archives/data/fae/2026-04-13.tar.gz"
    assert entry.source_identity == ("identity", "source")
    assert entry.destination_identity == ("FakeBucket", "destination")


@pytest.mark.unit()
def test_relative_archive_root_covers_prefix_boundaries() -> None:
    assert _relative_archive_root()("raw/prefix", "raw/prefix/") == ""
    assert _relative_key()("raw/prefix/file.txt", "raw/prefix/") == "file.txt"
    assert _relative_archive_root()("elsewhere", "raw/prefix/") == "elsewhere"
    assert _relative_key()("elsewhere/file.txt", "raw/prefix/") == "elsewhere/file.txt"


@pytest.mark.unit()
def test_manifest_filters_source_path_and_relativizes_default_archive_root() -> None:
    source = FakeBucket(
        "source",
        (
            _listed("outside/2026-04-13T00-00-00Z.txt", 1),
            _listed("raw/prefix/data/fae/2026-04-13T00-00-00Z.txt", 1),
        ),
    )

    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        versioning_state="Enabled",
        parser_kind="filename_timestamp",
        copy_mode="daily_tar_gz",
        source_path="raw/prefix",
    )

    assert [entry.key for entry in manifest.entries] == [
        "raw/prefix/data/fae/2026-04-13T00-00-00Z.txt"
    ]
    assert manifest.entries[0].archive_root == "data/fae"


class IdentityMode(Enum):
    daily = "daily"
    direct = "direct"


@dataclass(frozen=True)
class IdentityRecord:
    name: str


class ReprOnly:
    @override
    def __repr__(self) -> str:
        return "repr-only"


class IdentityBucket(FakeBucket):
    def storage_identity(self) -> tuple[str, str]:
        return ("identity", self.bucket)


def _relative_archive_root() -> Callable[[str, str], str]:
    name = "relative_archive_root"
    return cast(
        Callable[[str, str], str],
        getattr(manifest_paths_module, name),
    )


def _relative_key() -> Callable[[str, str], str]:
    name = "relative_key"
    return cast(
        Callable[[str, str], str],
        getattr(manifest_paths_module, name),
    )
