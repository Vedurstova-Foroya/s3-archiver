"""Archive command integration tests against isolated LocalStack buckets."""

from __future__ import annotations

from pathlib import Path

import pytest
from s3_archiver_core.archive import ArchiveRoute, run_archive
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.s3 import S3TransferCapabilities
from s3_archiver_core.settings import AppSettings
from s3_archiver_localstack_support.harness import LocalstackBucketPair
from s3_archiver_localstack_support.objects import (
    listed_keys,
    put_test_object,
    read_object_text,
    read_tar_gz_members_text,
)

from tests.integration.archive_cli_test_support import (
    FROZEN_ARCHIVE_RUN_STARTED_AT,
    ArchiveCommandPayload,
    update_single_route_config,
)
from tests.integration.archive_cli_test_support import archive_client as _client
from tests.integration.archive_cli_test_support import archive_env as _archive_env
from tests.integration.archive_cli_test_support import run_archive_command as _run_archive

TARGET_DAY = "2099-12-30"
TARGET_ARCHIVE_KEY = f"archive/{TARGET_DAY}.tar.gz"
INCOMPLETE_DAY = "2099-12-31"


@pytest.mark.integration()
def test_archive_command_archives_target_day_keys_without_deleting_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
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
    assert payload["source_object_count"] == len(source_keys)
    assert _phase_statuses(payload) == {
        "list": "ok",
        "copy": "ok",
        "verify": "ok",
    }
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        TARGET_ARCHIVE_KEY
    }
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, TARGET_ARCHIVE_KEY
    ) == {key: f"payload for {key}\n" for key in source_keys}
    assert listed_keys(source_client, localstack_bucket_pair.source) == source_keys


@pytest.mark.integration()
def test_archive_command_route_source_and_destination_paths_control_daily_archives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    update_single_route_config(env, source_path="include/", destination_path="routed/")
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
    assert payload["source_object_count"] == 2
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        f"routed/{TARGET_DAY}.tar.gz",
        f"routed/nested/{TARGET_DAY}.tar.gz",
    }
    assert read_tar_gz_members_text(
        destination_client,
        localstack_bucket_pair.destination,
        f"routed/{TARGET_DAY}.tar.gz",
    ) == {
        f"include/{TARGET_DAY}T00-00-00-a.txt": (
            f"payload for include/{TARGET_DAY}T00-00-00-a.txt\n"
        )
    }
    assert read_tar_gz_members_text(
        destination_client,
        localstack_bucket_pair.destination,
        f"routed/nested/{TARGET_DAY}.tar.gz",
    ) == {
        f"include/nested/{TARGET_DAY}T01-00-00-b.txt": (
            f"payload for include/nested/{TARGET_DAY}T01-00-00-b.txt\n"
        )
    }
    assert listed_keys(source_client, localstack_bucket_pair.source) == included_keys | {
        excluded_key
    }


@pytest.mark.integration()
def test_archive_command_direct_route_copies_source_path_without_deleting_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    update_single_route_config(
        env,
        name="localstack-direct",
        parser="direct",
        copy_mode="direct",
        source_path="raw/",
        destination_path="mirror/",
    )
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    copied_keys = {
        "raw/live-a.txt",
        "raw/nested/live-b.txt",
    }
    skipped_key = "other/live-c.txt"
    for key in copied_keys | {skipped_key}:
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert payload["source_object_count"] == len(copied_keys)
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        f"mirror/{key}" for key in copied_keys
    }
    for key in copied_keys:
        assert (
            read_object_text(
                destination_client, localstack_bucket_pair.destination, f"mirror/{key}"
            )
            == f"payload for {key}\n"
        )
    assert listed_keys(source_client, localstack_bucket_pair.source) == copied_keys | {skipped_key}


@pytest.mark.integration()
def test_archive_command_filename_parser_skips_incomplete_utc_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    prefix = "timestamp-boundary"
    env = _archive_env(tmp_path, localstack_bucket_pair)
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    seed_keys = {
        f"{prefix}/{INCOMPLETE_DAY}T00-00-00-start.txt",
        f"{prefix}/{INCOMPLETE_DAY}T11-59-59-before.txt",
        f"{prefix}/{INCOMPLETE_DAY}T12-00-01-after.txt",
    }
    for key in seed_keys:
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert payload["source_object_count"] == 0
    assert payload["skipped_object_count"] == len(seed_keys)
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == set()
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
    route = settings.routes[0]
    source = S3ArchiveBucket(source_client, localstack_bucket_pair.source, runtime_temp_dir)
    destination = S3ArchiveBucket(
        destination_client, localstack_bucket_pair.destination, runtime_temp_dir
    )
    routes = (
        ArchiveRoute(
            route.name,
            source,
            destination,
            parser_kind=route.parser.value,
            copy_mode=route.copy_mode.value,
            source_path=route.source.path,
            destination_path=route.destination.path,
            source_identity=route.source.storage_identity(),
            destination_identity=route.destination.storage_identity(),
            transfer_capabilities=S3TransferCapabilities(
                native_copy=False,
                multipart_copy=False,
                streaming_upload=True,
                temp_file_backed=True,
                streaming_limit_bytes=1,
            ),
        ),
    )
    decisions: list[str] = []

    result = run_archive(
        routes,
        run_timeout=settings.run_timeout,
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
        if name in {"list", "copy", "verify"}
    }
