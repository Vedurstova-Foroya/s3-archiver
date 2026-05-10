"""Bounded archive worker execution."""

from __future__ import annotations

from collections.abc import Callable
from queue import Empty, Queue
from threading import Thread

from s3_archiver_core.archive_manifest import ArchiveGroup, ManifestEntry


def run_archive_workers(
    entries: tuple[ManifestEntry, ...],
    max_workers: int,
    worker: Callable[[ManifestEntry], str | None],
    timed_out: Callable[[], bool],
    time_remaining: Callable[[], float],
) -> tuple[str, ...]:
    """Run archive phase workers until all entries complete or timeout expires."""

    failures: list[str] = []
    worker_count = max(1, max_workers)
    for batch_start in range(0, len(entries), worker_count):
        if timed_out():
            failures.append("archive run timed out")
            break
        batch = entries[batch_start : batch_start + worker_count]
        results: Queue[str | None] = Queue()
        for entry in batch:
            thread = Thread(target=_put_worker_result, args=(results, worker, entry), daemon=True)
            thread.start()
        pending = len(batch)
        while pending:
            try:
                failure = results.get(timeout=time_remaining())
            except Empty:
                failures.append("archive run timed out")
                return tuple(failures)
            pending -= 1
            if failure is not None:
                failures.append(failure)
    return tuple(failures)


def run_archive_group_workers(
    groups: tuple[ArchiveGroup, ...],
    max_workers: int,
    worker: Callable[[ArchiveGroup], str | None],
    timed_out: Callable[[], bool],
    time_remaining: Callable[[], float],
) -> tuple[str, ...]:
    """Run one worker per archive group through the manifest-entry worker runner."""

    groups_by_destination = {group.destination_archive_key: group for group in groups}

    def worker_entry(entry: ManifestEntry) -> str | None:
        group = groups_by_destination[entry.destination_archive_key]
        try:
            return worker(group)
        except Exception as exc:
            return f"{group.destination_archive_key}: {exc}"

    worker_entries = tuple(group.entries[0] for group in groups)
    return run_archive_workers(
        worker_entries,
        max_workers,
        worker_entry,
        timed_out,
        time_remaining,
    )


def _call_worker(worker: Callable[[ManifestEntry], str | None], entry: ManifestEntry) -> str | None:
    try:
        return worker(entry)
    except Exception as exc:
        return f"{entry.key}: {exc}"


def _put_worker_result(
    results: Queue[str | None],
    worker: Callable[[ManifestEntry], str | None],
    entry: ManifestEntry,
) -> None:
    try:
        failure = _call_worker(worker, entry)
    except Exception as exc:
        failure = f"worker failure: {exc}"
    results.put(failure)
