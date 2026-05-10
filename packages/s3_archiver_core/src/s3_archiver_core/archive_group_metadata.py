"""Archive group metadata and verification helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from s3_archiver_core._archive_protocols import ArchiveBucket
from s3_archiver_core.archive_manifest import ArchiveGroup

ARCHIVE_SHA256_METADATA_KEY = "s3-archiver-archive-sha256"
MANIFEST_SHA256_METADATA_KEY = "s3-archiver-manifest-sha256"
TARGET_DAY_METADATA_KEY = "s3-archiver-target-day"
SOURCE_COUNT_METADATA_KEY = "s3-archiver-source-count"
SCHEMA_VERSION_METADATA_KEY = "s3-archiver-schema-version"
ARCHIVE_SCHEMA_VERSION = "2"


def group_metadata(group: ArchiveGroup) -> Mapping[str, str]:
    """Return deterministic manifest metadata for one archive group."""

    return {
        MANIFEST_SHA256_METADATA_KEY: _group_manifest_sha256(group),
        TARGET_DAY_METADATA_KEY: group.target_day.isoformat(),
        SOURCE_COUNT_METADATA_KEY: str(len(group.entries)),
        SCHEMA_VERSION_METADATA_KEY: ARCHIVE_SCHEMA_VERSION,
    }


def existing_archive_verified(
    destination: ArchiveBucket,
    destination_key: str,
    existing: Mapping[str, str],
    expected: Mapping[str, str],
) -> bool:
    """Return whether an existing destination archive is verified for cleanup."""

    if not metadata_matches(existing, expected):
        return False
    archive_sha256 = existing.get(ARCHIVE_SHA256_METADATA_KEY)
    return (
        archive_sha256 is not None and destination.content_sha256(destination_key) == archive_sha256
    )


def uploaded_archive_verified(
    destination: ArchiveBucket,
    destination_key: str,
    existing: Mapping[str, str],
    expected: Mapping[str, str],
) -> bool:
    """Return whether a just-uploaded destination archive is verified."""

    return metadata_matches(existing, expected) and (
        destination.content_sha256(destination_key) == expected[ARCHIVE_SHA256_METADATA_KEY]
    )


def metadata_matches(existing: Mapping[str, str], expected: Mapping[str, str]) -> bool:
    """Return whether all expected archive metadata keys match."""

    return all(existing.get(key) == value for key, value in expected.items())


def _group_manifest_sha256(group: ArchiveGroup) -> str:
    rows = [
        {
            "key": entry.key,
            "size": entry.size,
            "etag": entry.etag,
            "version_id": entry.version_id,
            "selected_timestamp": (
                entry.selected_timestamp.isoformat() if entry.selected_timestamp else None
            ),
            "timestamp_source": entry.timestamp_source,
        }
        for entry in sorted(group.entries, key=lambda item: item.key)
    ]
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
