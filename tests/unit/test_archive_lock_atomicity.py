"""Atomicity regressions for archive run locks."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_core.archive_lock as archive_lock_module
from s3_archiver_core.archive_lock import FileArchiveRunLock


def read_lock(path: Path) -> Mapping[str, object]:
    decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(decoded, dict)
    return cast(Mapping[str, object], decoded)


@pytest.mark.unit()
def test_file_lock_installs_complete_payload_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    real_link = os.link
    observations: list[tuple[bool, bool, str]] = []

    def record_link(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(os.fsdecode(target)) == lock_path:
            source_path = Path(os.fsdecode(path))
            observations.append((source_path.exists(), lock_path.exists(), source_path.read_text()))
        real_link(
            path,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", record_link)

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="atomic",
        run_started_at_utc=datetime(2026, 4, 27, 12, tzinfo=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is True
    assert len(observations) == 1
    source_exists, target_existed, payload = observations[0]
    assert source_exists is True
    assert target_existed is False
    assert json.loads(payload)["run_id"] == "atomic"
    assert read_lock(lock_path)["run_id"] == "atomic"


@pytest.mark.unit()
def test_file_lock_stale_takeover_renames_lock_before_disposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    _ = lock_path.write_text("{", encoding="utf-8")
    disposed: list[str] = []
    real_unlink = Path.unlink

    def reject_live_lock_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == lock_path:
            raise AssertionError("live lock path was unlinked directly")
        disposed.append(self.name)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_live_lock_unlink)

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is True
    assert read_lock(lock_path)["run_id"] == "next"
    assert any(name.startswith("archive.lock.stale-") for name in disposed)


@pytest.mark.unit()
def test_file_lock_release_renames_lock_before_disposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    lock = FileArchiveRunLock(lock_path)
    assert lock.acquire(
        run_id="owner",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )
    disposed: list[str] = []
    real_unlink = Path.unlink

    def reject_live_lock_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == lock_path:
            raise AssertionError("live lock path was unlinked directly")
        disposed.append(self.name)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_live_lock_unlink)

    lock.release(run_id="owner")

    assert not lock_path.exists()
    assert any(name.startswith("archive.lock.released-") for name in disposed)


@pytest.mark.unit()
def test_file_lock_cleans_up_temp_file_when_payload_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="disk full"):
        _ = FileArchiveRunLock(lock_path).acquire(
            run_id="owner",
            run_started_at_utc=datetime.now(tz=UTC),
            timeout=timedelta(days=7),
        )

    leftovers = [path for path in tmp_path.iterdir() if path.suffix == ".tmp"]
    assert leftovers == []
    assert not lock_path.exists()


@pytest.mark.unit()
def test_file_lock_acquire_returns_false_when_install_attempts_are_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"

    def install_fails(_path: Path, _payload: str) -> bool:
        return False

    def takeover_succeeds(
        self: FileArchiveRunLock, timeout: timedelta, recover_unknown_host: bool
    ) -> bool:
        _ = (self, timeout, recover_unknown_host)
        return True

    monkeypatch.setattr(archive_lock_module, "_install_lock_payload", install_fails)
    monkeypatch.setattr(FileArchiveRunLock, "_take_over_existing_lock", takeover_succeeds)

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="owner",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is False


@pytest.mark.unit()
def test_file_lock_stale_takeover_fails_when_lock_cannot_be_renamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "archive.lock"
    _ = lock_path.write_text("{", encoding="utf-8")

    def fail_rename(self: Path, target: Path) -> Path:
        _ = (self, target)
        raise OSError("rename blocked")

    monkeypatch.setattr(Path, "rename", fail_rename)

    acquired = FileArchiveRunLock(lock_path).acquire(
        run_id="next",
        run_started_at_utc=datetime.now(tz=UTC),
        timeout=timedelta(days=7),
    )

    assert acquired is False
    assert lock_path.read_text(encoding="utf-8") == "{"


@pytest.mark.unit()
def test_dispose_lock_path_treats_missing_lock_as_disposed(tmp_path: Path) -> None:
    missing_lock = tmp_path / "missing.lock"

    dispose_lock_path = cast(
        Callable[[Path, str], bool], vars(archive_lock_module)["_dispose_lock_path"]
    )

    disposed = dispose_lock_path(missing_lock, "released")

    assert disposed is True
