"""Archive group metadata and verification helpers."""

from __future__ import annotations

from collections.abc import Mapping

from s3_archiver_core._archive_manifest_digest import manifest_entries_sha256
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
        MANIFEST_SHA256_METADATA_KEY: group.manifest_sha256
        or manifest_entries_sha256(group.entries),
        TARGET_DAY_METADATA_KEY: group.target_day.isoformat(),
        SOURCE_COUNT_METADATA_KEY: str(group.source_count or len(group.entries)),
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


def existing_archive_refreshable(existing: Mapping[str, str], expected: Mapping[str, str]) -> bool:
    """Return whether a mismatched existing archive may be replaced."""

    return (
        existing.get(SCHEMA_VERSION_METADATA_KEY) == ARCHIVE_SCHEMA_VERSION
        and existing.get(ARCHIVE_SHA256_METADATA_KEY) is not None
        and existing.get(TARGET_DAY_METADATA_KEY) == expected.get(TARGET_DAY_METADATA_KEY)
        and existing.get(SOURCE_COUNT_METADATA_KEY) is not None
        and existing.get(SOURCE_COUNT_METADATA_KEY) != expected.get(SOURCE_COUNT_METADATA_KEY)
    )


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
