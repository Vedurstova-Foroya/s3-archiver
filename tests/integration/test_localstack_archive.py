"""Archive command integration tests against isolated LocalStack buckets."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from s3_archiver_core.archive import run_archive
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.s3 import S3TransferCapabilities
from s3_archiver_core.settings import AppSettings

from tests.integration.archive_cli_test_support import (
    FROZEN_ARCHIVE_RUN_STARTED_AT,
    ArchiveCommandPayload,
)
from tests.integration.archive_cli_test_support import archive_client as _client
from tests.integration.archive_cli_test_support import archive_env as _archive_env
from tests.integration.archive_cli_test_support import run_archive_command as _run_archive
from tests.integration.localstack_harness import LocalstackBucketPair
from tests.integration.localstack_object_helpers import (
    listed_keys,
    put_test_object,
    read_tar_gz_members_text,
)

TARGET_DAY = "2099-12-31"
TARGET_ARCHIVE_KEY = f"archive/{TARGET_DAY}.tar.gz"


@pytest.mark.integration()
@pytest.mark.parametrize("cleanup_value", [None, "false", "true"])
def test_archive_command_archives_target_day_keys_and_honors_cleanup_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
    cleanup_value: str | None,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    if cleanup_value is None:
        del env["ARCHIVER_ENABLE_CLEANUP"]
    else:
        env["ARCHIVER_ENABLE_CLEANUP"] = cleanup_value
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    source_keys = {
        f"archive/{TARGET_DAY}T00-00-00-a.txt",
        f"archive/{TARGET_DAY}T01-00-00-b.txt",
    }
    for key in source_keys:
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert payload["source_bucket"] == localstack_bucket_pair.source
    assert payload["destination_bucket"] == localstack_bucket_pair.destination
    assert payload["manifest"]["object_count"] == len(source_keys)
    expected_cleanup_status = "ok" if cleanup_value == "true" else "skipped"
    assert _phase_statuses(payload) == {
        "list": "ok",
        "copy": "ok",
        "verify": "ok",
        "cleanup": expected_cleanup_status,
    }
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        TARGET_ARCHIVE_KEY
    }
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, TARGET_ARCHIVE_KEY
    ) == {key: f"payload for {key}\n" for key in source_keys}
    expected_source_keys: set[str] = set() if cleanup_value == "true" else source_keys
    assert listed_keys(source_client, localstack_bucket_pair.source) == expected_source_keys


@pytest.mark.integration()
def test_archive_command_whitelist_filter_controls_copy_and_cleanup_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    env["ARCHIVER_ENABLE_CLEANUP"] = "true"
    env["S3_SOURCE_PATH_WHITELIST_ENABLED"] = "true"
    env["S3_SOURCE_PATH_WHITELIST"] = json.dumps(["include/"])
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    included_keys = {
        f"include/{TARGET_DAY}T00-00-00-a.txt",
        f"include/nested/{TARGET_DAY}T01-00-00-b.txt",
    }
    excluded_key = f"exclude/{TARGET_DAY}T02-00-00-c.txt"
    for key in included_keys | {excluded_key}:
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert payload["manifest"]["object_count"] == 2
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        f"include/{TARGET_DAY}.tar.gz",
        f"include/nested/{TARGET_DAY}.tar.gz",
    }
    assert read_tar_gz_members_text(
        destination_client,
        localstack_bucket_pair.destination,
        f"include/{TARGET_DAY}.tar.gz",
    ) == {
        f"include/{TARGET_DAY}T00-00-00-a.txt": (
            f"payload for include/{TARGET_DAY}T00-00-00-a.txt\n"
        )
    }
    assert read_tar_gz_members_text(
        destination_client,
        localstack_bucket_pair.destination,
        f"include/nested/{TARGET_DAY}.tar.gz",
    ) == {
        f"include/nested/{TARGET_DAY}T01-00-00-b.txt": (
            f"payload for include/nested/{TARGET_DAY}T01-00-00-b.txt\n"
        )
    }
    assert listed_keys(source_client, localstack_bucket_pair.source) == {excluded_key}


@pytest.mark.integration()
def test_archive_command_blacklist_filter_controls_copy_and_cleanup_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    env["ARCHIVER_ENABLE_CLEANUP"] = "true"
    env["S3_SOURCE_PATH_BLACKLIST_ENABLED"] = "true"
    env["S3_SOURCE_PATH_BLACKLIST"] = json.dumps(["blocked/"])
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    allowed_key = f"allowed/{TARGET_DAY}T00-00-00-a.txt"
    blocked_keys = {
        f"blocked/{TARGET_DAY}T01-00-00-b.txt",
        f"blocked/nested/{TARGET_DAY}T02-00-00-c.txt",
    }
    for key in {allowed_key} | blocked_keys:
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert payload["manifest"]["object_count"] == 1
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        f"allowed/{TARGET_DAY}.tar.gz"
    }
    assert read_tar_gz_members_text(
        destination_client,
        localstack_bucket_pair.destination,
        f"allowed/{TARGET_DAY}.tar.gz",
    ) == {allowed_key: f"payload for {allowed_key}\n"}
    assert listed_keys(source_client, localstack_bucket_pair.source) == blocked_keys


@pytest.mark.integration()
@pytest.mark.parametrize(
    ("retention_days", "expected_days"),
    [(1, ("2099-12-31", "2099-11-02", "2099-11-01")), (60, ("2099-11-02", "2099-11-01"))],
)
def test_archive_command_retention_matrix_selects_each_eligible_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
    retention_days: int,
    expected_days: tuple[str, ...],
) -> None:
    prefix = "retention-boundary"
    env = _archive_env(tmp_path, localstack_bucket_pair)
    env["ARCHIVER_RETENTION_DAYS"] = str(retention_days)
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    seed_keys = {
        f"{prefix}/2099-12-31T00-00-00.txt",
        f"{prefix}/2099-11-02T00-00-00.txt",
        f"{prefix}/2099-11-01T00-00-00.txt",
    }
    for key in seed_keys:
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert payload["manifest"]["object_count"] == len(expected_days)
    archive_keys = {f"{prefix}/{day}.tar.gz" for day in expected_days}
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == archive_keys
    for day in expected_days:
        source_key = f"{prefix}/{day}T00-00-00.txt"
        assert read_tar_gz_members_text(
            destination_client, localstack_bucket_pair.destination, f"{prefix}/{day}.tar.gz"
        ) == {source_key: f"payload for {source_key}\n"}
    assert listed_keys(source_client, localstack_bucket_pair.source) == seed_keys


@pytest.mark.integration()
def test_archive_core_uses_temp_file_backed_transfer_against_localstack(
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    settings = AppSettings.from_env(env)
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    key = f"temp-file-backed/{TARGET_DAY}T00-00-00-runtime.txt"
    archive_key = f"temp-file-backed/{TARGET_DAY}.tar.gz"
    runtime_temp_dir = tmp_path / "runtime-temp"
    _ = put_test_object(source_client, localstack_bucket_pair.source, key, body=b"temp-file\n")
    options = replace(
        ArchiveOptions.from_settings(settings),
        transfer_capabilities=S3TransferCapabilities(
            native_copy=False,
            multipart_copy=False,
            streaming_upload=True,
            temp_file_backed=True,
            streaming_limit_bytes=1,
        ),
    )
    decisions: list[str] = []

    result = run_archive(
        S3ArchiveBucket(source_client, localstack_bucket_pair.source, runtime_temp_dir),
        S3ArchiveBucket(destination_client, localstack_bucket_pair.destination, runtime_temp_dir),
        options,
        run_started_at_utc=FROZEN_ARCHIVE_RUN_STARTED_AT,
        debug_logger=lambda _entry, strategy: decisions.append(strategy),
    )

    assert result.ok is True
    assert decisions == ["deterministic_tar_gzip"]
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {archive_key}
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, archive_key
    ) == {key: "temp-file\n"}
    assert listed_keys(source_client, localstack_bucket_pair.source) == {key}
    assert not runtime_temp_dir.exists() or list(runtime_temp_dir.iterdir()) == []


def _phase_statuses(payload: ArchiveCommandPayload) -> dict[str, str]:
    return {
        name: phase["status"]
        for name, phase in payload["phases"].items()
        if name in {"list", "copy", "verify", "cleanup"}
    }
