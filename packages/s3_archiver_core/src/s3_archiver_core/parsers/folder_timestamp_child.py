"""Folder timestamp parser that groups by the first child folder after a timestamp."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from s3_archiver_core.parsers.kinds import ParserKind
from s3_archiver_core.parsers.protocol import ParserContext, ParserListedObject
from s3_archiver_core.parsers.results import SelectedObject, SkippedObject

_YEAR_RE = re.compile(r"20\d{2}")
_MONTH_RE = re.compile(r"0[1-9]|1[0-2]")
_DAY_RE = re.compile(r"0[1-9]|[12]\d|3[01]")
_HOUR_RE = re.compile(r"[01]\d|2[0-3]")


@dataclass(frozen=True, slots=True)
class _TimestampSpan:
    value: datetime
    start: int
    count: int


class FolderTimestampChildParser:
    """Select folder timestamps and group archives by one following folder segment."""

    @property
    def kind(self) -> ParserKind:
        return ParserKind("folder_timestamp_child")

    def parse(
        self, listed: ParserListedObject, context: ParserContext | None = None
    ) -> SelectedObject | SkippedObject:
        """Select the object when parent folders contain a timestamp followed by a child."""

        _ = context
        selected = _selected_span(_path_parts(listed.key))
        if selected is None:
            return SkippedObject("no reliable folder timestamp child")
        timestamp, archive_root = selected
        return SelectedObject(timestamp, "path", archive_root)


def _selected_span(parts: tuple[str, ...]) -> tuple[datetime, str] | None:
    spans = _timestamp_spans(parts)
    if not spans:
        return None
    span = max(spans, key=lambda item: item.value)
    root_end = span.start + span.count + 1
    if len(parts) < root_end:
        return None
    return span.value, "/".join(parts[:root_end])


def _timestamp_spans(parts: tuple[str, ...]) -> tuple[_TimestampSpan, ...]:
    spans: list[_TimestampSpan] = []
    for index in range(len(parts) - 2):
        span = _segmented_timestamp_span(parts, index)
        if span is not None:
            spans.append(span)
    return tuple(spans)


def _segmented_timestamp_span(parts: tuple[str, ...], index: int) -> _TimestampSpan | None:
    year, month, day = parts[index : index + 3]
    hour = parts[index + 3] if index + 3 < len(parts) else None
    if not (_matches(year, _YEAR_RE) and _matches(month, _MONTH_RE) and _matches(day, _DAY_RE)):
        return None
    hour_value, count = _hour_value_and_count(hour)
    try:
        value = datetime(int(year), int(month), int(day), hour_value, tzinfo=UTC)
    except ValueError:
        return None
    return _TimestampSpan(value, index, count)


def _hour_value_and_count(hour: str | None) -> tuple[int, int]:
    if hour is not None and _HOUR_RE.fullmatch(hour) is not None:
        return int(hour), 4
    return 0, 3


def _path_parts(key: str) -> tuple[str, ...]:
    return tuple(part for part in PurePosixPath(key).parent.parts if part not in {"", "."})


def _matches(value: str | None, pattern: re.Pattern[str]) -> bool:
    return value is not None and pattern.fullmatch(value) is not None


Parser = FolderTimestampChildParser
