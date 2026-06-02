"""Unit tests for narrow S3 branch coverage."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast, final, override

import pytest
from botocore.exceptions import ClientError
from s3_archiver_core import s3_transfer as transfer_module
from s3_archiver_core._archive_s3_helpers import (
    ReadableBody,
    is_not_implemented_error,
    put_object_tags,
)
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.s3 import S3_CHUNK_BYTES, S3Client, S3ObjectProperties

from tests.unit.archive_s3_fakes import (
    FakeArchiveClient,
    FakeClientError,
    client_error,
)


class HeadErrorClient(FakeArchiveClient):
    def __init__(self, response: Mapping[str, object]) -> None:
        super().__init__()
        self.response: Mapping[str, object] = response

    @override
    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        raise PartialClientError(self.response)


@final
class PartialClientError(ClientError):
    def __init__(self, response: Mapping[str, object]) -> None:
        Exception.__init__(self, "partial")
        self.response = response  # pyright: ignore[reportAttributeAccessIssue]


def _minimal_properties(size: int = 1) -> S3ObjectProperties:
    return S3ObjectProperties(
        size,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        {},
        {},
        None,
        {},
        None,
    )


class FailingReadableBody:
    calls: int

    def __init__(self) -> None:
        self.calls = 0

    def read(self, amount: int | None = None) -> bytes:
        _ = amount
        self.calls += 1
        if self.calls == 1:
            return b"partial"
        raise RuntimeError("read failed")


@pytest.mark.unit()
def test_s3_archive_not_found_detection_handles_partial_error_shapes() -> None:
    assert (
        S3ArchiveBucket(
            HeadErrorClient({"Error": "bad", "ResponseMetadata": {"HTTPStatusCode": 404}}),
            "source",
        ).head_object("missing")
        is None
    )
    assert (
        S3ArchiveBucket(
            HeadErrorClient({"Error": {"Code": "404"}, "ResponseMetadata": "bad"}),
            "source",
        ).head_object("missing")
        is None
    )


@pytest.mark.unit()
def test_s3_transfer_omits_version_and_empty_optional_headers() -> None:
    source_client = FakeArchiveClient()
    destination_client = FakeArchiveClient()
    source_client.source_body = b"x"
    source = S3ArchiveBucket(source_client, "source")
    destination = S3ArchiveBucket(destination_client, "destination")

    destination.copy_from(
        source,
        "source",
        "key",
        None,
        _minimal_properties(),
        "key",
        {},
        "multipart_streaming",
    )

    assert source_client.get_call == {"Bucket": "source", "Key": "key"}
    assert destination_client.create_calls[0] == {
        "Bucket": "destination",
        "Key": "key",
        "Metadata": {},
    }


@pytest.mark.unit()
def test_s3_transfer_stage_failure_before_file_creation(tmp_path: Path) -> None:
    blocked_temp_dir = tmp_path / "not-a-directory"
    _ = blocked_temp_dir.write_text("file", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _ = transfer_module._stage(  # pyright: ignore[reportPrivateUsage]
            cast(ReadableBody, cast(object, FailingReadableBody())), blocked_temp_dir
        )

    assert blocked_temp_dir.read_text(encoding="utf-8") == "file"


@pytest.mark.unit()
def test_s3_transfer_stage_removes_temp_file_when_body_read_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="read failed"):
        _ = transfer_module._stage(  # pyright: ignore[reportPrivateUsage]
            cast(ReadableBody, cast(object, FailingReadableBody())), tmp_path
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit()
def test_s3_file_upload_chunk_size_supports_large_archives() -> None:
    size = 90 * 1024 * 1024 * 1024

    chunk_size = _multipart_chunk_size(size)

    assert chunk_size > S3_CHUNK_BYTES
    assert (size + chunk_size - 1) // chunk_size <= transfer_module.S3_MAX_MULTIPART_PARTS


def _multipart_chunk_size(size: int) -> int:
    return cast(
        Callable[[int], int],
        transfer_module.__dict__["_multipart_chunk_size"],
    )(size)


class AbortFailingClient(FakeArchiveClient):
    @override
    def abort_multipart_upload(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        raise RuntimeError("abort failed")


@pytest.mark.unit()
def test_safe_abort_multipart_logs_warning_when_abort_raises() -> None:
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("s3_archiver.archive")
    handler = _RecordHandler(records)
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        client = cast(S3Client, cast(object, AbortFailingClient()))
        transfer_module._safe_abort_multipart(client, "bucket", "key", "upload-id")  # pyright: ignore[reportPrivateUsage]
    finally:
        logger.removeHandler(handler)
    events = [record for record in records if record.message == "multipart abort failed"]
    assert len(events) == 1
    extras = events[0].__dict__
    assert extras["event"] == "archive.multipart.abort_failed"
    assert extras["upload_id"] == "upload-id"


@pytest.mark.unit()
def test_safe_abort_multipart_returns_quietly_when_abort_succeeds() -> None:
    client = FakeArchiveClient()
    transfer_module._safe_abort_multipart(  # pyright: ignore[reportPrivateUsage]
        cast(S3Client, cast(object, client)), "bucket", "key", "upload-id"
    )
    assert client.abort_calls == [{"Bucket": "bucket", "Key": "key", "UploadId": "upload-id"}]


@pytest.mark.unit()
def test_put_tags_skips_when_tag_map_is_empty() -> None:
    client = FakeArchiveClient()
    put_object_tags(cast(S3Client, cast(object, client)), "bucket", "key", {})
    assert client.tagging_calls == []


class TaggingErrorClient(FakeArchiveClient):
    def __init__(self, error: ClientError) -> None:
        super().__init__()
        self.error: ClientError = error

    @override
    def put_object_tagging(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        raise self.error


@pytest.mark.unit()
def test_put_tags_swallows_not_implemented_and_logs_warning() -> None:
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("s3_archiver.archive")
    handler = _RecordHandler(records)
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        client = TaggingErrorClient(client_error("NotImplemented", status=501))
        put_object_tags(cast(S3Client, cast(object, client)), "bucket", "key", {"kind": "source"})
    finally:
        logger.removeHandler(handler)
    events = [
        record
        for record in records
        if record.message == "destination does not support object tagging; tags dropped"
    ]
    assert len(events) == 1
    extras = events[0].__dict__
    assert extras["event"] == "archive.tagging.unsupported"
    assert extras["bucket"] == "bucket"
    assert extras["key"] == "key"


@pytest.mark.unit()
def test_put_tags_reraises_other_client_errors() -> None:
    client = TaggingErrorClient(client_error("AccessDenied", status=403))
    with pytest.raises(ClientError):
        put_object_tags(cast(S3Client, cast(object, client)), "bucket", "key", {"kind": "source"})


@pytest.mark.unit()
def test_is_not_implemented_error_detects_code_and_status() -> None:
    assert is_not_implemented_error(client_error("NotImplemented", status=501)) is True
    assert is_not_implemented_error(client_error("NotImplemented", status=400)) is True
    assert is_not_implemented_error(client_error("AccessDenied", status=501)) is True
    assert is_not_implemented_error(client_error("AccessDenied", status=403)) is False


@pytest.mark.unit()
def test_is_not_implemented_error_handles_malformed_responses() -> None:
    not_implemented_in_garbage = FakeClientError("NotImplemented", 200)
    not_implemented_in_garbage.response = cast(  # pyright: ignore[reportAttributeAccessIssue]
        Mapping[str, object], {"Error": "bad", "ResponseMetadata": "bad"}
    )
    assert is_not_implemented_error(not_implemented_in_garbage) is False


class _RecordHandler(logging.Handler):
    records: list[logging.LogRecord]

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self.records = records

    @override
    def emit(self, record: logging.LogRecord) -> None:
        record.message = record.getMessage()
        self.records.append(record)
