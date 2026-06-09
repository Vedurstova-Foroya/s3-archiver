"""Tests for cleanup manifest corruption detection (mangled manifests)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from s3_archiver_core.cleanup_manifest import (
    CleanupRecord,
    iter_cleanup_records,
    validate_cleanup_manifest,
    write_cleanup_manifest,
)
from s3_archiver_core.errors import CleanupManifestError

RUN_STARTED = "2026-04-20T00:00:00+00:00"


def _record(key: str = "data/a.xml", version_id: str | None = "v1") -> CleanupRecord:
    return CleanupRecord(
        route_name="default",
        source_identity="src-identity",
        source_bucket="source",
        key=key,
        version_id=version_id,
        size=10,
        etag='"etag"',
        destination_bucket="destination",
        destination_key="",
        destination_archive_key="data/2026-04-13.tar.gz",
        copy_mode="daily_tar_gz",
    )


def _write_valid(path: Path, records: list[CleanupRecord]) -> None:
    _ = write_cleanup_manifest(
        path, run_id="run-1", run_started_at_utc=RUN_STARTED, records=records
    )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").rstrip("\n").split("\n")


def _rewrite(path: Path, lines: list[str]) -> None:
    _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mutated_record_line(line: str, **changes: object) -> str:
    decoded = cast(dict[str, object], json.loads(line))
    decoded.update(changes)
    return json.dumps(decoded, sort_keys=True, separators=(",", ":"))


def _mutated_footer(line: str, **changes: object) -> str:
    footer = cast(dict[str, object], json.loads(line))
    footer.update(changes)
    return json.dumps(footer, sort_keys=True, separators=(",", ":"))


@pytest.mark.unit()
def test_invalid_json_line_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    lines[0] = "{not json"

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="invalid JSON"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_non_object_line_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    lines[0] = "[1, 2, 3]"

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="not a JSON object"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_missing_summary_line_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)

    _rewrite(path, lines[:-1])

    with pytest.raises(CleanupManifestError, match="missing the manifest summary"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_content_after_summary_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)

    _rewrite(path, [*lines, "trailing"])

    with pytest.raises(CleanupManifestError, match="content after the manifest summary"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_object_count_mismatch_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    lines[-1] = _mutated_footer(lines[-1], object_count=5)

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="does not match"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_digest_mismatch_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record("data/a.xml")])
    lines = _lines(path)
    lines[0] = _mutated_record_line(lines[0], key="data/tampered.xml")

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="digest mismatch"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_unsupported_schema_version_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    lines[-1] = _mutated_footer(lines[-1], schema_version=99)

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="schema version"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_footer_field_wrong_type_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    lines[-1] = _mutated_footer(lines[-1], object_count="one")

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="must be an integer"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_record_missing_source_identity_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    decoded = cast(dict[str, object], json.loads(lines[0]))
    del decoded["source_identity"]
    lines[0] = json.dumps(decoded, sort_keys=True, separators=(",", ":"))

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="source_identity"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_record_string_field_wrong_type_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    lines[0] = _mutated_record_line(lines[0], route_name=12)

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="must be a string"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_record_optional_field_wrong_type_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    lines[0] = _mutated_record_line(lines[0], version_id=7)

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="string or null"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_record_missing_optional_key_is_mangled(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)
    decoded = cast(dict[str, object], json.loads(lines[0]))
    del decoded["etag"]
    lines[0] = json.dumps(decoded, sort_keys=True, separators=(",", ":"))

    _rewrite(path, lines)

    with pytest.raises(CleanupManifestError, match="missing 'etag'"):
        _ = validate_cleanup_manifest(path)


@pytest.mark.unit()
def test_iter_cleanup_records_rejects_content_after_summary(tmp_path: Path) -> None:
    path = tmp_path / "run-1.jsonl"
    _write_valid(path, [_record()])
    lines = _lines(path)

    _rewrite(path, [*lines, "trailing"])

    with pytest.raises(CleanupManifestError, match="content after the manifest summary"):
        _ = list(iter_cleanup_records(path))
