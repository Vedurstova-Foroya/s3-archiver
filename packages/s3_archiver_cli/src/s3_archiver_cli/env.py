"""Runtime environment loading for the CLI."""

from __future__ import annotations

import os
from pathlib import Path

from s3_archiver_core.errors import ConfigError

DEFAULT_ENV_FILE = ".env"


def load_runtime_env() -> dict[str, str]:
    """Load the selected env file and overlay process environment variables."""

    env_file = selected_env_file()
    file_env = parse_env_file(env_file) if env_file.is_file() else {}
    runtime_env = dict(file_env)
    runtime_env.update(os.environ)
    return runtime_env


def selected_env_file() -> Path:
    """Return the env file selected by environment, or the default."""

    env_file = os.environ.get("APP_ENV_FILE") or os.environ.get("ENV_FILE") or DEFAULT_ENV_FILE
    return Path(env_file)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE env files, supporting quoted multi-line values."""

    loaded: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        index += 1
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        key, separator, raw_value = stripped.partition("=")
        if separator == "" or key.strip() == "":
            raise ConfigError(f"Invalid env assignment in {path}:{index}")
        value, consumed = _read_value(raw_value.strip(), lines, index)
        if consumed is None:
            raise ConfigError(
                f"Unterminated quoted value for {key.strip()} starting at {path}:{index}"
            )
        index = consumed
        loaded[key.strip()] = strip_optional_quotes(value)
    return loaded


def _read_value(first: str, lines: list[str], index: int) -> tuple[str, int | None]:
    quote = first[:1] if first[:1] in {"'", '"'} else None
    if quote is None or (len(first) >= 2 and first.endswith(quote)):
        return first, index
    collected = [first]
    while index < len(lines):
        next_line = lines[index]
        index += 1
        collected.append(next_line)
        if next_line.rstrip().endswith(quote):
            return "\n".join(collected), index
    return "", None


def strip_optional_quotes(value: str) -> str:
    """Remove matching single or double quotes around one env value."""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
