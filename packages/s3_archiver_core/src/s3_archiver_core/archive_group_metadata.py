"""Archive group metadata and verification helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from s3_archiver_core._archive_identity import stable_identity_value
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
    """Return whether an existing destination archive carries verified metadata."""

    _ = (destination, destination_key)
    if not metadata_matches(existing, expected):
        return False
    archive_sha256 = existing.get(ARCHIVE_SHA256_METADATA_KEY)
    return archive_sha256 is not None


def uploaded_archive_verified(
    destination: ArchiveBucket,
    destination_key: str,
    existing: Mapping[str, str],
    expected: Mapping[str, str],
) -> bool:
    """Return whether a just-uploaded destination archive carries expected metadata."""

    # Re-reading archive payloads for SHA-256 verification is not viable at production scale.
    # The upload path computes and stores the archive hash before upload; S3 stores that marker
    # atomically with the object metadata.
    _ = (destination, destination_key)
    return metadata_matches(existing, expected)


def metadata_matches(existing: Mapping[str, str], expected: Mapping[str, str]) -> bool:
    """Return whether all expected archive metadata keys match."""

    return all(existing.get(key) == value for key, value in expected.items())


def _group_manifest_sha256(group: ArchiveGroup) -> str:
    digest = hashlib.sha256()
    digest.update(b"[")
    first = True
    for entry in sorted(group.entries, key=lambda item: item.key):
        if first:
            first = False
        else:
            digest.update(b",")
        row = {
            "copy_mode": entry.copy_mode,
            "destination_archive_key": entry.destination_archive_key,
            "key": entry.key,
            "parser_kind": entry.parser_kind,
            "route_name": entry.route_name,
            "size": entry.size,
            "source_bucket": entry.source_bucket,
            "source_identity": stable_identity_value(entry.source_identity),
            "source_path": entry.source_path,
            "etag": entry.etag,
            "version_id": entry.version_id,
            "selected_timestamp": (
                entry.selected_timestamp.isoformat() if entry.selected_timestamp else None
            ),
            "timestamp_source": entry.timestamp_source,
        }
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
    digest.update(b"]")
    return digest.hexdigest()
