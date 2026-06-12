"""Tests for source-object deletion on the S3 archive bucket adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast, override

import pytest
from botocore.exceptions import ClientError
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.source_deletes import SourceDeleteRequest

from tests.unit.archive_s3_fakes import FakeArchiveClient, client_error


class ConditionalDeleteErrorClient(FakeArchiveClient):
    code: str
    status: int

    def __init__(self, code: str, status: int) -> None:
        super().__init__()
        self.code = code
        self.status = status

    @override
    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        if "IfMatch" in kwargs:
            self.delete_calls.append(kwargs)
            raise client_error(self.code, self.status)
        return super().delete_object(**kwargs)


class ChangedHeadConditionalDeleteClient(ConditionalDeleteErrorClient):
    @override
    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        response = dict(super().head_object(**kwargs))
        response["ETag"] = '"new-etag"'
        return response


@pytest.mark.unit()
def test_delete_source_object_targets_exact_version() -> None:
    client = FakeArchiveClient()
    bucket = S3ArchiveBucket(client, "source")

    bucket.delete_source_object("data/a.xml", "v9")

    assert client.delete_calls == [{"Bucket": "source", "Key": "data/a.xml", "VersionId": "v9"}]


@pytest.mark.unit()
def test_delete_source_object_without_version() -> None:
    client = FakeArchiveClient()
    bucket = S3ArchiveBucket(client, "source")

    bucket.delete_source_object("data/a.xml")

    assert client.delete_calls == [{"Bucket": "source", "Key": "data/a.xml"}]


@pytest.mark.unit()
def test_delete_source_object_can_use_etag_condition() -> None:
    client = FakeArchiveClient()
    bucket = S3ArchiveBucket(client, "source")

    bucket.delete_source_object("data/a.xml", if_match='"etag"')

    assert client.delete_calls == [{"Bucket": "source", "Key": "data/a.xml", "IfMatch": '"etag"'}]


@pytest.mark.unit()
def test_delete_source_object_falls_back_when_etag_condition_is_unsupported() -> None:
    client = ConditionalDeleteErrorClient("NotImplemented", 501)
    bucket = S3ArchiveBucket(client, "source")

    bucket.delete_source_object("data/a.xml", if_match='"etag"')

    assert client.delete_calls == [
        {"Bucket": "source", "Key": "data/a.xml", "IfMatch": '"etag"'},
        {"Bucket": "source", "Key": "data/a.xml"},
    ]


@pytest.mark.unit()
def test_delete_source_object_keeps_non_unsupported_etag_condition_errors() -> None:
    client = ConditionalDeleteErrorClient("AccessDenied", 403)
    bucket = S3ArchiveBucket(client, "source")

    with pytest.raises(ClientError):
        bucket.delete_source_object("data/a.xml", if_match='"etag"')

    assert client.delete_calls == [{"Bucket": "source", "Key": "data/a.xml", "IfMatch": '"etag"'}]


@pytest.mark.unit()
def test_delete_source_object_does_not_fallback_when_object_changed() -> None:
    client = ChangedHeadConditionalDeleteClient("NotImplemented", 501)
    bucket = S3ArchiveBucket(client, "source")

    with pytest.raises(ClientError):
        bucket.delete_source_object("data/a.xml", if_match='"etag"')

    assert client.delete_calls == [{"Bucket": "source", "Key": "data/a.xml", "IfMatch": '"etag"'}]


@pytest.mark.unit()
def test_delete_source_object_keeps_original_error_when_fallback_head_fails() -> None:
    client = ConditionalDeleteErrorClient("NotImplemented", 501)
    client.head_error = client_error("ServiceUnavailable", 503)
    bucket = S3ArchiveBucket(client, "source")

    with pytest.raises(ClientError) as exc_info:
        bucket.delete_source_object("data/a.xml", if_match='"etag"')

    error = cast(Mapping[str, object], exc_info.value.response.get("Error", {}))
    assert error.get("Code") == "NotImplemented"
    assert client.delete_calls == [{"Bucket": "source", "Key": "data/a.xml", "IfMatch": '"etag"'}]


@pytest.mark.unit()
def test_delete_source_objects_batches_versioned_keys() -> None:
    client = FakeArchiveClient()
    bucket = S3ArchiveBucket(client, "source")

    failures = bucket.delete_source_objects(
        (
            SourceDeleteRequest("data/a.xml", "v1"),
            SourceDeleteRequest("data/b.xml", "v2"),
        )
    )

    assert failures == ()
    assert client.delete_objects_calls == [
        {
            "Bucket": "source",
            "Delete": {
                "Objects": [
                    {"Key": "data/a.xml", "VersionId": "v1"},
                    {"Key": "data/b.xml", "VersionId": "v2"},
                ],
                "Quiet": True,
            },
        }
    ]


@pytest.mark.unit()
def test_delete_source_objects_reports_per_key_errors() -> None:
    client = FakeArchiveClient()
    client.delete_objects_response = {
        "Errors": [
            {
                "Key": "data/b.xml",
                "VersionId": "v2",
                "Code": "AccessDenied",
                "Message": "denied",
            }
        ]
    }
    bucket = S3ArchiveBucket(client, "source")

    failures = bucket.delete_source_objects(
        (
            SourceDeleteRequest("data/a.xml", "v1"),
            SourceDeleteRequest("data/b.xml", "v2"),
        )
    )

    assert len(failures) == 1
    assert failures[0].key == "data/b.xml"
    assert failures[0].version_id == "v2"
    assert failures[0].detail == "delete failed: AccessDenied: denied"


@pytest.mark.unit()
def test_delete_source_objects_reports_batch_request_failure_for_each_key() -> None:
    client = FakeArchiveClient()
    client.delete_objects_error = client_error("AccessDenied", 403)
    bucket = S3ArchiveBucket(client, "source")

    failures = bucket.delete_source_objects(
        (
            SourceDeleteRequest("data/a.xml", "v1"),
            SourceDeleteRequest("data/b.xml"),
        )
    )

    assert [(failure.key, failure.version_id) for failure in failures] == [
        ("data/a.xml", "v1"),
        ("data/b.xml", None),
    ]
    assert all("AccessDenied" in failure.detail for failure in failures)
    assert client.delete_objects_calls == []


@pytest.mark.unit()
def test_delete_source_objects_raises_not_implemented_for_rejected_batch_api() -> None:
    client = FakeArchiveClient()
    client.delete_objects_error = client_error("NotImplemented", 501)
    bucket = S3ArchiveBucket(client, "source")

    with pytest.raises(NotImplementedError, match="batch delete is not supported"):
        _ = bucket.delete_source_objects((SourceDeleteRequest("data/a.xml", "v1"),))


@pytest.mark.unit()
def test_delete_source_objects_raises_not_implemented_without_batch_api() -> None:
    client = FakeArchiveClient()
    client.__dict__["delete_objects"] = None
    bucket = S3ArchiveBucket(client, "source")

    with pytest.raises(NotImplementedError, match="batch delete is not supported"):
        _ = bucket.delete_source_objects((SourceDeleteRequest("data/a.xml", "v1"),))


@pytest.mark.unit()
def test_delete_source_objects_ignores_non_list_errors() -> None:
    client = FakeArchiveClient()
    client.delete_objects_response = {"Errors": {"Key": "data/a.xml"}}
    bucket = S3ArchiveBucket(client, "source")

    assert bucket.delete_source_objects((SourceDeleteRequest("data/a.xml", "v1"),)) == ()


@pytest.mark.unit()
def test_delete_source_objects_ignores_malformed_error_entries() -> None:
    client = FakeArchiveClient()
    client.delete_objects_response = {
        "Errors": [
            "not-an-error",
            {"Code": "AccessDenied"},
            {"Key": "data/c.xml", "Code": "AccessDenied", "Message": "AccessDenied"},
        ]
    }
    bucket = S3ArchiveBucket(client, "source")

    failures = bucket.delete_source_objects((SourceDeleteRequest("data/a.xml", "v1"),))

    assert len(failures) == 1
    assert failures[0].key == "data/c.xml"
    assert failures[0].version_id is None
    assert failures[0].detail == "delete failed: AccessDenied"


@pytest.mark.unit()
def test_delete_source_objects_rejects_conditional_batch_delete() -> None:
    client = FakeArchiveClient()
    bucket = S3ArchiveBucket(client, "source")

    with pytest.raises(NotImplementedError, match="conditional"):
        _ = bucket.delete_source_objects((SourceDeleteRequest("data/a.xml", if_match='"etag"'),))
