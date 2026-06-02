"""Tests for archive runtime temp-file handling."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple, cast

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.temp_files import (
    TRANSFER_TEMP_PREFIX,
    cleanup_stale_transfer_files,
    ensure_temp_storage_available,
    log_temp_storage,
    prepare_runtime_temp_dir,
)


@pytest.mark.unit()
def test_prepare_runtime_temp_dir_removes_stale_archiver_files(tmp_path: Path) -> None:
    temp_dir = tmp_path / "runtime-temp"
    temp_dir.mkdir()
    stale = temp_dir / f"{TRANSFER_TEMP_PREFIX}old"
    unrelated = temp_dir / "application-owned"
    _ = stale.write_bytes(b"old")
    _ = unrelated.write_bytes(b"keep")

    prepare_runtime_temp_dir(temp_dir)

    assert not stale.exists()
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.unit()
def test_prepare_runtime_temp_dir_rejects_file_path(tmp_path: Path) -> None:
    temp_dir = tmp_path / "not-a-dir"
    _ = temp_dir.write_bytes(b"")

    with pytest.raises(ConfigError, match="ARCHIVER_TEMP_DIR"):
        prepare_runtime_temp_dir(temp_dir)


@pytest.mark.unit()
def test_prepare_runtime_temp_dir_rejects_path_that_is_not_directory_after_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_dir = tmp_path / "runtime-temp"

    def noop_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
        _ = (self, parents, exist_ok)

    def never_directory(self: Path) -> bool:
        _ = self
        return False

    monkeypatch.setattr(Path, "mkdir", noop_mkdir)
    monkeypatch.setattr(Path, "is_dir", never_directory)

    with pytest.raises(ConfigError, match="ARCHIVER_TEMP_DIR must be a directory"):
        prepare_runtime_temp_dir(temp_dir)


@pytest.mark.unit()
def test_prepare_runtime_temp_dir_wraps_creation_failures(tmp_path: Path) -> None:
    temp_parent = tmp_path / "not-a-dir"
    _ = temp_parent.write_bytes(b"")

    with pytest.raises(ConfigError, match="ARCHIVER_TEMP_DIR cannot be created"):
        prepare_runtime_temp_dir(temp_parent / "runtime")


@pytest.mark.unit()
def test_cleanup_stale_transfer_files_keeps_directories(tmp_path: Path) -> None:
    temp_dir = tmp_path / "runtime-temp"
    temp_dir.mkdir()
    transfer_dir = temp_dir / f"{TRANSFER_TEMP_PREFIX}directory"
    transfer_dir.mkdir()

    cleanup_stale_transfer_files(temp_dir)

    assert transfer_dir.is_dir()


@pytest.mark.unit()
def test_log_temp_storage_records_available_bytes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_archive_logger(monkeypatch)
    monkeypatch.setattr(
        "s3_archiver_core.temp_files.shutil.disk_usage",
        _disk_usage(total=100, used=40, free=60),
    )

    with caplog.at_level(logging.INFO, logger="s3_archiver.archive"):
        snapshot = log_temp_storage(tmp_path / "runtime-temp")

    assert snapshot.free_bytes == 60
    record = _single_record(caplog, "archive.temp_storage.available")
    assert _record_value(record, "temp_dir") == str(tmp_path / "runtime-temp")
    assert _record_value(record, "total_bytes") == 100
    assert _record_value(record, "used_bytes") == 40
    assert _record_value(record, "free_bytes") == 60


@pytest.mark.unit()
def test_ensure_temp_storage_available_rejects_object_that_cannot_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "s3_archiver_core.temp_files.shutil.disk_usage",
        _disk_usage(total=100, used=90, free=10),
    )

    with pytest.raises(RuntimeError, match=r"source_key=large\.bin"):
        _ = ensure_temp_storage_available(
            tmp_path / "runtime-temp",
            required_bytes=11,
            source_key="large.bin",
            destination_key="archive/large.bin",
            operation="temp_file_backed_transfer",
        )


def _single_record(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    records = [record for record in caplog.records if getattr(record, "event", None) == event]
    assert len(records) == 1
    return records[0]


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def _disk_usage(*, total: int, used: int, free: int) -> object:
    def disk_usage(_path: object) -> _DiskUsage:
        return _DiskUsage(total=total, used=used, free=free)

    return disk_usage


def _record_value(record: logging.LogRecord, key: str) -> object:
    return cast(dict[str, object], record.__dict__)[key]


def _isolate_archive_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("s3_archiver")
    for handler in logger.handlers:
        handler.close()
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
