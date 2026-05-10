"""Cleanup-focused archive runtime tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import override

import pytest
from s3_archiver_core.archive import ARCHIVE_SHA256_METADATA_KEY, group_metadata, run_archive
from s3_archiver_core.archive_manifest import SourcePathFilter, build_archive_manifest
from s3_archiver_core.archive_options import ArchiveOptions
from s3_archiver_core.s3 import S3ObjectProperties

from tests.unit.archive_workflow_fakes import FakeBucket
from tests.unit.archive_workflow_fakes import listed_object as _listed

STARTED = datetime(2024, 4, 20, tzinfo=UTC)


def _clock() -> datetime:
    return STARTED


def _target_key() -> str:
    return "data/fae/2024/02/20/2024-02-20T00-00-00.txt"


@pytest.mark.unit()
def test_cleanup_uses_verified_archive_group_without_late_destination_recheck() -> None:
    source_object = _listed(_target_key(), 90)
    source = FakeBucket("source", (source_object,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=60,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )
    archive_key = manifest.archive_groups[0].destination_archive_key
    payload = b"archive"
    metadata = dict(group_metadata(manifest.archive_groups[0])) | {
        ARCHIVE_SHA256_METADATA_KEY: hashlib.sha256(payload).hexdigest()
    }

    class CleanupMutationBucket(FakeBucket):
        head_calls: int

        def __init__(self) -> None:
            super().__init__(
                "destination",
                destination={archive_key: source_object.properties},
                payloads={archive_key: payload},
            )
            self.head_calls = 0

        @override
        def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
            self.head_calls += 1
            properties = super().head_object(key, version_id)
            if properties is not None and self.head_calls <= 2:
                return S3ObjectProperties(
                    size=properties.size,
                    etag=properties.etag,
                    content_type=properties.content_type,
                    content_encoding=properties.content_encoding,
                    content_language=properties.content_language,
                    content_disposition=properties.content_disposition,
                    cache_control=properties.cache_control,
                    expires=properties.expires,
                    metadata=metadata,
                    tags=properties.tags,
                    last_modified=properties.last_modified,
                    checksums=properties.checksums,
                    checksum_type=properties.checksum_type,
                )
            return properties

    destination = CleanupMutationBucket()

    result = run_archive(
        source,
        destination,
        ArchiveOptions(retention_days=60, cleanup_enabled=True, max_workers=1),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert result.copy.ok is True
    assert result.verify.ok is True
    assert result.cleanup.ok is True
    assert source.deleted == [(source_object.key, "v1")]
    assert destination.head_calls == 2


@pytest.mark.unit()
def test_existing_archive_with_missing_manifest_metadata_blocks_cleanup() -> None:
    listed = _listed(_target_key(), 90)
    source = FakeBucket("source", (listed,))
    manifest = build_archive_manifest(
        source,
        run_started_at_utc=STARTED,
        retention_days=60,
        versioning_state="Enabled",
        source_filter=SourcePathFilter(),
    )
    archive_key = manifest.archive_groups[0].destination_archive_key
    destination = FakeBucket("destination", destination={archive_key: listed.properties})

    result = run_archive(
        source,
        destination,
        ArchiveOptions(retention_days=60, cleanup_enabled=True, max_workers=1),
        run_started_at_utc=STARTED,
        clock=_clock,
    )

    assert result.copy.failures == ()
    assert result.skipped_archive_keys == (archive_key,)
    assert result.cleanup.skipped is False
    assert source.deleted == []
