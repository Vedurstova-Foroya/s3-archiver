"""Data classes for cleanup-input manifests and cleanup run results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CleanupManifestStatus(StrEnum):
    """Integrity status of a cleanup-input manifest file."""

    VALID = "valid"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class CleanupRecord:
    """One archived source object marked for source-side deletion."""

    route_name: str
    source_identity: object
    source_bucket: str
    key: str
    version_id: str | None
    size: int
    etag: str | None
    destination_bucket: str
    destination_key: str
    destination_archive_key: str
    copy_mode: str


@dataclass(frozen=True, slots=True)
class CleanupManifestSummary:
    """Validated header/footer summary describing a cleanup manifest file."""

    status: CleanupManifestStatus
    object_count: int
    run_id: str
    run_started_at_utc: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CleanupManifestOutcome:
    """Cleanup outcome for one input manifest file."""

    path: Path
    object_count: int
    cleaned_count: int
    failures: tuple[str, ...]
    removed: bool

    @property
    def ok(self) -> bool:
        """Return whether every object was cleaned and the manifest was retired."""

        return self.removed and self.failures == ()


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Aggregate outcome of one cleanup run over zero or more manifests."""

    outcomes: tuple[CleanupManifestOutcome, ...]
    empty: bool

    @property
    def ok(self) -> bool:
        """Return whether every processed manifest was fully cleaned and retired."""

        return all(outcome.ok for outcome in self.outcomes)

    @property
    def object_count(self) -> int:
        """Return the total number of objects referenced across all manifests."""

        return sum(outcome.object_count for outcome in self.outcomes)

    @property
    def cleaned_count(self) -> int:
        """Return the total number of objects verified deleted across all manifests."""

        return sum(outcome.cleaned_count for outcome in self.outcomes)

    @property
    def failures(self) -> tuple[str, ...]:
        """Return every per-object failure across all processed manifests."""

        return tuple(failure for outcome in self.outcomes for failure in outcome.failures)
