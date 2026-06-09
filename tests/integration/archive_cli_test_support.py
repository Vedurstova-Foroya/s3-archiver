"""Shared archive-command helpers for LocalStack integration tests."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_core._archive_protocols import ArchiveRunLock
from s3_archiver_core.archive import ArchiveRoute, ArchiveRunResult
from s3_archiver_core.archive import run_archive as run_core_archive
from s3_archiver_core.archive_progress import ProgressLogger
from s3_archiver_core.s3 import S3Client
from s3_archiver_localstack_support import (
    is_retryable_localstack_message,
    last_json_object,
)
from s3_archiver_localstack_support.harness import (
    LOCALSTACK_HOST_ENDPOINT,
    LocalstackBucketPair,
    localstack_test_env,
)
from s3_archiver_localstack_support.objects import localstack_s3_client
from typer.testing import CliRunner

RUNNER = CliRunner()
FROZEN_ARCHIVE_RUN_STARTED_AT = datetime(2099, 12, 31, 12, tzinfo=UTC)
_RETRYABLE_LOCALSTACK_ERRORS = (
    "when calling the HeadBucket operation: Not Found",
    "when calling the ListObjectVersions operation: The specified bucket does not exist",
)
type ArchiveSide = Literal["source", "destination"]
type DebugLogger = Callable[[object, str], None]
type JsonObject = dict[str, object]


class ArchivePhasePayload(TypedDict):
    status: str


class ArchiveCommandPayload(TypedDict):
    status: str
    source_bucket: str
    destination_bucket: str
    source_object_count: int
    skipped_object_count: int
    phases: dict[str, ArchivePhasePayload]


class CleanupCommandPayload(TypedDict):
    status: str
    object_count: int
    cleaned_count: int
    removed_manifest_count: int
    failure_count: int


def archive_env(tmp_path: Path, bucket_pair: LocalstackBucketPair) -> dict[str, str]:
    env = localstack_test_env(
        bucket_pair,
        endpoint=os.environ.get("LOCALSTACK_S3_URL", LOCALSTACK_HOST_ENDPOINT),
        log_dir=str(tmp_path / "logs"),
    )
    env["ARCHIVER_PAYLOAD_DETAIL"] = "full"
    return env


def update_single_route_config(
    env: dict[str, str],
    *,
    name: str | None = None,
    parser: str | None = None,
    copy_mode: str | None = None,
    source_path: str | None = None,
    destination_path: str | None = None,
) -> None:
    routes = cast(list[JsonObject], json.loads(env["ARCHIVER_CONFIG_JSON"]))
    route = routes[0]
    if name is not None:
        route["name"] = name
    if parser is not None:
        route["parser"] = parser
    if copy_mode is not None:
        route["copy_mode"] = copy_mode
    source = cast(JsonObject, route["source"])
    destination = cast(JsonObject, route["destination"])
    if source_path is not None:
        source["path"] = source_path
    if destination_path is not None:
        destination["path"] = destination_path
    env["ARCHIVER_CONFIG_JSON"] = json.dumps(routes)


def archive_client(env: Mapping[str, str], side: ArchiveSide) -> S3Client:
    return localstack_s3_client(env, side)


def run_archive_command(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    attempts: int = 3,
) -> ArchiveCommandPayload:
    monkeypatch.setattr(os, "environ", env)
    core_run_archive = run_core_archive

    def run_archive_with_frozen_timestamp(
        routes: tuple[ArchiveRoute, ...],
        *,
        run_timeout: timedelta,
        run_started_at_utc: datetime | None = None,
        run_lock: ArchiveRunLock | None = None,
        debug_logger: DebugLogger | None = None,
        progress_logger: ProgressLogger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ArchiveRunResult:
        _ = (run_started_at_utc, run_lock)
        return core_run_archive(
            routes,
            run_timeout=run_timeout,
            run_started_at_utc=FROZEN_ARCHIVE_RUN_STARTED_AT,
            debug_logger=debug_logger,
            progress_logger=progress_logger,
            clock=clock,
        )

    monkeypatch.setattr(cli_module, "run_archive", run_archive_with_frozen_timestamp)
    for attempt in range(attempts):
        result = RUNNER.invoke(cli_module.app, ["archive-once"])
        if result.exit_code == 0 and result.stderr == "":
            return cast(ArchiveCommandPayload, cast(object, last_json_object(result.stdout)))
        if attempt == attempts - 1 or not _is_retryable_archive_failure(result.stderr):
            assert result.exit_code == 0, result.stderr
        time.sleep(0.5)
    raise AssertionError("archive retry loop exhausted without returning")


def run_cleanup_command(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    attempts: int = 3,
) -> CleanupCommandPayload:
    monkeypatch.setattr(os, "environ", env)
    for attempt in range(attempts):
        result = RUNNER.invoke(cli_module.app, ["cleanup-once"])
        if result.exit_code == 0 and result.stderr == "":
            return cast(CleanupCommandPayload, cast(object, last_json_object(result.stdout)))
        if attempt == attempts - 1 or not _is_retryable_archive_failure(result.stderr):
            assert result.exit_code == 0, result.stderr
        time.sleep(0.5)
    raise AssertionError("cleanup retry loop exhausted without returning")


def _is_retryable_archive_failure(stderr: str) -> bool:
    return is_retryable_localstack_message(
        stderr,
        extra_fragments=_RETRYABLE_LOCALSTACK_ERRORS,
    )
