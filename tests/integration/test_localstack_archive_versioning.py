"""Versioning-focused archive command integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

import pytest
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_localstack_support.harness import LocalstackBucketPair
from s3_archiver_localstack_support.objects import (
    listed_key_versions,
    listed_keys,
    put_test_object,
    read_object_text,
    read_tar_gz_members_text,
)

from tests.integration.archive_cli_test_support import archive_client as _client
from tests.integration.archive_cli_test_support import archive_env as _archive_env
from tests.integration.archive_cli_test_support import run_archive_command as _run_archive

TARGET_DAY = "2099-12-30"


class DeleteObjectClient(Protocol):
    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        """Delete one test setup object."""
        ...


@pytest.mark.integration()
def test_archive_command_archives_latest_version_without_deleting_source_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    _ = source_client.put_bucket_versioning(
        Bucket=localstack_bucket_pair.source,
        VersioningConfiguration={"Status": "Enabled"},
    )
    key = f"versioned/{TARGET_DAY}T00-00-00-history.txt"
    archive_key = f"versioned/{TARGET_DAY}.tar.gz"
    first = put_test_object(source_client, localstack_bucket_pair.source, key, body=b"first\n")
    second = put_test_object(source_client, localstack_bucket_pair.source, key, body=b"second\n")

    payload = _run_archive(monkeypatch, env, attempts=6)

    assert payload["status"] == "ok"
    assert payload["source_object_count"] == 1
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, archive_key
    ) == {key: "second\n"}
    assert read_object_text(source_client, localstack_bucket_pair.source, key) == "second\n"
    versions = listed_key_versions(source_client, localstack_bucket_pair.source, key)
    assert (key, str(first["VersionId"]), False) in versions
    assert (key, str(second["VersionId"]), True) in versions


@pytest.mark.integration()
def test_archive_command_rerun_verifies_existing_archive_without_deleting_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    key = f"versioned/{TARGET_DAY}T00-00-00-rerun.txt"
    archive_key = f"versioned/{TARGET_DAY}.tar.gz"
    first_env = _archive_env(tmp_path, localstack_bucket_pair)
    source_client = _client(first_env, "source")
    destination_client = _client(first_env, "destination")
    _ = source_client.put_bucket_versioning(
        Bucket=localstack_bucket_pair.source,
        VersioningConfiguration={"Status": "Enabled"},
    )
    archived = put_test_object(
        source_client, localstack_bucket_pair.source, key, body=b"archived-version\n"
    )

    first_payload = _run_archive(monkeypatch, first_env)

    assert first_payload["status"] == "ok"
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, archive_key
    ) == {key: "archived-version\n"}
    rerun_env = dict(first_env)

    rerun_payload = _run_archive(monkeypatch, rerun_env)

    assert rerun_payload["status"] == "ok"
    assert rerun_payload["source_object_count"] == 1
    destination_metadata = cast(
        dict[str, str],
        destination_client.head_object(Bucket=localstack_bucket_pair.destination, Key=archive_key)[
            "Metadata"
        ],
    )
    assert destination_metadata["s3-archiver-source-count"] == "1"
    assert read_tar_gz_members_text(
        destination_client, localstack_bucket_pair.destination, archive_key
    ) == {key: "archived-version\n"}
    assert listed_keys(source_client, localstack_bucket_pair.source) == {key}
    assert (
        read_object_text(source_client, localstack_bucket_pair.source, key) == "archived-version\n"
    )
    versions = listed_key_versions(source_client, localstack_bucket_pair.source, key)
    assert (key, str(archived["VersionId"]), True) in versions


@pytest.mark.integration()
def test_versioned_listing_paginates_across_pages_and_filters_delete_markers(
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    source_client = _client(env, "source")
    delete_client = cast(DeleteObjectClient, cast(object, source_client))
    _ = source_client.put_bucket_versioning(
        Bucket=localstack_bucket_pair.source,
        VersioningConfiguration={"Status": "Enabled"},
    )
    early_deleted_keys = {f"aaa-deleted-{index:04d}.txt" for index in range(2)}
    live_keys = {f"mmm-live-{index:04d}.txt" for index in range(998)}
    late_deleted_keys = {f"zzz-deleted-{index:04d}.txt" for index in range(2)}
    for key in sorted(early_deleted_keys):
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)
        _ = delete_client.delete_object(Bucket=localstack_bucket_pair.source, Key=key)
    for key in sorted(live_keys):
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)
    for key in sorted(late_deleted_keys):
        _ = put_test_object(source_client, localstack_bucket_pair.source, key)
        _ = delete_client.delete_object(Bucket=localstack_bucket_pair.source, Key=key)

    first_page = source_client.list_object_versions(
        Bucket=localstack_bucket_pair.source,
        MaxKeys=1000,
    )
    second_page = source_client.list_object_versions(
        Bucket=localstack_bucket_pair.source,
        MaxKeys=1000,
        KeyMarker=str(first_page["NextKeyMarker"]),
        VersionIdMarker=str(first_page["NextVersionIdMarker"]),
    )
    listed = list(
        S3ArchiveBucket(source_client, localstack_bucket_pair.source).list_source_objects("Enabled")
    )

    assert first_page["IsTruncated"] is True
    assert _delete_marker_keys(first_page) == early_deleted_keys
    assert _delete_marker_keys(second_page) == late_deleted_keys
    assert {entry.key for entry in listed} == live_keys
    assert len(listed) == len(live_keys)
    assert all(entry.version_id is not None for entry in listed)


def _delete_marker_keys(page: Mapping[str, object]) -> set[str]:
    delete_markers = page.get("DeleteMarkers")
    if not isinstance(delete_markers, list):
        return set()
    keys: set[str] = set()
    for raw_marker in cast(list[object], delete_markers):
        if not isinstance(raw_marker, dict):
            continue
        marker = cast(dict[str, object], raw_marker)
        if marker.get("Key") is not None:
            keys.add(str(marker["Key"]))
    return keys
