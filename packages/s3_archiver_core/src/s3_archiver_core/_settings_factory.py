"""Structural type for settings-builder callables.

``AppSettingsFactory`` is a PEP 544 ``Protocol``: the ``...`` body is an
interface stub, not an abstract method — any callable with the matching
signature satisfies it at runtime.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Protocol

from s3_archiver_core._settings_models import (
    RouteSettings,
)


class AppSettingsFactory[T](Protocol):
    """Callable constructor shape shared by settings decoders."""

    def __call__(
        self,
        *,
        run_timeout: timedelta,
        temp_dir: Path,
        log_level: str,
        log_dir: Path,
        routes: tuple[RouteSettings, ...],
        cleanup_enabled: bool,
    ) -> T:
        """Build settings from decoded fields."""
        ...
