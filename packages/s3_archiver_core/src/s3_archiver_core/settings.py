from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from s3_archiver_core._removed_env import reject_removed_archiver_env
from s3_archiver_core._settings_models import (
    CopyMode,
    RouteSettings,
    S3AddressingStyle,
    S3LocationSettings,
    S3Provider,
    StorageLocationIdentity,
)
from s3_archiver_core._settings_parse import EnvDecoder
from s3_archiver_core.errors import ConfigError

_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})

__all__ = (
    "AppSettings",
    "CopyMode",
    "RouteSettings",
    "S3AddressingStyle",
    "S3LocationSettings",
    "S3Provider",
    "StorageLocationIdentity",
)


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Validated runtime settings for the CLI and archive workflow."""

    run_timeout: timedelta
    temp_dir: Path
    log_level: str
    log_dir: Path
    routes: tuple[RouteSettings, ...]
    cleanup_enabled: bool = False
    whitelist_enabled: bool = False
    bucket_whitelist: tuple[str, ...] = ()

    @property
    def archive_lock_path(self) -> Path:
        """Return the path to the archive run lock file."""

        return self.log_dir / "archive.lock"

    @property
    def cleanup_pending_dir(self) -> Path:
        """Return the directory holding cleanup-input manifests awaiting cleanup."""

        return self.log_dir / "cleanup" / "pending"

    @property
    def cleanup_cleaned_dir(self) -> Path:
        """Return the directory holding the temporary cleaned-object manifests."""

        return self.log_dir / "cleanup" / "cleaned"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AppSettings:
        """Parse and validate application settings from environment values."""

        reject_removed_archiver_env(env)
        decoder = EnvDecoder(env)
        log_level = env.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in _VALID_LOG_LEVELS:
            decoder.fail(
                "LOG_LEVEL", f"LOG_LEVEL must be one of {_VALID_LOG_LEVELS}, got {log_level!r}"
            )
        config_json = env.get("ARCHIVER_CONFIG_JSON")
        if config_json is not None and config_json.strip() != "":
            from s3_archiver_core._route_config import load_app_settings_from_config_json

            return load_app_settings_from_config_json(cls, decoder, config_json, log_level)
        raise ConfigError("ARCHIVER_CONFIG_JSON is required")
