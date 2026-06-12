"""Cleanup command integration tests against isolated LocalStack buckets.

These exercise real source-object deletion end to end: archive writes a
cleanup-input manifest, cleanup deletes the archived source objects, verifies
they are gone, and retires both manifests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import s3_archiver_cli.main as cli_module
from s3_archiver_localstack_support import last_json_object
from s3_archiver_localstack_support.harness import LocalstackBucketPair
from s3_archiver_localstack_support.objects import listed_keys, put_test_object
from typer.testing import CliRunner

from tests.integration.archive_cli_test_support import archive_client as _client
from tests.integration.archive_cli_test_support import archive_env as _archive_env
from tests.integration.archive_cli_test_support import run_archive_command as _run_archive
from tests.integration.archive_cli_test_support import run_cleanup_command as _run_cleanup

TARGET_DAY = "2099-12-30"
TARGET_ARCHIVE_KEY = f"archive/{TARGET_DAY}.tar.gz"
RUNNER = CliRunner()


def _pending_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "cleanup" / "pending"


def _cleaned_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "cleanup" / "cleaned"


@pytest.mark.integration()
def test_cleanup_command_deletes_archived_source_objects(
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

    archive_payload = _run_archive(monkeypatch, env)
    assert archive_payload["status"] == "ok"
    assert len(list(_pending_dir(tmp_path).glob("*.jsonl"))) == 1
    assert listed_keys(source_client, localstack_bucket_pair.source) == source_keys

    cleanup_payload = _run_cleanup(monkeypatch, env)

    assert cleanup_payload["status"] == "ok"
    assert cleanup_payload["object_count"] == len(source_keys)
    assert cleanup_payload["cleaned_count"] == len(source_keys)
    assert cleanup_payload["removed_manifest_count"] == 1
    assert cleanup_payload["failure_count"] == 0
    assert listed_keys(source_client, localstack_bucket_pair.source) == set()
    assert listed_keys(destination_client, localstack_bucket_pair.destination) == {
        TARGET_ARCHIVE_KEY
    }
    assert list(_pending_dir(tmp_path).glob("*.jsonl")) == []
    assert list(_cleaned_dir(tmp_path).glob("*.jsonl")) == []


@pytest.mark.integration()
def test_cleanup_command_is_empty_without_pending_manifests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    _ = put_test_object(
        _client(env, "source"), localstack_bucket_pair.source, f"archive/{TARGET_DAY}T00-00-00.txt"
    )
    monkeypatch.setattr(os, "environ", env)

    result = RUNNER.invoke(cli_module.app, ["cleanup-once"])

    payload = last_json_object(result.stderr)
    assert result.exit_code == 1
    assert payload["status"] == "empty"
    assert payload["reason"] == "cleanup_manifest_empty"
    assert listed_keys(_client(env, "source"), localstack_bucket_pair.source) == {
        f"archive/{TARGET_DAY}T00-00-00.txt"
    }
