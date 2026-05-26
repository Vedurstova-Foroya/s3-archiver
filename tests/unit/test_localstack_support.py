"""Unit tests for shared LocalStack support helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast, override

import pytest
import s3_archiver_localstack_support.buckets as bucket_module
import s3_archiver_localstack_support.harness as harness_module
import s3_archiver_localstack_support.objects as object_module
from botocore.exceptions import ClientError, EndpointConnectionError
from s3_archiver_core.settings import AppSettings, S3LocationSettings
from s3_archiver_localstack_support.harness import LocalstackBucketPair

from tests.unit.localstack_support_fakes import FakeAdminClient, FakeObjectClient, as_s3_client
from tests.unit.localstack_support_fakes import tar_gz_payload as _tar_gz_payload

pytestmark = pytest.mark.unit()

HarnessClient = harness_module.LocalstackS3AdminClient


def test_bucket_pair_lifecycle_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = LocalstackBucketPair("source", "destination")
    client = FakeAdminClient()
    created: list[str] = []
    deleted: list[str] = []

    def record_create(_client: HarnessClient, bucket: str) -> None:
        created.append(bucket)

    def record_delete(_client: HarnessClient, bucket: str) -> None:
        deleted.append(bucket)

    monkeypatch.setattr(bucket_module, "ensure_localstack_bucket", record_create)
    monkeypatch.setattr(bucket_module, "delete_localstack_bucket", record_delete)

    bucket_module.ensure_localstack_bucket_pair(client, pair)
    bucket_module.delete_localstack_bucket_pair(client, pair)

    assert created == ["source", "destination"]
    assert deleted == ["source", "destination"]
    pair_env = harness_module.localstack_test_env(
        pair,
        endpoint=harness_module.LOCALSTACK_HOST_ENDPOINT,
        log_dir="/tmp/logs",
    )

    def fake_build_client(_location: object) -> FakeAdminClient:
        return client

    monkeypatch.setattr(bucket_module, "build_s3_client", fake_build_client)
    settings = AppSettings.from_env(pair_env)
    assert bucket_module.localstack_admin_client(settings) is client


def test_bucket_pair_delete_aggregates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = LocalstackBucketPair("source", "destination")

    def fail_delete(_client: object, bucket: str) -> None:
        raise RuntimeError(f"{bucket} failed")

    monkeypatch.setattr(bucket_module, "delete_localstack_bucket", fail_delete)

    with pytest.raises(RuntimeError, match=r"demo buckets.*source failed.*destination failed"):
        bucket_module.delete_localstack_bucket_pair(FakeAdminClient(), pair, context="demo buckets")


def test_harness_env_and_bucket_cleanup_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pair = LocalstackBucketPair("source", "destination")
    endpoint = harness_module.LOCALSTACK_HOST_ENDPOINT
    env = harness_module.localstack_test_env(
        pair,
        endpoint=endpoint,
        log_dir="/tmp/logs",
    )
    pair_env = {"TEST_S3_SOURCE_BUCKET": "source", "TEST_S3_DESTINATION_BUCKET": "destination"}
    assert harness_module.bucket_pair_from_env(pair_env) == pair
    assert env["ARCHIVER_CONFIG_JSON"].startswith('[{"name":"localstack-daily"')
    assert harness_module.compose_runtime_log_dir(pair).endswith("/source")
    assert (
        harness_module.write_localstack_env_file(
            tmp_path,
            pair,
            endpoint=endpoint,
            log_dir="/tmp/logs",
        )
    ).exists()
    with pytest.raises(RuntimeError, match="S3_SOURCE_PROVIDER"):
        harness_module.assert_localstack_test_target({**env, "S3_SOURCE_PROVIDER": "aws"})
    missing_endpoint_env = {**env}
    del missing_endpoint_env["S3_SOURCE_ENDPOINT"]
    with pytest.raises(RuntimeError, match="must be set"):
        harness_module.assert_localstack_test_target(missing_endpoint_env)
    with pytest.raises(RuntimeError, match="not allowed"):
        harness_module.assert_localstack_test_target(
            {**env, "S3_SOURCE_ENDPOINT": "https://example.com"}
        )

    client = FakeAdminClient()
    client.version_pages = [
        {
            "Versions": [{"Key": "versioned", "VersionId": "1"}],
            "DeleteMarkers": [
                {"Key": "deleted", "VersionId": "2"},
                {"Key": "missing-version"},
                {"VersionId": "missing-key"},
            ],
            "IsTruncated": True,
            "NextKeyMarker": "versioned",
            "NextVersionIdMarker": "1",
        },
        {"IsTruncated": False},
    ]
    client.object_pages = [
        {"Contents": [{"Key": "current"}], "IsTruncated": True, "NextContinuationToken": "next"},
        {"Contents": [], "IsTruncated": False},
    ]
    harness_module.delete_localstack_bucket(client, "bucket")
    assert client.deleted_buckets == ["bucket"]
    assert client.deleted_objects

    harness_time = cast(ModuleType, harness_module.__dict__["time"])

    def ignore_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(harness_time, "sleep", ignore_sleep)
    retry_client = FakeAdminClient()
    retry_client.delete_bucket_errors = [
        EndpointConnectionError(endpoint_url="http://127.0.0.1:4566")
    ]
    harness_module.delete_localstack_bucket(retry_client, "bucket")
    assert retry_client.deleted_buckets == ["bucket"]


def test_harness_bucket_error_paths() -> None:
    existing = ClientError({"Error": {"Code": "BucketAlreadyExists"}}, "CreateBucket")
    bad_create = ClientError({"Error": {"Code": "AccessDenied"}}, "CreateBucket")
    missing = ClientError({"Error": {"Code": "NoSuchBucket"}}, "DeleteBucket")
    denied = ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteBucket")

    class CreateClient(FakeAdminClient):
        def __init__(self, error: ClientError) -> None:
            super().__init__()
            self.error: ClientError = error

        @override
        def create_bucket(self, *, Bucket: str) -> object:
            _ = Bucket
            raise self.error

    harness_module.ensure_localstack_bucket(CreateClient(existing), "bucket")
    with pytest.raises(ClientError):
        harness_module.ensure_localstack_bucket(CreateClient(bad_create), "bucket")

    missing_client = FakeAdminClient()
    missing_client.delete_bucket_errors = [missing]
    harness_module.delete_localstack_bucket(missing_client, "bucket")

    denied_client = FakeAdminClient()
    denied_client.delete_bucket_errors = [denied]
    with pytest.raises(RuntimeError, match="Failed to delete"):
        harness_module.delete_localstack_bucket(denied_client, "bucket")


def test_object_helpers_cover_listing_reads_tags_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeObjectClient()
    payload = _tar_gz_payload({"a.txt": b"a", "empty": b""}, directories=("folder",))
    client.objects = {"plain": b"hello", "archive": payload}
    client.object_pages = [
        {"Contents": ["not-an-object", {"NotKey": "ignored"}, {"Key": "a"}], "IsTruncated": True},
        {"Contents": [{"Key": "b"}], "IsTruncated": False},
    ]
    client.version_payload = {
        "Versions": [
            "not-an-object",
            {"Key": "a", "VersionId": "1", "IsLatest": True},
            {"Key": "b", "VersionId": "2", "IsLatest": False},
        ]
    }
    retry_client = FakeObjectClient()
    retry_client.put_errors = [EndpointConnectionError(endpoint_url="http://127.0.0.1:4566")]
    object_time = cast(ModuleType, object_module.__dict__["time"])

    def ignore_object_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(object_time, "sleep", ignore_object_sleep)
    assert object_module.put_test_object(as_s3_client(retry_client), "bucket", "retry") == {
        "ETag": "etag"
    }

    response = object_module.put_test_object(
        as_s3_client(client),
        "bucket",
        "key",
        tags={"b": "2", "a": "1"},
    )

    assert response == {"ETag": "etag"}
    assert client.tagging_calls[0]["Tagging"] == {
        "TagSet": [{"Key": "a", "Value": "1"}, {"Key": "b", "Value": "2"}]
    }
    assert object_module.listed_keys(as_s3_client(client), "bucket") == {"a", "b"}
    assert object_module.listed_key_versions(as_s3_client(client), "bucket", "a") == [
        ("a", "1", True)
    ]
    assert object_module.read_object_text(as_s3_client(client), "bucket", "plain") == "hello"
    assert object_module.read_object_bytes(as_s3_client(client), "bucket", "plain") == b"hello"
    assert object_module.read_tar_gz_members_text(as_s3_client(client), "bucket", "archive") == {
        "a.txt": "a",
        "empty": "",
    }
    assert object_module.read_tar_gz_member_pax_headers(
        as_s3_client(client), "bucket", "archive"
    ) == {"a.txt": {}, "empty": {}, "folder": {}}

    bad_list_client = FakeObjectClient()
    bad_list_client.object_pages = [{"IsTruncated": True}]
    with pytest.raises(AssertionError, match="truncated empty"):
        _ = object_module.listed_keys(as_s3_client(bad_list_client), "bucket")

    denied_client = FakeObjectClient()
    denied_client.put_errors = [ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")]
    with pytest.raises(ClientError):
        _ = object_module.put_test_object(as_s3_client(denied_client), "bucket", "denied")


def test_localstack_s3_client_builds_selected_side(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = LocalstackBucketPair("source", "destination")
    env = harness_module.localstack_test_env(
        pair,
        endpoint=harness_module.LOCALSTACK_HOST_ENDPOINT,
        log_dir="/tmp/logs",
    )
    client = FakeObjectClient()
    requested_providers: list[str] = []

    def fake_build_s3_client(location: S3LocationSettings) -> FakeObjectClient:
        requested_providers.append(location.provider.value)
        return client

    monkeypatch.setattr(object_module, "build_s3_client", fake_build_s3_client)

    assert object_module.localstack_s3_client(env, "source") is client
    assert object_module.localstack_s3_client(env, "destination") is client
    assert requested_providers == ["localstack", "localstack"]


def test_seed_helpers() -> None:
    client = FakeObjectClient()
    seed_now = datetime(2026, 4, 24, tzinfo=UTC)
    object_module.seed_timestamped_objects(
        as_s3_client(client),
        "bucket",
        prefix="prefix",
        days=(1, 2),
        seed_now=seed_now,
    )
    assert [call["Key"] for call in client.put_calls] == [
        "prefix/age-1-days.txt",
        "prefix/age-2-days.txt",
    ]
    object_module.seed_canonical_route_dataset(
        as_s3_client(client),
        "bucket",
        prefix="all",
        seed_now=seed_now,
    )
    assert object_module.route_dataset_keys("prefix", days=(1, 2)) == {
        "prefix/age-1-days.txt",
        "prefix/age-2-days.txt",
    }
    assert object_module.archive_eligible_days(1, days=(0, 1, 2)) == {2}
