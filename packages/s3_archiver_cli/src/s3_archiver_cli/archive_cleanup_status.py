"""Archive cleanup status and failure detail helpers."""

from __future__ import annotations

from s3_archiver_core.archive import ArchivePhaseResult, ArchiveRunResult

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]


def apply_group_cleanup_statuses(
    result: ArchiveRunResult, groups: list[dict[str, JsonValue]]
) -> None:
    """Apply per-group cleanup status to archive group payloads."""

    if result.cleanup.skipped:
        return
    failed_keys, has_unscoped_failures = _cleanup_failure_scope(result.cleanup, groups)
    verified = set(result.verified_archive_keys)
    skipped = set(result.skipped_archive_keys)
    for group in groups:
        key = group["destination_archive_key"]
        if key in skipped:
            group["cleanup_status"] = "skipped"
        elif (
            has_unscoped_failures
            or key in failed_keys
            or _group_has_cleanup_failure(group, failed_keys)
        ):
            group["cleanup_status"] = "error"
        elif result.cleanup.ok or key in verified:
            group["cleanup_status"] = "ok"


def payload_cleanup_known_keys(phase: str, payload: dict[str, JsonValue]) -> tuple[str, ...]:
    """Return known cleanup archive/source keys from an archive payload."""

    if phase != "cleanup":
        return ()
    archive_groups = payload.get("archive_groups")
    if not isinstance(archive_groups, list):
        return ()
    groups = [group for group in archive_groups if isinstance(group, dict)]
    return _cleanup_known_keys(groups)


def failure_key(detail: str, known_keys: tuple[str, ...] = ()) -> str | None:
    """Return the failure key from a phase failure detail."""

    key, _ = _failure_detail_parts(detail, known_keys)
    return key


def mismatch_payload(
    phase: str, detail: str, known_keys: tuple[str, ...] = ()
) -> dict[str, JsonValue] | None:
    """Return structured mismatch details for a phase failure."""

    if detail == "archive run timed out":
        return None
    key, mismatch_detail = _failure_detail_parts(detail, known_keys)
    return {
        "phase": phase,
        "key": key,
        "category": _mismatch_category(mismatch_detail),
        "detail": mismatch_detail,
    }


def _cleanup_failure_scope(
    result: ArchivePhaseResult,
    groups: list[dict[str, JsonValue]],
) -> tuple[set[str], bool]:
    known_keys = _cleanup_known_keys(groups)
    failed_keys: set[str] = set()
    has_unscoped_failures = False
    for failure in result.failures:
        failed_key = _cleanup_failure_key(failure, known_keys)
        if failed_key is None:
            has_unscoped_failures = True
        else:
            failed_keys.add(failed_key)
    return failed_keys, has_unscoped_failures


def _cleanup_known_keys(groups: list[dict[str, JsonValue]]) -> tuple[str, ...]:
    keys: set[str] = set()
    for group in groups:
        destination_key = group.get("destination_archive_key")
        if isinstance(destination_key, str) and destination_key:
            keys.add(destination_key)
        for source_object in _source_objects(group):
            source_key = source_object.get("key")
            if isinstance(source_key, str) and source_key:
                keys.add(source_key)
    return tuple(sorted(keys, key=len, reverse=True))


def _source_objects(group: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    source_objects = group.get("source_objects")
    if not isinstance(source_objects, list):
        return []
    return [item for item in source_objects if isinstance(item, dict)]


def _cleanup_failure_key(detail: str, known_keys: tuple[str, ...]) -> str | None:
    for key in known_keys:
        if detail == key or detail.startswith(f"{key}:"):
            return key
    return None


def _group_has_cleanup_failure(group: dict[str, JsonValue], failed_keys: set[str]) -> bool:
    return any(source_object.get("key") in failed_keys for source_object in _source_objects(group))


def _failure_detail_parts(detail: str, known_keys: tuple[str, ...]) -> tuple[str | None, str]:
    for key in known_keys:
        if detail == key:
            return key, ""
        if detail.startswith(f"{key}:"):
            return key, detail[len(key) + 1 :].strip()
    key, separator, remainder = detail.partition(":")
    if separator:
        return key, remainder.strip()
    return None, detail


def _mismatch_category(detail: str) -> str:
    normalized = detail.lower()
    for category in (
        "source fingerprint",
        "size",
        "object property",
        "metadata",
        "tag",
        "content",
        "source changed",
        "destination missing",
        "source missing",
    ):
        if category in normalized:
            return category.replace(" ", "_")
    return "archive_failure"
