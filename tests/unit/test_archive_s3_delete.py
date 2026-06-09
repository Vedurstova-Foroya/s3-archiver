"""Tests for source-object deletion on the S3 archive bucket adapter."""

from __future__ import annotations

import pytest
from s3_archiver_core.archive_s3 import S3ArchiveBucket

from tests.unit.archive_s3_fakes import FakeArchiveClient


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
