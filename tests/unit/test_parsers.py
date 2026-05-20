"""Tests for object parsers."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
import s3_archiver_core.parsers.registry as parser_registry
from s3_archiver_core.parsers import SelectedObject, SkippedObject
from s3_archiver_core.parsers.direct import DirectParser
from s3_archiver_core.parsers.filename_timestamp import FilenameTimestampParser
from s3_archiver_core.parsers.folder_timestamp import FolderTimestampParser
from s3_archiver_core.parsers.kinds import ParserKind
from s3_archiver_core.parsers.protocol import ParserContext
from s3_archiver_core.parsers.registry import (
    ParserFactory,
    clear_parser_registry_cache,
    discover_parser_factories,
    parser_for_kind,
    registered_parser_kinds,
)
from s3_archiver_core.parsers.template import Parser as TemplateParser
from s3_archiver_core.s3 import S3ListedObject, S3ObjectProperties


def _listed(key: str, last_modified: datetime | None = None) -> S3ListedObject:
    timestamp = last_modified or datetime(2026, 4, 20, 12, tzinfo=UTC)
    properties = S3ObjectProperties(
        size=10,
        etag='"etag"',
        content_type="text/plain",
        content_encoding=None,
        content_language=None,
        content_disposition=None,
        cache_control=None,
        expires=None,
        metadata={"owner": "archive"},
        tags={"kind": "source"},
        last_modified=timestamp,
    )
    return S3ListedObject(
        key=key,
        size=10,
        last_modified=timestamp,
        etag='"etag"',
        version_id="v1",
        properties=properties,
    )


@pytest.mark.unit()
def test_registry_contains_builtin_parser_kinds() -> None:
    kinds = registered_parser_kinds()

    assert {
        ParserKind("direct"),
        ParserKind("filename_timestamp"),
        ParserKind("folder_timestamp"),
    } <= kinds
    assert isinstance(parser_for_kind("direct"), DirectParser)
    assert "template" not in {kind.value for kind in kinds}
    assert DirectParser().kind == ParserKind.DIRECT
    assert FilenameTimestampParser().kind == ParserKind.FILENAME_TIMESTAMP
    assert FolderTimestampParser().kind == ParserKind.FOLDER_TIMESTAMP


@pytest.mark.unit()
def test_registry_loads_copied_template_module_by_filename(tmp_path: Path) -> None:
    package = tmp_path / "copied_parsers"
    package.mkdir()
    _ = (package / "__init__.py").write_text("", encoding="utf-8")
    _ = (package / "customer_timestamp.py").write_text(
        "\n".join(
            (
                "from s3_archiver_core.parsers.results import SelectedObject",
                "",
                "class Parser:",
                "    def parse(self, listed, context=None):",
                '        return SelectedObject(listed.last_modified, "last_modified", "")',
                "",
            )
        ),
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))

    try:
        registry = discover_parser_factories(
            ("copied_parsers.customer_timestamp",),
            importlib.import_module,
        )
    finally:
        sys.path.remove(str(tmp_path))
        _ = sys.modules.pop("copied_parsers.customer_timestamp", None)
        _ = sys.modules.pop("copied_parsers", None)

    assert registry.keys() == {ParserKind("customer_timestamp")}


@pytest.mark.unit()
def test_parser_for_kind_rejects_unsupported_parser() -> None:
    with pytest.raises(ValueError, match="unsupported parser kind"):
        _ = parser_for_kind("unsupported")


@pytest.mark.unit()
def test_registry_auto_discovers_parser_class_by_module_filename() -> None:
    custom = ModuleType("s3_archiver_core.parsers.customer_timestamp")
    custom.__dict__["Parser"] = DirectParser
    no_parser = ModuleType("s3_archiver_core.parsers.no_parser")
    not_callable = ModuleType("s3_archiver_core.parsers.not_callable")
    not_callable.__dict__["Parser"] = "not-callable"
    modules = {
        custom.__name__: custom,
        no_parser.__name__: no_parser,
        not_callable.__name__: not_callable,
    }

    registry = discover_parser_factories(
        (
            "s3_archiver_core.parsers.customer_timestamp",
            "s3_archiver_core.parsers.no_parser",
            "s3_archiver_core.parsers.not_callable",
            "s3_archiver_core.parsers.template",
        ),
        modules.__getitem__,
    )

    assert registry == {ParserKind("customer_timestamp"): DirectParser}


@pytest.mark.unit()
def test_parser_for_kind_reuses_discovered_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], Callable[[str], ModuleType]]] = []

    def discover(
        module_names: Iterable[str],
        import_module: Callable[[str], ModuleType],
    ) -> Mapping[ParserKind, ParserFactory]:
        calls.append((tuple(module_names), import_module))
        return {ParserKind("direct"): DirectParser}

    clear_parser_registry_cache()
    monkeypatch.setattr(parser_registry, "discover_parser_factories", discover)

    try:
        assert isinstance(parser_for_kind("direct"), DirectParser)
        assert isinstance(parser_for_kind("direct"), DirectParser)
        assert registered_parser_kinds() == {ParserKind("direct")}
        assert len(calls) == 1
    finally:
        clear_parser_registry_cache()


@pytest.mark.unit()
def test_template_parser_documents_select_and_skip_results() -> None:
    parser = TemplateParser()

    assert parser.parse(_listed("data/file.txt")) == SkippedObject("not an XML object")
    assert parser.parse(_listed("file.xml")) == SelectedObject(
        datetime(2026, 1, 1, tzinfo=UTC),
        "basename",
        "",
    )
    result = parser.parse(_listed("data/file.xml"))
    assert isinstance(result, SelectedObject)
    assert result.archive_root == "data"


@pytest.mark.unit()
def test_direct_parser_selects_s3_last_modified_timestamp() -> None:
    listed = _listed("data/fae/file.txt", datetime(2026, 4, 13, 8))

    selected = DirectParser().parse(listed)

    assert selected == SelectedObject(
        datetime(2026, 4, 13, 8, tzinfo=UTC),
        "last_modified",
        "data/fae",
    )


@pytest.mark.unit()
def test_direct_parser_validates_parser_context_properties() -> None:
    listed = _listed("data/fae/file.txt", datetime(2026, 4, 13, 8, tzinfo=UTC))
    other = _listed("data/fae/other.txt", datetime(2026, 4, 13, 8, tzinfo=UTC))
    larger = S3ObjectProperties(
        size=11,
        etag=listed.properties.etag,
        content_type=listed.properties.content_type,
        content_encoding=listed.properties.content_encoding,
        content_language=listed.properties.content_language,
        content_disposition=listed.properties.content_disposition,
        cache_control=listed.properties.cache_control,
        expires=listed.properties.expires,
        metadata=listed.properties.metadata,
        tags=listed.properties.tags,
        last_modified=listed.properties.last_modified,
    )

    assert DirectParser().parse(listed, ParserContext(listed)).timestamp == listed.last_modified
    assert (
        DirectParser().parse(listed, ParserContext(listed, listed.properties)).timestamp
        == listed.last_modified
    )
    with pytest.raises(ValueError, match="context does not match"):
        _ = DirectParser().parse(listed, ParserContext(other, other.properties))
    with pytest.raises(ValueError, match="size differs"):
        _ = DirectParser().parse(listed, ParserContext(listed, larger))


@pytest.mark.unit()
def test_filename_parser_selects_key_timestamp_without_s3_fallback() -> None:
    parser = FilenameTimestampParser()

    selected = parser.parse(
        _listed("data/fae/2026-04-13T07-00-00Z.xml", datetime(1999, 1, 1, tzinfo=UTC))
    )
    skipped = parser.parse(_listed("data/fae/no-stamp.xml"))

    assert selected == SelectedObject(
        datetime(2026, 4, 13, 7, tzinfo=UTC),
        "basename",
        "data/fae",
    )
    assert skipped == SkippedObject("no reliable key timestamp")


@pytest.mark.unit()
def test_folder_parser_selects_folder_timestamp_without_basename_fallback() -> None:
    parser = FolderTimestampParser()

    folder_selected = parser.parse(_listed("data/fae/2026/04/13/07/no-stamp.xml"))
    basename_only = parser.parse(_listed("data/fae/2026-04-13T07-00-00Z.xml"))

    assert folder_selected == SelectedObject(
        datetime(2026, 4, 13, 7, tzinfo=UTC),
        "path",
        "data/fae",
    )
    assert basename_only == SkippedObject("no reliable folder timestamp")
