"""Private archive protocol definitions.

The ``Protocol`` classes below are PEP 544 structural types: the ``...`` method
bodies are interface stubs, not abstract methods. Any object whose shape
matches satisfies the protocol at runtime, which is how production adapters
(e.g. ``S3ArchiveBucket``) and the in-memory test fakes both fit without
subclassing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from s3_archiver_core.archive_transfer import TransferStrategy
from s3_archiver_core.s3 import S3ListedObject, S3ObjectProperties, VersioningState


class ArchiveReadableBody(Protocol):
    """Readable source body returned by an archive bucket."""

    def read(self, amt: int = -1) -> bytes:
        """Read up to ``amt`` bytes."""
        ...

    def close(self) -> None:
        """Close the body."""
        ...


class ArchiveBucket(Protocol):
    """S3 bucket operations required by the archive engine."""

    @property
    def bucket(self) -> str:
        """Return the bucket name."""
        ...

    @property
    def temp_dir(self) -> Path:
        """Return the runtime temp directory for staged files."""
        ...

    def versioning_state(self) -> VersioningState:
        """Return bucket versioning state."""
        ...

    def list_source_objects(
        self, versioning_state: VersioningState, *, prefix: str = ""
    ) -> Iterable[S3ListedObject]:
        """List source objects."""
        ...

    def head_object(self, key: str, version_id: str | None = None) -> S3ObjectProperties | None:
        """Return object properties."""
        ...

    def delete_source_object(
        self, key: str, version_id: str | None = None, *, if_match: str | None = None
    ) -> None:
        """Delete one source object, optionally a specific version."""
        ...

    def content_sha256(self, key: str, version_id: str | None = None) -> str | None:
        """Return a SHA-256 digest for object content."""
        ...

    def read_source_bytes(self, key: str, version_id: str | None = None) -> bytes:
        """Return source object content."""
        ...

    def read_source_stream(self, key: str, version_id: str | None = None) -> ArchiveReadableBody:
        """Return a streaming source object body."""
        ...

    def upload_archive_file(
        self, destination_key: str, archive_path: Path, metadata: Mapping[str, str]
    ) -> None:
        """Upload an archive object."""
        ...

    def copy_from(
        self,
        source: ArchiveBucket,
        source_bucket: str,
        source_key: str,
        source_version_id: str | None,
        properties: S3ObjectProperties,
        destination_key: str,
        destination_metadata: Mapping[str, str],
        strategy: TransferStrategy,
    ) -> None:
        """Copy a source object."""
        ...


class ArchiveRunLock(Protocol):
    """Single-run lock boundary."""

    def acquire(self, *, run_id: str, run_started_at_utc: datetime, timeout: timedelta) -> bool:
        """Try to acquire the lock for this run."""
        ...

    def release(self, *, run_id: str) -> None:
        """Release the lock owned by this run."""
        ...
