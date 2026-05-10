"""Archive run result models."""

from __future__ import annotations

from dataclasses import dataclass, field

from s3_archiver_core.archive_manifest import ArchiveManifest


@dataclass(frozen=True, slots=True)
class ArchivePhaseResult:
    """Outcome for one archive phase."""

    phase: str
    failures: tuple[str, ...] = ()
    skipped: bool = False

    @property
    def ok(self) -> bool:
        """Return whether the phase completed without failures."""
        return self.failures == ()


@dataclass(frozen=True, slots=True)
class ArchiveRunResult:
    """Outcome for a complete archive run."""

    run_id: str
    manifest: ArchiveManifest
    copy: ArchivePhaseResult
    verify: ArchivePhaseResult
    cleanup: ArchivePhaseResult
    list: ArchivePhaseResult = field(default_factory=lambda: ArchivePhaseResult("list"))
    verified_archive_keys: tuple[str, ...] = ()
    skipped_archive_keys: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Return whether every archive phase completed without failures."""
        return self.list.ok and self.copy.ok and self.verify.ok and self.cleanup.ok
