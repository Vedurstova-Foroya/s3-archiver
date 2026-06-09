"""Tests for chained automatic cleanup inside the archive run."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core.archive import ArchivePhaseResult
from s3_archiver_core.errors import CleanupError, CleanupManifestError
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.settings import AppSettings

from tests.unit.cli_cleanup_test_support import (
    build_deleting,
    build_opaque,
    build_present,
    install_archive_mocks,
    make_result,
    make_settings,
    record_status,
)


def _private_attr(name: str) -> object:
    return cast(object, getattr(cli_module, name))


_run_archive = cast(
    Callable[[AppSettings, Path], dict[str, JsonValue]],
    _private_attr("_run_archive"),
)


@pytest.mark.unit()
def test_run_archive_chains_cleanup_when_enabled(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=True)
    deletes: list[dict[str, object]] = []
    install_archive_mocks(
        monkeypatch, make_result(settings, with_entry=True), build_deleting(deletes)
    )

    payload = _run_archive(settings, Path("/tmp/log"))

    cleanup = cast(dict[str, JsonValue], payload["cleanup"])
    assert payload["status"] == "ok"
    assert cleanup["status"] == "ok"
    assert cleanup["cleaned_count"] == 1
    assert deletes == [{"Bucket": "archive-bucket", "Key": "data/a.xml", "VersionId": "v1"}]
    assert not (settings.cleanup_pending_dir / "locked-run.jsonl").exists()


@pytest.mark.unit()
def test_run_archive_writes_manifest_without_cleanup_when_disabled(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=False)
    install_archive_mocks(monkeypatch, make_result(settings, with_entry=True), build_opaque)

    payload = _run_archive(settings, Path("/tmp/log"))

    assert "cleanup" not in payload
    assert (settings.cleanup_pending_dir / "locked-run.jsonl").exists()


@pytest.mark.unit()
def test_run_archive_chained_cleanup_failure_records_and_raises(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=True)
    install_archive_mocks(monkeypatch, make_result(settings, with_entry=True), build_present)

    with pytest.raises(CleanupError, match="still present after delete"):
        _ = _run_archive(settings, Path("/tmp/log"))

    assert record_status(settings.log_dir) == "failed"
    assert (settings.cleanup_pending_dir / "locked-run.jsonl").exists()


@pytest.mark.unit()
def test_run_archive_chained_cleanup_aborts_on_mangled_manifest(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=True)
    settings.cleanup_pending_dir.mkdir(parents=True, exist_ok=True)
    _ = (settings.cleanup_pending_dir / "old-run.jsonl").write_text("{mangled", encoding="utf-8")
    deletes: list[dict[str, object]] = []
    install_archive_mocks(
        monkeypatch, make_result(settings, with_entry=True), build_deleting(deletes)
    )

    with pytest.raises(CleanupManifestError):
        _ = _run_archive(settings, Path("/tmp/log"))

    assert deletes == []


@pytest.mark.unit()
def test_run_archive_chained_cleanup_is_empty_when_nothing_archived(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=True)
    install_archive_mocks(monkeypatch, make_result(settings, with_entry=False), build_opaque)

    payload = _run_archive(settings, Path("/tmp/log"))

    cleanup = cast(dict[str, JsonValue], payload["cleanup"])
    assert cleanup["status"] == "empty"


@pytest.mark.unit()
def test_run_archive_skips_cleanup_export_when_result_not_ok(
    monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, base_env, cleanup=True)
    failed = make_result(
        settings, with_entry=True, copy=ArchivePhaseResult("copy", ("data/a.xml: boom",))
    )
    install_archive_mocks(monkeypatch, failed, build_opaque)

    payload = _run_archive(settings, Path("/tmp/log"))

    assert payload["status"] == "error"
    assert "cleanup" not in payload
    assert not (settings.cleanup_pending_dir / "locked-run.jsonl").exists()
