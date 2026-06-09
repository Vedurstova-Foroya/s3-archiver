"""Cleanup-input manifest serialization and integrity validation.

A cleanup manifest is a newline-delimited JSON file. Each record line names one
archived source object that is safe to delete; a trailing summary line carries a
``sha256`` digest over the record lines plus their count. Any structural problem
(unreadable JSON, wrong field types, a missing/duplicated summary, a record
count or digest that does not match) is treated as a *mangled* manifest and
raises :class:`CleanupManifestError`, so cleanup never deletes from a manifest in
a bad state. A missing or zero-record manifest is *empty*, not mangled.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import cast

from s3_archiver_core._archive_identity import stable_identity_value
from s3_archiver_core._archive_manifest_models import ManifestEntry
from s3_archiver_core._cleanup_models import (
    CleanupManifestStatus,
    CleanupManifestSummary,
    CleanupRecord,
)
from s3_archiver_core.errors import CleanupManifestError

__all__ = (
    "CleanupManifestStatus",
    "CleanupManifestSummary",
    "CleanupRecord",
    "cleanup_record_from_entry",
    "iter_cleanup_records",
    "validate_cleanup_manifest",
    "write_cleanup_manifest",
)

CLEANUP_MANIFEST_KIND = "s3-archiver-cleanup-manifest"
CLEANUP_MANIFEST_SCHEMA_VERSION = 1
_WRITE_TEMP_PREFIX = "s3-archiver-cleanup-manifest-"


def cleanup_record_from_entry(entry: ManifestEntry) -> CleanupRecord:
    """Build a cleanup record describing one archived source object."""

    return CleanupRecord(
        route_name=entry.route_name,
        source_identity=stable_identity_value(entry.source_identity),
        source_bucket=entry.source_bucket,
        key=entry.key,
        version_id=entry.version_id,
        size=entry.size,
        etag=entry.etag,
        destination_bucket=entry.destination_bucket,
        destination_key=entry.destination_key,
        destination_archive_key=entry.destination_archive_key,
        copy_mode=entry.copy_mode,
    )


def write_cleanup_manifest(
    path: Path,
    *,
    run_id: str,
    run_started_at_utc: str,
    records: Iterable[CleanupRecord],
) -> CleanupManifestSummary:
    """Atomically write a cleanup manifest and return its validated summary."""

    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    object_count = 0
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=_WRITE_TEMP_PREFIX, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        try:
            for record in records:
                line = _record_line(record)
                _ = handle.write(line)
                digest.update(line.encode("utf-8"))
                object_count += 1
            sha256 = digest.hexdigest()
            _ = handle.write(_footer_line(run_id, run_started_at_utc, object_count, sha256))
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    _ = temp_path.replace(path)
    status = CleanupManifestStatus.EMPTY if object_count == 0 else CleanupManifestStatus.VALID
    return CleanupManifestSummary(status, object_count, run_id, run_started_at_utc, sha256)


def validate_cleanup_manifest(path: Path) -> CleanupManifestSummary:
    """Stream a manifest and verify its integrity, raising when it is mangled."""

    digest = hashlib.sha256()
    record_count = 0
    footer: CleanupManifestSummary | None = None
    for line in _manifest_lines(path):
        if footer is not None:
            raise CleanupManifestError(f"{path}: content after the manifest summary line")
        decoded = _decode_line(path, line)
        if _is_footer(decoded):
            footer = _decode_footer(path, decoded)
        else:
            _ = _decode_record(path, decoded)
            digest.update((line + "\n").encode("utf-8"))
            record_count += 1
    return _finalize(path, footer, record_count, digest.hexdigest())


def iter_cleanup_records(path: Path) -> Iterator[CleanupRecord]:
    """Stream cleanup records from a validated manifest in file order."""

    footer_seen = False
    for line in _manifest_lines(path):
        if footer_seen:
            raise CleanupManifestError(f"{path}: content after the manifest summary line")
        decoded = _decode_line(path, line)
        if _is_footer(decoded):
            footer_seen = True
        else:
            yield _decode_record(path, decoded)


def _manifest_lines(path: Path) -> Iterator[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    lines = text.split("\n")
    if lines and lines[-1] == "":
        del lines[-1]
    yield from lines


def _decode_line(path: Path, line: str) -> Mapping[str, object]:
    try:
        decoded = cast(object, json.loads(line))
    except json.JSONDecodeError as exc:
        raise CleanupManifestError(f"{path}: invalid JSON manifest line: {exc}") from exc
    if not isinstance(decoded, dict):
        raise CleanupManifestError(f"{path}: manifest line is not a JSON object")
    return cast(Mapping[str, object], decoded)


def _is_footer(decoded: Mapping[str, object]) -> bool:
    return decoded.get("kind") == CLEANUP_MANIFEST_KIND


def _decode_footer(path: Path, decoded: Mapping[str, object]) -> CleanupManifestSummary:
    if decoded.get("schema_version") != CLEANUP_MANIFEST_SCHEMA_VERSION:
        raise CleanupManifestError(
            f"{path}: unsupported cleanup manifest schema version {decoded.get('schema_version')!r}"
        )
    object_count = _require_int(path, decoded, "object_count")
    run_id = _require_str(path, decoded, "run_id")
    run_started_at_utc = _require_str(path, decoded, "run_started_at_utc")
    sha256 = _require_str(path, decoded, "sha256")
    status = CleanupManifestStatus.EMPTY if object_count == 0 else CleanupManifestStatus.VALID
    return CleanupManifestSummary(status, object_count, run_id, run_started_at_utc, sha256)


def _decode_record(path: Path, decoded: Mapping[str, object]) -> CleanupRecord:
    if "source_identity" not in decoded:
        raise CleanupManifestError(f"{path}: manifest record missing 'source_identity'")
    return CleanupRecord(
        route_name=_require_str(path, decoded, "route_name"),
        source_identity=decoded["source_identity"],
        source_bucket=_require_str(path, decoded, "source_bucket"),
        key=_require_str(path, decoded, "key"),
        version_id=_optional_str(path, decoded, "version_id"),
        size=_require_int(path, decoded, "size"),
        etag=_optional_str(path, decoded, "etag"),
        destination_bucket=_require_str(path, decoded, "destination_bucket"),
        destination_key=_require_str(path, decoded, "destination_key"),
        destination_archive_key=_require_str(path, decoded, "destination_archive_key"),
        copy_mode=_require_str(path, decoded, "copy_mode"),
    )


def _finalize(
    path: Path,
    footer: CleanupManifestSummary | None,
    record_count: int,
    sha256: str,
) -> CleanupManifestSummary:
    if footer is None:
        if record_count == 0:
            return CleanupManifestSummary(CleanupManifestStatus.EMPTY, 0, "", "", "")
        raise CleanupManifestError(f"{path}: missing the manifest summary line")
    if footer.object_count != record_count:
        counts = f"summary object count {footer.object_count} does not match {record_count} records"
        raise CleanupManifestError(f"{path}: {counts}")
    if footer.sha256 != sha256:
        raise CleanupManifestError(f"{path}: manifest content digest mismatch")
    return footer


def _require_str(path: Path, decoded: Mapping[str, object], field: str) -> str:
    value = decoded.get(field)
    if not isinstance(value, str):
        raise CleanupManifestError(f"{path}: manifest field {field!r} must be a string")
    return value


def _optional_str(path: Path, decoded: Mapping[str, object], field: str) -> str | None:
    if field not in decoded:
        raise CleanupManifestError(f"{path}: manifest record missing {field!r}")
    value = decoded.get(field)
    if value is None or isinstance(value, str):
        return value
    raise CleanupManifestError(f"{path}: manifest field {field!r} must be a string or null")


def _require_int(path: Path, decoded: Mapping[str, object], field: str) -> int:
    value = decoded.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CleanupManifestError(f"{path}: manifest field {field!r} must be an integer")
    return value


def _record_line(record: CleanupRecord) -> str:
    return (
        json.dumps(
            {
                "copy_mode": record.copy_mode,
                "destination_archive_key": record.destination_archive_key,
                "destination_bucket": record.destination_bucket,
                "destination_key": record.destination_key,
                "etag": record.etag,
                "key": record.key,
                "route_name": record.route_name,
                "size": record.size,
                "source_bucket": record.source_bucket,
                "source_identity": record.source_identity,
                "version_id": record.version_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _footer_line(run_id: str, run_started_at_utc: str, object_count: int, sha256: str) -> str:
    return (
        json.dumps(
            {
                "kind": CLEANUP_MANIFEST_KIND,
                "object_count": object_count,
                "run_id": run_id,
                "run_started_at_utc": run_started_at_utc,
                "schema_version": CLEANUP_MANIFEST_SCHEMA_VERSION,
                "sha256": sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
