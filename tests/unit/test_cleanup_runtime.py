"""Tests for the CLI cleanup runtime helpers."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_cli.cleanup_runtime as cleanup_runtime
from s3_archiver_cli.cleanup_runtime import (
    cleanup_child_command,
    perform_cleanup,
    run_cleanup_subprocess,
    selected_manifests,
)
from s3_archiver_core.archive_routes import ArchiveRoute
from s3_archiver_core.cleanup_manifest import CleanupRecord, write_cleanup_manifest
from s3_archiver_core.payload_utils import JsonValue
from s3_archiver_core.settings import AppSettings

from tests.unit.archive_workflow_fakes import FakeBucket, archive_routes
from tests.unit.archive_workflow_fakes import listed_object as _listed

RUN_STARTED = "2026-04-20T00:00:00+00:00"
STARTED = datetime(2026, 4, 20, 12, tzinfo=UTC)


def _record(key: str, version_id: str | None = "v1") -> CleanupRecord:
    return CleanupRecord(
        route_name="default",
        source_identity=None,
        source_bucket="source",
        key=key,
        version_id=version_id,
        size=10,
        etag='"etag"',
        destination_bucket="destination",
        destination_key="",
        destination_archive_key="data/2026-04-13.tar.gz",
        copy_mode="daily_tar_gz",
    )


def _routes(source: FakeBucket) -> tuple[ArchiveRoute, ...]:
    return archive_routes(source, FakeBucket("destination"))


def _settings(tmp_path: Path, base_env: dict[str, str]) -> AppSettings:
    return AppSettings.from_env({**base_env, "LOG_DIR": str(tmp_path / "logs")})


def _dummy_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        run_timeout=timedelta(days=1),
        temp_dir=tmp_path / "tmp",
        log_level="INFO",
        log_dir=tmp_path / "logs",
        routes=(),
    )


@pytest.mark.unit()
def test_cleanup_child_command_with_and_without_manifest() -> None:
    assert cleanup_child_command(None) == [
        sys.executable,
        "-c",
        "from s3_archiver_cli.main import main; main()",
        "cleanup-once",
    ]
    assert cleanup_child_command(Path("run-1.jsonl"))[-2:] == ["--manifest", "run-1.jsonl"]


@pytest.mark.unit()
def test_selected_manifests_returns_explicit_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "given.jsonl"

    assert selected_manifests(_dummy_settings(tmp_path), manifest) == [manifest]


@pytest.mark.unit()
def test_selected_manifests_without_pending_dir(tmp_path: Path) -> None:
    assert selected_manifests(_dummy_settings(tmp_path), None) == []


@pytest.mark.unit()
def test_selected_manifests_lists_pending_sorted(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.cleanup_pending_dir.mkdir(parents=True, exist_ok=True)
    second = settings.cleanup_pending_dir / "run-2.jsonl"
    first = settings.cleanup_pending_dir / "run-1.jsonl"
    _ = second.write_text("", encoding="utf-8")
    _ = first.write_text("", encoding="utf-8")

    assert selected_manifests(settings, None) == [first, second]


@pytest.mark.unit()
def test_perform_cleanup_success_payload(tmp_path: Path, base_env: dict[str, str]) -> None:
    settings = _settings(tmp_path, base_env)
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    manifest = tmp_path / "run-1.jsonl"
    _ = write_cleanup_manifest(
        manifest, run_id="run-1", run_started_at_utc=RUN_STARTED, records=[_record("data/a.xml")]
    )

    result, payload = perform_cleanup(
        settings,
        _routes(source),
        manifest=manifest,
        started=STARTED,
        log_file=Path("/tmp/log"),
        now=lambda: STARTED,
    )

    assert result.ok is True
    assert payload["status"] == "ok"
    assert payload["cleaned_count"] == 1
    assert payload["removed_manifest_count"] == 1
    assert not manifest.exists()


@pytest.mark.unit()
def test_perform_cleanup_empty_payload(tmp_path: Path, base_env: dict[str, str]) -> None:
    settings = _settings(tmp_path, base_env)
    source = FakeBucket("source")

    result, payload = perform_cleanup(
        settings, _routes(source), manifest=None, started=STARTED, log_file=Path("/tmp/log")
    )

    assert result.empty is True
    assert payload["status"] == "empty"
    assert payload["reason"] == "cleanup_manifest_empty"


@pytest.mark.unit()
def test_perform_cleanup_error_payload(tmp_path: Path, base_env: dict[str, str]) -> None:
    settings = _settings(tmp_path, base_env)
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    source.fail_delete = True
    manifest = tmp_path / "run-1.jsonl"
    _ = write_cleanup_manifest(
        manifest, run_id="run-1", run_started_at_utc=RUN_STARTED, records=[_record("data/a.xml")]
    )

    _result, payload = perform_cleanup(
        settings,
        _routes(source),
        manifest=manifest,
        started=STARTED,
        log_file=Path("/tmp/log"),
        now=lambda: STARTED,
    )

    assert payload["status"] == "error"
    assert payload["reason"] == "cleanup_run_failed"
    assert payload["details"] == "data/a.xml: delete failed: delete failed"
    assert manifest.exists()


@pytest.mark.unit()
def test_perform_cleanup_respects_deadline(tmp_path: Path, base_env: dict[str, str]) -> None:
    settings = _settings(tmp_path, base_env)
    source = FakeBucket("source", (_listed("data/a.xml", 1, "v1"),))
    manifest = tmp_path / "run-1.jsonl"
    _ = write_cleanup_manifest(
        manifest, run_id="run-1", run_started_at_utc=RUN_STARTED, records=[_record("data/a.xml")]
    )
    after_deadline = STARTED + settings.run_timeout + settings.run_timeout

    _result, payload = perform_cleanup(
        settings,
        _routes(source),
        manifest=manifest,
        started=STARTED,
        log_file=Path("/tmp/log"),
        now=lambda: after_deadline,
    )

    assert payload["status"] == "error"
    assert source.deleted == []
    assert manifest.exists()


@pytest.mark.unit()
def test_run_cleanup_subprocess_relays_injected_command(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    settings = _settings(tmp_path, base_env)
    stdout: list[str] = []
    stderr: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 3, stdout="out\n", stderr="err\n")

    code = run_cleanup_subprocess(
        settings,
        Path("/tmp/log"),
        command=["cleanup"],
        run_command=fake_run,
        stdout_echo=stdout.append,
        stderr_echo=stderr.append,
    )

    assert code == 3
    assert stdout == ["out\n"]
    assert stderr == ["err\n"]


@pytest.mark.unit()
def test_run_cleanup_subprocess_injected_command_times_out(
    tmp_path: Path, base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = _settings(tmp_path, base_env)
    errors: list[object] = []
    stderr: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=1, output="o", stderr="e")

    code = run_cleanup_subprocess(
        settings,
        Path("/tmp/log"),
        run_command=fake_run,
        stdout_echo=lambda _message: None,
        stderr_echo=stderr.append,
        log_error=errors.append,
    )

    assert code == 1
    assert errors and _payload_field(errors[0], "reason") == "cleanup_run_timeout"
    assert any("cleanup run timed out" in message for message in stderr)


@pytest.mark.unit()
def test_run_cleanup_subprocess_uses_streaming_by_default(
    tmp_path: Path, base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, base_env)

    def fake_streaming(
        command: list[str],
        _settings: AppSettings,
        _stdout: Callable[[str], None],
        _stderr: Callable[[str], None],
    ) -> int:
        assert command[-1] == "cleanup-once"
        return 7

    monkeypatch.setattr(cleanup_runtime, "_run_streaming_command", fake_streaming)

    assert run_cleanup_subprocess(settings, Path("/tmp/log")) == 7


@pytest.mark.unit()
def test_run_cleanup_subprocess_streaming_timeout_recovers_lock(
    tmp_path: Path, base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", base_env)
    settings = _settings(tmp_path, base_env)
    errors: list[object] = []

    def fake_streaming(
        command: list[str],
        _settings: AppSettings,
        _stdout: Callable[[str], None],
        _stderr: Callable[[str], None],
    ) -> int:
        raise subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr(cleanup_runtime, "_run_streaming_command", fake_streaming)

    code = run_cleanup_subprocess(
        settings,
        Path("/tmp/log"),
        stderr_echo=lambda _message: None,
        log_error=errors.append,
    )

    assert code == 1
    assert errors and _payload_field(errors[0], "reason") == "cleanup_run_timeout"


def _payload_field(payload: object, field: str) -> JsonValue:
    assert isinstance(payload, dict)
    return cast(dict[str, JsonValue], payload)[field]
