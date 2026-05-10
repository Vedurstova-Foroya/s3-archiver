from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from io import BytesIO

from botocore.exceptions import ClientError
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.archive_transfer import TransferStrategy
from s3_archiver_core.s3 import S3ObjectProperties


class FakeArchiveClient:
    def __init__(self) -> None:
        self.list_v2_calls: list[dict[str, object]] = []
        self.version_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.copy_call: dict[str, object] = {}
        self.create_calls: list[dict[str, object]] = []
        self.upload_part_copy_calls: list[dict[str, object]] = []
        self.upload_part_sizes: list[int] = []
        self.complete_calls: list[dict[str, object]] = []
        self.abort_calls: list[dict[str, object]] = []
        self.tagging_calls: list[dict[str, object]] = []
        self.get_call: dict[str, object] = {}
        self.put_call: dict[str, object] = {}
        self.head_call: dict[str, object] = {}
        self.source_body: bytes = b""
        self.head_error: ClientError | None = None
        self.tag_error: Exception | None = None
        self.fail_upload_part: bool = False

    def get_bucket_versioning(self, *, Bucket: str) -> Mapping[str, object]:  # noqa: N803
        return {"Status": "Suspended", "Bucket": Bucket}

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        self.list_v2_calls.append(kwargs)
        if "StartAfter" not in kwargs:
            return {
                "IsTruncated": True,
                "Contents": [object_item("a.txt")],
            }
        return {"IsTruncated": False, "Contents": [object_item("b.txt")]}

    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        self.version_calls.append(kwargs)
        if "KeyMarker" not in kwargs:
            return {
                "IsTruncated": True,
                "NextKeyMarker": "k",
                "NextVersionIdMarker": "v",
                "DeleteMarkers": [object_item("deleted.txt")],
                "Versions": [
                    object_item("old.txt", version_id="old", is_latest=False),
                    object_item("current.txt", version_id="v1", is_latest=True),
                ],
            }
        return {
            "IsTruncated": False,
            "Versions": [object_item("null.txt", version_id="null", is_latest=True)],
        }

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        self.head_call = kwargs
        if self.head_error is not None:
            raise self.head_error
        return {
            "ContentLength": 10,
            "ETag": '"etag"',
            "ContentType": "text/plain",
            "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
            "Metadata": {"source": "yes"},
        }

    def get_object_tagging(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        if self.tag_error is not None:
            raise self.tag_error
        return {"TagSet": [{"Key": "kind", "Value": "source"}]}

    def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        return self._set("copy_call", kwargs, {})

    def create_multipart_upload(self, **kwargs: object) -> Mapping[str, object]:
        return self._record(
            self.create_calls, kwargs, {"UploadId": f"upload-{len(self.create_calls) + 1}"}
        )

    def upload_part_copy(self, **kwargs: object) -> Mapping[str, object]:
        etag = f'"copy-{len(self.upload_part_copy_calls) + 1}"'
        return self._record(self.upload_part_copy_calls, kwargs, {"CopyPartResult": {"ETag": etag}})

    def upload_part(self, **kwargs: object) -> Mapping[str, object]:
        if self.fail_upload_part:
            raise RuntimeError("upload failed")
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.upload_part_sizes.append(len(body))
        return {"ETag": f'"part-{len(self.upload_part_sizes)}"'}

    def complete_multipart_upload(self, **kwargs: object) -> Mapping[str, object]:
        return self._record(self.complete_calls, kwargs, {})

    def abort_multipart_upload(self, **kwargs: object) -> Mapping[str, object]:
        return self._record(self.abort_calls, kwargs, {})

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        return self._set("get_call", kwargs, {"Body": BytesIO(self.source_body)})

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        return self._set("put_call", kwargs, {})

    def put_object_tagging(self, **kwargs: object) -> Mapping[str, object]:
        return self._record(self.tagging_calls, kwargs, {})

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        return self._record(self.delete_calls, kwargs, {})

    def _record(
        self,
        calls: list[dict[str, object]],
        kwargs: dict[str, object],
        response: Mapping[str, object],
    ) -> Mapping[str, object]:
        calls.append(kwargs)
        return response

    def _set(
        self, name: str, kwargs: dict[str, object], response: Mapping[str, object]
    ) -> Mapping[str, object]:
        setattr(self, name, kwargs)
        return response


class FakeClientError(ClientError):
    def __init__(self, code: str, status: int) -> None:
        Exception.__init__(self, code)
        self.response = {  # pyright: ignore[reportUnannotatedClassAttribute]
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": {},
                "HostId": "host",
                "RequestId": "request",
                "RetryAttempts": 0,
            },
        }


def object_item(
    key: str, *, version_id: str | None = None, is_latest: bool | None = None
) -> dict[str, object]:
    item: dict[str, object] = {
        "Key": key,
        "Size": 10,
        "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
        "ETag": '"etag"',
    }
    if version_id is not None:
        item["VersionId"] = version_id
    if is_latest is not None:
        item["IsLatest"] = is_latest
    return item


def properties(size: int = 10) -> S3ObjectProperties:
    return S3ObjectProperties(
        size,
        '"etag"',
        "text/plain",
        "gzip",
        "en",
        "inline",
        "max-age=60",
        datetime(2025, 1, 1, tzinfo=UTC),
        {"source": "yes"},
        {"kind": "source"},
        datetime(2024, 1, 1, tzinfo=UTC),
        {},
        None,
    )


def client_error(code: str, status: int = 404) -> ClientError:
    return FakeClientError(code, status)


def copy_object(
    bucket: S3ArchiveBucket,
    object_properties: S3ObjectProperties,
    strategy: TransferStrategy,
    source: S3ArchiveBucket | None = None,
) -> None:
    metadata = {"source": "yes", "fingerprint": "value"}
    bucket.copy_from(
        source or bucket,
        "source",
        "large.bin",
        "v1",
        object_properties,
        "large.bin",
        metadata,
        strategy,
    )
