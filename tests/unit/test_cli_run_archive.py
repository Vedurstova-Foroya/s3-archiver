"""Tests for direct `_run_archive` CLI runtime behavior."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.error_logging as error_logging
import s3_archiver_cli.main as cli_module
from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult
from s3_archiver_core.archive_manifest import ArchiveManifest
from s3_archiver_core.errors import ArchiveRunError, HealthCheckError
from s3_archiver_core.settings import AppSettings


def _private_attr(module: object, name: str) -> object:
    return cast(object, getattr(module, name))


_run_archive = cast(
    Callable[[AppSettings, Path], dict[str, object]],
    _private_attr(cli_module, "_run_archive"),
)


@pytest.mark.unit()
def test_run_archive_keeps_matching_run_id_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    released: list[str] = []

    class FixedUuid:
        hex: str = "locked-run"

    class RecordingLock:
        def __init__(self, _path: Path, **_kwargs: object) -> None:
            return

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            _ = (run_started_at_utc, timeout)
            return run_id == "locked-run"

        def release(self, *, run_id: str) -> None:
            released.append(run_id)

    def run_health(_settings: AppSettings, _log_file: Path) -> object:
        return object()

    def build_client(_location: object) -> object:
        return object()

    def run_core_archive(
        source: object,
        destination: object,
        options: object,
        *,
        run_started_at_utc: datetime,
        debug_logger: object | None = None,
    ) -> ArchiveRunResult:
        _ = (source, destination, options, run_started_at_utc, debug_logger)
        return _archive_result(run_id="locked-run")

    monkeypatch.setattr(cli_module, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cli_module, "run_health_check", run_health)
    monkeypatch.setattr(cli_module, "build_s3_client", build_client)
    monkeypatch.setattr(cli_module, "run_archive", run_core_archive)

    payload = _run_archive(settings, Path("/tmp/log"))

    assert payload["status"] == "ok"
    assert released == ["locked-run"]


@pytest.mark.unit()
def test_run_archive_preserves_group_state_when_rewriting_run_id(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)

    class FixedUuid:
        hex: str = "locked-run"

    class RecordingLock:
        def __init__(self, _path: Path, **_kwargs: object) -> None:
            return

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            return True

        def release(self, *, run_id: str) -> None:
            _ = run_id

    def run_health(_settings: AppSettings, _log_file: Path) -> object:
        return object()

    def build_client(_location: object) -> object:
        return object()

    def run_core_archive(
        source: object,
        destination: object,
        options: object,
        *,
        run_started_at_utc: datetime,
        debug_logger: object | None = None,
    ) -> ArchiveRunResult:
        _ = (source, destination, options, run_started_at_utc, debug_logger)
        result = _archive_result(run_id="core-run")
        return ArchiveRunResult(
            result.run_id,
            result.manifest,
            result.copy,
            result.verify,
            result.cleanup,
            result.list,
            ("verified.tar.gz",),
            ("skipped.tar.gz",),
        )

    def archive_result_payload(
        status: str,
        result: ArchiveRunResult,
        _settings: AppSettings,
        _log_file: Path,
    ) -> dict[str, object]:
        return {
            "status": status,
            "run_id": result.run_id,
            "verified_archive_keys": list(result.verified_archive_keys),
            "skipped_archive_keys": list(result.skipped_archive_keys),
        }

    monkeypatch.setattr(cli_module, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cli_module, "run_health_check", run_health)
    monkeypatch.setattr(cli_module, "build_s3_client", build_client)
    monkeypatch.setattr(cli_module, "run_archive", run_core_archive)
    monkeypatch.setattr(error_logging, "archive_result_payload", archive_result_payload)

    payload = _run_archive(settings, Path("/tmp/log"))
    record_path = Path(base_env["LOG_DIR"]) / "archive-runs" / "locked-run.json"
    record = cast(dict[str, object], json.loads(record_path.read_text(encoding="utf-8")))

    assert payload["run_id"] == "locked-run"
    assert payload["verified_archive_keys"] == ["verified.tar.gz"]
    assert payload["skipped_archive_keys"] == ["skipped.tar.gz"]
    assert cast(dict[str, object], record["payload"])["run_id"] == "locked-run"


@pytest.mark.unit()
def test_run_archive_raises_when_lock_is_already_held(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)

    class RefusingLock:
        def __init__(self, _path: Path, **_kwargs: object) -> None:
            return

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            return False

        def release(self, *, run_id: str) -> None:
            raise AssertionError(f"unexpected release for {run_id}")

    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RefusingLock)

    with pytest.raises(ArchiveRunError, match="already held"):
        _ = _run_archive(settings, Path("/tmp/log"))


@pytest.mark.unit()
def test_run_archive_reraises_domain_errors_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)
    released: list[str] = []

    class RecordingLock:
        def __init__(self, _path: Path, **_kwargs: object) -> None:
            return

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            return True

        def release(self, *, run_id: str) -> None:
            released.append(run_id)

    def raise_health_error(_settings: AppSettings, _log_file: Path) -> object:
        raise HealthCheckError("auth failed: denied")

    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cli_module, "run_health_check", raise_health_error)

    with pytest.raises(HealthCheckError, match="auth failed: denied"):
        _ = _run_archive(settings, Path("/tmp/log"))

    assert len(released) == 1


@pytest.mark.unit()
def test_run_archive_records_failed_run_when_preflight_raises(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = AppSettings.from_env(base_env)

    class FixedUuid:
        hex: str = "locked-run"

    class RecordingLock:
        def __init__(self, _path: Path, **_kwargs: object) -> None:
            return

        def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: object) -> bool:
            _ = (run_id, run_started_at_utc, timeout)
            return True

        def release(self, *, run_id: str) -> None:
            _ = run_id

    def raise_health_error(_settings: AppSettings, _log_file: Path) -> object:
        raise HealthCheckError("auth failed: denied")

    monkeypatch.setattr(cli_module, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(cli_module, "FileArchiveRunLock", RecordingLock)
    monkeypatch.setattr(cli_module, "run_health_check", raise_health_error)

    with pytest.raises(HealthCheckError, match="auth failed: denied"):
        _ = _run_archive(settings, Path("/tmp/log"))

    record_path = Path(base_env["LOG_DIR"]) / "archive-runs" / "locked-run.json"
    decoded = cast(object, json.loads(record_path.read_text(encoding="utf-8")))
    assert isinstance(decoded, dict)
    record = cast(dict[str, object], decoded)
    assert record["status"] == "failed"
    payload = record["payload"]
    assert isinstance(payload, dict)
    assert payload["message"] == "auth failed: denied"
    assert payload["phase"] == "startup.preflight"


def _archive_result(*, run_id: str = "run-id") -> ArchiveRunResult:
    return ArchiveRunResult(
        run_id=run_id,
        manifest=ArchiveManifest(
            run_started_at_utc=datetime.fromisoformat("2026-04-09T17:00:43+00:00"),
            retention_cutoff_utc=datetime.fromisoformat("2026-02-08T17:00:43+00:00"),
            entries=(),
        ),
        copy=ArchivePhaseResult("copy"),
        verify=ArchivePhaseResult("verify"),
        cleanup=ArchivePhaseResult("cleanup"),
    )
