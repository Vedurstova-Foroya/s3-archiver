"""Payload and progress edge coverage for archive summaries."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast, override

import pytest
from s3_archiver_cli import streaming_subprocess
from s3_archiver_cli.archive_progress_reporting import (
    ArchiveProgressReporter,
    include_archive_payload_details,
)
from s3_archiver_core._archive_env import bool_env, positive_int_env
from s3_archiver_core._archive_manifest_models import (
    ArchiveGroup,
    CopyMode,
    ManifestEntry,
)
from s3_archiver_core.archive_payloads import archive_group_payload, archive_manifest_payload
from s3_archiver_core.archive_progress import ArchiveProgress
from s3_archiver_core.settings import AppSettings

from tests.unit.archive_workflow_fakes import listed_object as _listed

TARGET_DAY = date(2026, 4, 13)


@pytest.mark.unit()
def test_archive_progress_reporter_logs_repeated_percent_after_step() -> None:
    messages: list[str] = []
    logger = logging.getLogger("s3_archiver.archive.test")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = _ListHandler(messages)
    logger.addHandler(handler)
    reporter = ArchiveProgressReporter()
    object.__setattr__(reporter, "_logger", logger)

    try:
        reporter(ArchiveProgress("copy", 99, 100))
        reporter(ArchiveProgress("copy", 100, 200))
        reporter(ArchiveProgress("copy", 198, 200))
        reporter(ArchiveProgress("zero", 0, 0))
        reporter(ArchiveProgress("zero", 999, 0))
        reporter(ArchiveProgress("empty", 0, 10))
    finally:
        logger.removeHandler(handler)
    assert sum("archive progress copy" in message for message in messages) == 2
    assert sum("archive progress zero 100%" in message for message in messages) == 1
    assert any(
        "archive progress empty 0%" in message and "eta=unknown" in message for message in messages
    )


@pytest.mark.unit()
def test_archive_payload_detail_env_defaults_to_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHIVER_PAYLOAD_DETAIL", raising=False)
    assert include_archive_payload_details() is False
    monkeypatch.setenv("ARCHIVER_PAYLOAD_DETAIL", "full")
    assert include_archive_payload_details() is True
    monkeypatch.setenv("ARCHIVER_TEST_POSITIVE_INT", "not-an-int")
    assert positive_int_env("ARCHIVER_TEST_POSITIVE_INT", 5) == 5
    monkeypatch.setenv("ARCHIVER_TEST_POSITIVE_INT", "0")
    assert positive_int_env("ARCHIVER_TEST_POSITIVE_INT", 5) == 5
    monkeypatch.setenv("ARCHIVER_TEST_BOOL", "yes")
    assert bool_env("ARCHIVER_TEST_BOOL") is True
    monkeypatch.setenv("ARCHIVER_TEST_BOOL", "no")
    assert bool_env("ARCHIVER_TEST_BOOL") is False


@pytest.mark.unit()
def test_archive_manifest_payload_optional_sections_and_fallbacks() -> None:
    entry = _entry("data/2026-04-13T01-00-00Z.xml")
    direct = _entry("raw/current.txt", copy_mode="direct", target_day=None)
    group = ArchiveGroup(
        date(2026, 4, 13),
        "",
        "archives/2026-04-13.tar.gz",
        (entry,),
        source_bucket="source",
        destination_bucket="archive",
    )
    manifest = _ManifestWithIterable(
        run_started_at_utc=datetime(2026, 4, 27, 12, tzinfo=UTC),
        entries=_EntryIterable((entry, direct)),
        archive_groups=_TargetDays((group,)),
        skipped_objects=None,
        source_byte_count=20,
    )

    summary = archive_manifest_payload(manifest, include_details=False)
    assert "archive_groups" not in summary

    full = archive_manifest_payload(
        manifest,
        include_archive_days=True,
        include_entries=True,
        include_run_started_at_utc=True,
    )
    assert full["archive_days"] == ["2026-04-13"]
    assert full["run_started_at_utc"] == "2026-04-27T12:00:00+00:00"
    assert full["direct_copy_count"] == 1
    assert len(cast(list[object], full["entries"])) == 2

    fallback_group = _PayloadGroup(None, (entry,))
    empty_group = _PayloadGroup(None, ())
    assert archive_group_payload(fallback_group)["destination_archive_key"] == (
        "archives/2026-04-13.tar.gz"
    )
    assert archive_group_payload(empty_group)["destination_archive_key"] == ""
    assert archive_manifest_payload(
        _ManifestWithPlainGroups((group,)), include_details=False, include_archive_days=True
    )["archive_days"] == ["2026-04-13"]
    assert archive_manifest_payload(
        _ManifestWithIterable(
            run_started_at_utc=datetime(2026, 4, 27, 12, tzinfo=UTC),
            entries=_BadCopyModeCounter((direct,)),
            archive_groups=_BadTargetDays((group,)),
            skipped_objects=(),
        ),
        include_details=False,
        include_archive_days=True,
    )["archive_days"] == ["2026-04-13"]
    assert (
        archive_manifest_payload(
            _ManifestWithIterable(
                run_started_at_utc=datetime(2026, 4, 27, 12, tzinfo=UTC),
                entries=None,
                archive_groups=(),
                skipped_objects=(),
            ),
            include_details=False,
        )["direct_copy_count"]
        == 0
    )


@pytest.mark.unit()
def test_streaming_subprocess_relay_pipe_accepts_missing_pipe() -> None:
    class Process:
        stdout: None = None
        stderr: None = None

        def wait(self, timeout: float) -> int:
            assert 0.0 < timeout <= 1.0
            return 0

    def popen(*_args: object, **_kwargs: object) -> Process:
        return Process()

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("s3_archiver_cli.streaming_subprocess.subprocess.Popen", popen)
        assert (
            streaming_subprocess.run_streaming_command(
                ["cmd"],
                cast(AppSettings, cast(object, _Settings(timedelta(seconds=1)))),
                lambda _line: None,
                lambda _line: None,
            )
            == 0
        )
    finally:
        monkeypatch.undo()


@dataclass(frozen=True, slots=True)
class _EntryIterable:
    entries: tuple[ManifestEntry, ...]

    def __iter__(self) -> Iterator[ManifestEntry]:
        return iter(self.entries)


@dataclass(frozen=True, slots=True)
class _BadCopyModeCounter:
    entries: tuple[ManifestEntry, ...]

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[ManifestEntry]:
        return iter(self.entries)

    def count_copy_mode(self, _copy_mode: str) -> str:
        return "unknown"


@dataclass(frozen=True, slots=True)
class _TargetDays:
    groups: tuple[ArchiveGroup, ...]

    def __len__(self) -> int:
        return len(self.groups)

    def __iter__(self) -> Iterator[ArchiveGroup]:
        return iter(self.groups)

    def target_days(self) -> tuple[str, ...]:
        return ("2026-04-13",)


@dataclass(frozen=True, slots=True)
class _BadTargetDays:
    groups: tuple[ArchiveGroup, ...]

    def __len__(self) -> int:
        return len(self.groups)

    def __iter__(self) -> Iterator[ArchiveGroup]:
        return iter(self.groups)

    def target_days(self) -> str:
        return "bad"


class _ListHandler(logging.Handler):
    messages: list[str]

    def __init__(self, messages: list[str]) -> None:
        super().__init__()
        self.messages = messages

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@dataclass(frozen=True, slots=True)
class _PayloadGroup:
    destination_archive_key: str | None
    entries: tuple[ManifestEntry, ...]
    target_day: date = TARGET_DAY
    archive_root: str = ""
    route_name: str = "daily"
    parser_kind: str = "filename_timestamp"
    copy_mode: str = "daily_tar_gz"
    source_bucket: str = "source"
    destination_bucket: str = "archive"


@dataclass(frozen=True, slots=True)
class _ManifestWithPlainGroups:
    archive_groups: tuple[ArchiveGroup, ...]
    entries: tuple[ManifestEntry, ...] = ()
    skipped_objects: tuple[object, ...] = ()
    target_day: None = None
    manifest_storage: str = "memory"
    source_byte_count: int = 0


@dataclass(frozen=True, slots=True)
class _ManifestWithIterable:
    run_started_at_utc: datetime
    entries: object
    archive_groups: object
    skipped_objects: object | None
    target_day: None = None
    manifest_storage: str = "memory"
    source_byte_count: int = 0


@dataclass(frozen=True, slots=True)
class _Settings:
    run_timeout: timedelta


def _entry(
    key: str,
    *,
    copy_mode: str = "daily_tar_gz",
    target_day: date | None = TARGET_DAY,
) -> ManifestEntry:
    listed = _listed(key, 1, "v1")
    destination_key = "raw/current.txt" if copy_mode == "direct" else "archives/2026-04-13.tar.gz"
    return ManifestEntry(
        source_bucket="source",
        key=key,
        size=listed.size,
        last_modified=listed.last_modified,
        etag=listed.etag,
        version_id=listed.version_id,
        object=listed,
        target_day=target_day,
        archive_root="",
        destination_archive_key=destination_key,
        route_name="daily",
        parser_kind="filename_timestamp",
        copy_mode=cast(CopyMode, copy_mode),
        destination_bucket="archive",
        destination_key=destination_key,
    )
