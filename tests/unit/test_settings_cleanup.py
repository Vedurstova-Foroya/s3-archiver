"""Tests for the CLEANUP setting and cleanup manifest directories."""

from __future__ import annotations

import pytest
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import AppSettings


@pytest.mark.unit()
def test_cleanup_enabled_defaults_false(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)

    assert settings.cleanup_enabled is False


@pytest.mark.unit()
def test_cleanup_enabled_parses_true(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env({**base_env, "CLEANUP": "true"})

    assert settings.cleanup_enabled is True


@pytest.mark.unit()
def test_cleanup_enabled_rejects_invalid_value(base_env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="CLEANUP must be true or false"):
        _ = AppSettings.from_env({**base_env, "CLEANUP": "maybe"})


@pytest.mark.unit()
def test_cleanup_directories_live_under_log_dir(base_env: dict[str, str]) -> None:
    settings = AppSettings.from_env(base_env)

    assert settings.cleanup_pending_dir == settings.log_dir / "cleanup" / "pending"
    assert settings.cleanup_cleaned_dir == settings.log_dir / "cleanup" / "cleaned"
