"""Timestamp parsing and archive path derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Literal

TimestampSource = Literal["basename", "path"]

_DATE = r"(?P<year>20\d{2})[-_/]?(?P<month>0[1-9]|1[0-2])[-_/]?(?P<day>0[1-9]|[12]\d|3[01])"
_TIME = r"(?P<hour>[01]\d|2[0-3])(?P<tsep>[-:]?)(?P<minute>[0-5]\d)(?P=tsep)(?P<second>[0-5]\d)"
_DATE_RE = re.compile(_DATE)
_TIME_RE = re.compile(rf"{_TIME}(?P<zone>Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)?")


@dataclass(frozen=True, slots=True)
class _TimestampCandidate:
    value: datetime
    source: TimestampSource
    text: str


@dataclass(frozen=True, slots=True)
class _CandidateScan:
    candidates: tuple[_TimestampCandidate, ...]
    malformed: bool


class _MalformedTimestampError(ValueError):
    """Timestamp-shaped text was present but could not be parsed reliably."""


def select_key_timestamp(
    key: str, last_modified: datetime | None = None
) -> tuple[datetime, TimestampSource] | None:
    """Select the reliable UTC timestamp embedded in a source key."""

    path = PurePosixPath(key)
    basename_scan = _candidate_scan(path.name, "basename")
    basename_candidates = basename_scan.candidates
    path_candidates = _path_candidates(path)
    if basename_candidates:
        return _pick_candidate(basename_candidates, path_candidates, last_modified)
    if basename_scan.malformed:
        return None
    if path_candidates:
        candidate = _closest_to_last_modified(path_candidates, last_modified)
        return candidate.value, candidate.source
    return None


def archive_root_for_key(key: str) -> str:
    """Return the archive grouping root for a timestamped source key."""

    parts = list(PurePosixPath(key).parent.parts)
    while parts:
        stripped = _strip_one_timestamp_suffix(parts)
        if len(stripped) == len(parts):
            break
        parts = stripped
    return "/".join(part for part in parts if part not in {"", "."})


def destination_archive_key(archive_root: str, target_day: date) -> str:
    """Return the destination tar.gz key for an archive root and day."""

    filename = f"{target_day.isoformat()}.tar.gz"
    return f"{archive_root}/{filename}" if archive_root else filename


def _pick_candidate(
    basename_candidates: tuple[_TimestampCandidate, ...],
    path_candidates: tuple[_TimestampCandidate, ...],
    last_modified: datetime | None,
) -> tuple[datetime, TimestampSource]:
    path_values = {candidate.value for candidate in path_candidates}
    candidate = max(
        basename_candidates,
        key=lambda item: (
            1 if item.value in path_values else 0,
            -_last_modified_distance(item.value, last_modified),
            item.value,
        ),
    )
    return candidate.value, candidate.source


def _closest_to_last_modified(
    candidates: tuple[_TimestampCandidate, ...], last_modified: datetime | None
) -> _TimestampCandidate:
    return max(
        candidates,
        key=lambda candidate: (
            -_last_modified_distance(candidate.value, last_modified),
            candidate.value,
        ),
    )


def _last_modified_distance(value: datetime, last_modified: datetime | None) -> float:
    if last_modified is None:
        return 0.0
    return abs((value - _as_utc(last_modified)).total_seconds())


def _path_candidates(path: PurePosixPath) -> tuple[_TimestampCandidate, ...]:
    parent = "/".join(part for part in path.parent.parts if part not in {"", "."})
    return _dedupe((*_candidates(parent, "path"), *_segmented_path_candidates(path.parent.parts)))


def _candidates(text: str, source: TimestampSource) -> tuple[_TimestampCandidate, ...]:
    return _candidate_scan(text, source).candidates


def _candidate_scan(text: str, source: TimestampSource) -> _CandidateScan:
    candidates: list[_TimestampCandidate] = []
    malformed = False
    for match in _DATE_RE.finditer(text):
        try:
            candidate = _match_datetime(text, match, source)
        except ValueError:
            malformed = True
            continue
        candidates.append(candidate)
    return _CandidateScan(_dedupe(tuple(candidates)), malformed)


def _segmented_path_candidates(parts: tuple[str, ...]) -> tuple[_TimestampCandidate, ...]:
    candidates: list[_TimestampCandidate] = []
    for index in range(len(parts) - 2):
        year, month, day = parts[index : index + 3]
        hour = parts[index + 3] if index + 3 < len(parts) else None
        if _is_year(year) and _is_month(month) and _is_day(day):
            hour_value = int(hour) if hour is not None and _is_hour(hour) else 0
            try:
                dt = datetime(int(year), int(month), int(day), hour_value, tzinfo=UTC)
            except ValueError:
                continue
            candidates.append(_TimestampCandidate(dt, "path", "/".join(parts[index : index + 4])))
    return _dedupe(tuple(candidates))


def _match_datetime(
    text: str, date_match: re.Match[str], source: TimestampSource
) -> _TimestampCandidate:
    tail = text[date_match.end() :]
    time_start = _time_start(tail)
    if time_start is None:
        dt = datetime(
            int(date_match["year"]),
            int(date_match["month"]),
            int(date_match["day"]),
            tzinfo=UTC,
        )
        return _TimestampCandidate(dt, source, date_match.group(0))

    time_match = _TIME_RE.match(tail, time_start)
    if time_match is None:
        raise _MalformedTimestampError
    if _has_unconsumed_timezone_suffix(tail[time_match.end() :]):
        raise _MalformedTimestampError
    dt = datetime(
        int(date_match["year"]),
        int(date_match["month"]),
        int(date_match["day"]),
        int(time_match["hour"]),
        int(time_match["minute"]),
        int(time_match["second"]),
        tzinfo=_timezone(time_match["zone"]),
    ).astimezone(UTC)
    consumed = date_match.end() + time_match.end()
    return _TimestampCandidate(dt, source, text[date_match.start() : consumed])


def _has_unconsumed_timezone_suffix(value: str) -> bool:
    if not value:
        return False
    if value[0] in {"+", "Z"}:
        return True
    return value[0] == "-" and len(value) > 1 and (value[1].isdigit() or value[1] == ".")


def _time_start(tail: str) -> int | None:
    if not tail:
        return None
    if tail[0] in {"T", "_", " "}:
        return 1
    if tail[0] == "-" and len(tail) > 1 and tail[1].isdigit():
        return 1
    if tail[0].isdigit():
        return 0
    return None


def _timezone(value: str | None) -> timezone:
    if value is None or value == "Z":
        return UTC
    sign = 1 if value[0] == "+" else -1
    offset = value[1:].replace(":", "")
    hours = int(offset[:2])
    minutes = int(offset[2:])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _strip_one_timestamp_suffix(parts: list[str]) -> list[str]:
    if (
        len(parts) >= 4
        and _is_year(parts[-4])
        and _is_month(parts[-3])
        and _is_day(parts[-2])
        and _is_hour(parts[-1])
    ):
        return parts[:-4]
    if len(parts) >= 3 and _is_year(parts[-3]) and _is_month(parts[-2]) and _is_day(parts[-1]):
        return parts[:-3]
    if _candidates(parts[-1], "path"):
        return parts[:-1]
    return parts


def _dedupe(candidates: tuple[_TimestampCandidate, ...]) -> tuple[_TimestampCandidate, ...]:
    seen: set[tuple[datetime, TimestampSource, str]] = set()
    deduped: list[_TimestampCandidate] = []
    for candidate in candidates:
        key = (candidate.value, candidate.source, candidate.text)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return tuple(deduped)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_year(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"20\d{2}", value) is not None


def _is_month(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"0[1-9]|1[0-2]", value) is not None


def _is_day(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"0[1-9]|[12]\d|3[01]", value) is not None


def _is_hour(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"[01]\d|2[0-3]", value) is not None
