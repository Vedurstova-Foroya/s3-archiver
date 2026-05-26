"""Private parsing helpers for settings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from s3_archiver_core.archive_lock import parse_duration
from s3_archiver_core.errors import ConfigError

LOCALSTACK_ENDPOINT_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "localstack", "localstack-alt", "localhost.localstack.cloud"}
)


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """One env decoding failure captured before the settings boundary raises."""

    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ParseResult[T]:
    """Pure result wrapper used by the env decoding boundary."""

    value: T | None
    issue: ParseIssue | None = None

    @property
    def ok(self) -> bool:
        """Return whether parsing succeeded."""

        return self.issue is None


class EnvDecoder:
    """Collect the first env parse issue and raise at the settings boundary."""

    env: Mapping[str, str]

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env
        self._issue: ParseIssue | None = None

    def consume[T](self, result: ParseResult[T]) -> T | None:
        if result.issue is not None and self._issue is None:
            self._issue = result.issue
        return result.value

    def fail(self, field: str, message: str) -> None:
        if self._issue is None:
            self._issue = ParseIssue(field, message)

    def finish(self) -> None:
        if self._issue is not None:
            raise ConfigError(self._issue.message)


def parse_bool_result(env: Mapping[str, str], key: str, *, default: bool) -> ParseResult[bool]:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return ParseResult(default)
    value = raw.strip().lower()
    if value == "true":
        return ParseResult(True)
    if value == "false":
        return ParseResult(False)
    return ParseResult(None, ParseIssue(key, f"{key} must be true or false"))


def parse_int_result(
    env: Mapping[str, str], key: str, *, default: int, minimum: int
) -> ParseResult[int]:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return ParseResult(default)
    try:
        value = int(raw)
    except ValueError:
        return ParseResult(None, ParseIssue(key, f"{key} must be an integer"))
    if value < minimum:
        return ParseResult(
            None,
            ParseIssue(key, f"{key} must be greater than or equal to {minimum}"),
        )
    return ParseResult(value)


def parse_runtime_duration_result(raw: str, key: str) -> ParseResult[timedelta]:
    try:
        return ParseResult(parse_duration(raw))
    except ValueError:
        return ParseResult(None, ParseIssue(key, f"{key} must be a positive duration such as 7d"))


def parse_string_array_result(env: Mapping[str, str], key: str) -> ParseResult[tuple[str, ...]]:
    raw = env.get(key, "[]")
    if raw.strip() == "":
        raw = "[]"
    try:
        parsed = cast(object, json.loads(raw))
    except json.JSONDecodeError:
        return ParseResult(None, ParseIssue(key, f"{key} must be a JSON array of strings"))
    if not isinstance(parsed, list):
        return ParseResult(None, ParseIssue(key, f"{key} must be a JSON array of strings"))
    items: list[str] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, str):
            return ParseResult(None, ParseIssue(key, f"{key} must be a JSON array of strings"))
        items.append(item)
    return ParseResult(tuple(items))


def normalize_endpoint_url(raw: str, *, field: str = "S3_ENDPOINT") -> str:
    result = normalize_endpoint_url_result(raw, field=field)
    if result.issue is not None:
        raise ConfigError(result.issue.message)
    return cast(str, result.value)


def normalize_endpoint_url_result(raw: str, *, field: str = "S3_ENDPOINT") -> ParseResult[str]:
    parsed = urlsplit(raw)
    if parsed.scheme == "" or parsed.hostname is None:
        return ParseResult(
            None,
            ParseIssue(field, f"{field} must include a URL scheme and host, got {raw!r}"),
        )
    if parsed.query != "" or parsed.fragment != "":
        return ParseResult(
            None,
            ParseIssue(field, f"{field} must not include query or fragment components"),
        )
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return ParseResult(
            None,
            ParseIssue(field, f"{field} scheme must be http or https, got {scheme!r}"),
        )
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return ParseResult(None, ParseIssue(field, f"{field} has an invalid port"))
    default_port = 80 if scheme == "http" else 443
    netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return ParseResult(urlunsplit((scheme, netloc, path, "", "")))


def require_env_result(env: Mapping[str, str], key: str) -> ParseResult[str]:
    value = env.get(key)
    if value is None or value.strip() == "":
        return ParseResult(None, ParseIssue(key, f"{key} is required"))
    return ParseResult(value.strip())


def optional_env_result(env: Mapping[str, str], key: str) -> ParseResult[str | None]:
    value = env.get(key)
    if value is None or value.strip() == "":
        return ParseResult(None)
    return ParseResult(value.strip())
