"""Template for custom object parsers.

Copy this file, rename the copy to a snake_case parser name such as
``customer_timestamp.py``, edit the sections marked ``CHANGE HERE``, and use
that filename without ``.py`` in route config, for example
``"parser": "customer_timestamp"``. The parser registry automatically loads
parser modules in this package that expose a ``Parser`` class. This template
file itself is intentionally skipped. No registry, ``ParserKind``,
``__init__.py``, or settings change is needed for the copied parser.
"""

from __future__ import annotations

from datetime import UTC, datetime

from s3_archiver_core.parsers.protocol import ParserContext, ParserListedObject
from s3_archiver_core.parsers.results import SelectedObject, SkippedObject


class Parser:
    """Select or skip one listed S3 object."""

    def parse(
        self, listed: ParserListedObject, context: ParserContext | None = None
    ) -> SelectedObject | SkippedObject:
        """Return a parser decision for one object.

        ``listed`` is the S3 object from the bucket listing. It includes the key,
        size, last-modified timestamp, version id, and listed properties.

        ``context`` carries the same listed object plus hydrated object
        properties when the archive workflow has fetched headers, metadata,
        tags, or checksums before parser execution.

        Return ``SelectedObject`` when this object should be archived. The
        timestamp controls the target data day, and ``archive_root`` controls
        grouping below the route source path.

        Return ``SkippedObject`` when this object should not be archived.
        """

        _ = context

        # CHANGE HERE: decide which keys your parser accepts.
        if not listed.key.endswith(".xml"):
            return SkippedObject("not an XML object")

        # CHANGE HERE: extract the reliable timestamp for this object.
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)

        # CHANGE HERE: set the grouping root used for archive paths.
        archive_root = _parent_prefix(listed.key)

        # CHANGE HERE: choose "basename", "path", or "last_modified".
        timestamp_source = "basename"

        return SelectedObject(timestamp, timestamp_source, archive_root)


def _parent_prefix(key: str) -> str:
    parent, separator, _name = key.rpartition("/")
    return parent if separator else ""
