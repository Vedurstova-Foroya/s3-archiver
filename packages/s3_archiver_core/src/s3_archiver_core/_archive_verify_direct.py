"""Direct-copy destination verification."""

from __future__ import annotations

from s3_archiver_core._archive_env import bool_env
from s3_archiver_core._archive_manifest_models import ManifestEntry
from s3_archiver_core.archive_routes import ArchiveRoute
from s3_archiver_core.archive_transfer import (
    VerificationResult,
    verify_destination,
    verify_destination_checksum,
    verify_destination_content,
)
from s3_archiver_core.s3 import S3ObjectProperties

_DIRECT_CONTENT_VERIFY_ENV = "ARCHIVER_DIRECT_CONTENT_VERIFY"


def verify_direct_entry(
    route: ArchiveRoute,
    entry: ManifestEntry,
    destination: S3ObjectProperties | None,
) -> VerificationResult:
    """Verify one direct-copy destination object."""

    _ = route
    verified = verify_destination(entry, destination)
    if not verified.ok:
        return verified
    assert destination is not None
    checksum_verified = verify_destination_checksum(entry.object.properties, destination)
    if checksum_verified is not None:
        return checksum_verified
    if not bool_env(_DIRECT_CONTENT_VERIFY_ENV):
        return VerificationResult(True, "ok")
    return verify_destination_content(
        route.source.content_sha256(entry.key, entry.version_id),
        route.destination.content_sha256(entry.destination_key),
    )
