"""Unit tests for S3 archive adapter and transfer edge cases."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import override

import pytest
from botocore.exceptions import ClientError
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.s3 import S3ObjectProperties

from tests.unit.archive_s3_fakes import FakeArchiveClient, client_error, properties


class VersioningClient(FakeArchiveClient):
    """Fake client with configurable versioning status."""

    def __init__(self, status: str | None) -> None:
        super().__init__()
        self.status: str | None = status

    @override
    def get_bucket_versioning(self, *, Bucket: str) -> Mapping[str, object]:
        response: dict[str, object] = {"Bucket": Bucket}
        if self.status is not None:
            response["Status"] = self.status
        return response


class StaticPageClient(FakeArchiveClient):
    """Fake client returning one configured list page."""

    def __init__(self, page: Mapping[str, object]) -> None:
        super().__init__()
        self.page: Mapping[str, object] = page

    @override
    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        self.list_v2_calls.append(kwargs)
        return self.page


class TagShapeClient(FakeArchiveClient):
    """Fake client returning a configured tag response."""

    def __init__(self, response: Mapping[str, object]) -> None:
        super().__init__()
        self.response: Mapping[str, object] = response

    @override
    def get_object_tagging(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return self.response


class HeadShapeClient(FakeArchiveClient):
    """Fake client returning a configured head response."""

    head_call: dict[str, object]

    def __init__(self, response: Mapping[str, object]) -> None:
        super().__init__()
        self.response: Mapping[str, object] = response

    @override
    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        self.head_call = dict(kwargs)
        return self.response


class ChecksumRetryClient(FakeArchiveClient):
    """Fake client that rejects checksum mode once, then succeeds without it."""

    head_call: dict[str, object]

    @override
    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        self.head_call = dict(kwargs)
        if kwargs.get("ChecksumMode") == "ENABLED":
            raise client_error("AccessDenied", status=403)
        return super().head_object(**kwargs)


class ChecksumUnsupportedClient(FakeArchiveClient):
    @override
    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        if kwargs.get("ChecksumMode") == "ENABLED":
            raise ClientError({"Error": {"Code": "InternalError"}}, "HeadObject")
        return super().head_object(**kwargs)


class ChecksumRetryNotFoundClient(FakeArchiveClient):
    @override
    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        if kwargs.get("ChecksumMode") == "ENABLED":
            raise client_error("AccessDenied", status=403)
        raise client_error("NoSuchKey")


@pytest.mark.unit()
def test_s3_archive_bucket_versioning_state_defaults_to_disabled() -> None:
    assert S3ArchiveBucket(VersioningClient("Enabled"), "source").versioning_state() == "Enabled"
    assert S3ArchiveBucket(VersioningClient(None), "source").versioning_state() == "Disabled"


@pytest.mark.unit()
def test_s3_archive_bucket_tag_parsing_ignores_invalid_shapes() -> None:
    assert S3ArchiveBucket(TagShapeClient({"TagSet": "bad"}), "source").get_tags("key") == {}
    assert S3ArchiveBucket(
        TagShapeClient(
            {
                "TagSet": [
                    object(),
                    {"Key": "kept", "Value": 3},
                    {"Key": "missing-value"},
                ]
            }
        ),
        "source",
    ).get_tags("key") == {"kept": "3"}


@pytest.mark.unit()
def test_s3_archive_bucket_rejects_non_s3_archive_source() -> None:
    bucket = S3ArchiveBucket(FakeArchiveClient(), "destination")

    with pytest.raises(TypeError, match="requires an S3ArchiveBucket source"):
        bucket.copy_from(
            object(),
            "source",
            "key",
            None,
            properties(),
            "key",
            {},
            "simple_native_copy",
        )


@pytest.mark.unit()
def test_s3_archive_bucket_list_and_head_coerce_s3_shapes() -> None:
    listed = tuple(
        S3ArchiveBucket(
            StaticPageClient(
                {
                    "IsTruncated": False,
                    "Contents": [
                        object(),
                        {
                            "Key": "from-string.txt",
                            "Size": "10",
                            "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
                        },
                    ],
                }
            ),
            "source",
        ).list_source_objects("Disabled")
    )
    assert [(item.key, item.size, item.etag) for item in listed] == [("from-string.txt", 10, None)]

    assert (
        tuple(
            S3ArchiveBucket(
                StaticPageClient({"IsTruncated": False, "Contents": "bad"}),
                "source",
            ).list_source_objects("Disabled")
        )
        == ()
    )

    with pytest.raises(TypeError, match="expected integer-compatible"):
        _ = tuple(
            S3ArchiveBucket(
                StaticPageClient(
                    {
                        "IsTruncated": False,
                        "Contents": [
                            {
                                "Key": "bad-size.txt",
                                "Size": object(),
                                "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
                            }
                        ],
                    }
                ),
                "source",
            ).list_source_objects("Disabled")
        )


@pytest.mark.unit()
def test_s3_archive_bucket_rejects_truncated_empty_unversioned_page() -> None:
    bucket = S3ArchiveBucket(StaticPageClient({"IsTruncated": True, "Contents": []}), "source")

    with pytest.raises(RuntimeError, match="truncated empty page"):
        _ = tuple(bucket.list_source_objects("Disabled"))


@pytest.mark.unit()
def test_s3_archive_bucket_head_defaults_and_coerces_metadata() -> None:
    bucket = S3ArchiveBucket(
        HeadShapeClient({"ETag": 3, "Metadata": "bad"}),
        "source",
    )

    assert bucket.head_object("key") == S3ObjectProperties(
        0, "3", None, None, None, None, None, None, {}, {"kind": "source"}, None, {}
    )


@pytest.mark.unit()
def test_s3_archive_bucket_head_collects_last_modified_and_checksums() -> None:
    client = HeadShapeClient(
        {
            "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
            "ChecksumSHA256": "sha256-value",
            "ChecksumCRC32": "crc32-value",
        }
    )
    bucket = S3ArchiveBucket(client, "source")

    properties = bucket.head_object("key")

    assert properties is not None
    assert client.head_call["ChecksumMode"] == "ENABLED"
    assert properties.last_modified == datetime(2024, 1, 1, tzinfo=UTC)
    assert properties.checksums == {"sha256": "sha256-value", "crc32": "crc32-value"}


@pytest.mark.unit()
def test_s3_archive_bucket_head_retries_without_checksum_mode_on_compatibility_error() -> None:
    client = ChecksumRetryClient()
    bucket = S3ArchiveBucket(client, "source")

    properties = bucket.head_object("key")

    assert properties is not None
    assert client.head_call == {"Bucket": "source", "Key": "key"}
    assert properties.checksums == {}


@pytest.mark.unit()
def test_s3_archive_bucket_head_reraises_non_compatibility_checksum_error() -> None:
    bucket = S3ArchiveBucket(ChecksumUnsupportedClient(), "source")

    with pytest.raises(ClientError):
        _ = bucket.head_object("key")


@pytest.mark.unit()
def test_s3_archive_bucket_head_returns_none_when_retry_without_checksum_mode_is_missing() -> None:
    bucket = S3ArchiveBucket(ChecksumRetryNotFoundClient(), "source")

    assert bucket.head_object("key") is None
