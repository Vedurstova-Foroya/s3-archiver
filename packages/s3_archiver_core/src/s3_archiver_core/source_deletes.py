"""Source-object delete request and result models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceDeleteRequest:
    """One source object delete request."""

    key: str
    version_id: str | None = None
    if_match: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDeleteFailure:
    """One source object delete failure."""

    key: str
    version_id: str | None
    detail: str
