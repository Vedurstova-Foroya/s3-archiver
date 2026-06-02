"""Tests for OCI Object Storage S3-compatibility quirks.

OCI returns NotImplemented for object tagging and may return it for other
operations that have no equivalent in its own data model (e.g. bucket
versioning). The S3 adapter must degrade gracefully so archive runs can
proceed against OCI.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import override

import pytest
from botocore.exceptions import ClientError
from s3_archiver_core.archive_s3 import S3ArchiveBucket

from tests.unit.archive_s3_fakes import FakeArchiveClient, client_error


class VersioningErrorClient(FakeArchiveClient):
    """Fake client that fails GetBucketVersioning with the configured error."""

    def __init__(self, error: ClientError) -> None:
        super().__init__()
        self.error: ClientError = error

    @override
    def get_bucket_versioning(self, *, Bucket: str) -> Mapping[str, object]:
        _ = Bucket
        raise self.error


class TagErrorClient(FakeArchiveClient):
    """Fake client that fails GetObjectTagging with the configured error."""

    def __init__(self, error: ClientError) -> None:
        super().__init__()
        self.error: ClientError = error

    @override
    def get_object_tagging(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        raise self.error


@pytest.mark.unit()
def test_versioning_state_treats_not_implemented_as_disabled() -> None:
    bucket = S3ArchiveBucket(
        VersioningErrorClient(client_error("NotImplemented", status=501)), "source"
    )

    assert bucket.versioning_state() == "Disabled"


@pytest.mark.unit()
def test_versioning_state_reraises_other_client_errors() -> None:
    bucket = S3ArchiveBucket(
        VersioningErrorClient(client_error("AccessDenied", status=403)), "source"
    )

    with pytest.raises(ClientError):
        _ = bucket.versioning_state()


@pytest.mark.unit()
def test_get_tags_returns_empty_on_not_implemented() -> None:
    bucket = S3ArchiveBucket(TagErrorClient(client_error("NotImplemented", status=501)), "source")

    assert bucket.get_tags("key") == {}


@pytest.mark.unit()
def test_get_tags_reraises_other_client_errors() -> None:
    bucket = S3ArchiveBucket(TagErrorClient(client_error("AccessDenied", status=403)), "source")

    with pytest.raises(ClientError):
        _ = bucket.get_tags("key")
