"""Runtime helpers for the long-running ``schedule`` command.

These helpers wrap the scheduler's signal-handling, exponential backoff,
loop body, and structured shutdown logging so the CLI entrypoint stays
small.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType

from s3_archiver_core.errors import S3ArchiverError

_SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None
_SignalHandlerPair = tuple[_SignalHandler, _SignalHandler]


@dataclass
class ShutdownFlag:
    """Mutable flag toggled by signal handlers to request a clean exit."""

    requested: bool = False
    signal_name: str = field(default="")


def install_schedule_signals(flag: ShutdownFlag) -> _SignalHandlerPair:
    """Install SIGTERM and SIGINT handlers that mark ``flag`` for shutdown."""

    def handler(signum: int, _frame: FrameType | None) -> None:
        flag.requested = True
        flag.signal_name = signal.Signals(signum).name

    previous_term = signal.signal(signal.SIGTERM, handler)
    previous_int = signal.signal(signal.SIGINT, handler)
    return previous_term, previous_int


def restore_schedule_signals(previous: _SignalHandlerPair) -> None:
    """Restore the handlers returned by :func:`install_schedule_signals`."""

    previous_term, previous_int = previous
    _ = signal.signal(signal.SIGTERM, previous_term)
    _ = signal.signal(signal.SIGINT, previous_int)


def log_schedule_shutdown(signal_name: str) -> None:
    """Emit one structured info event when the scheduler exits cleanly."""

    logging.getLogger("s3_archiver.archive").info(
        "archive scheduler shutting down",
        extra={
            "event": "archive.schedule.shutdown",
            "signal": signal_name,
        },
    )


def log_lock_reconcile_failed() -> None:
    """Warn when the startup reconciliation pass cannot clear the lock."""

    logging.getLogger("s3_archiver.archive").warning(
        "archive scheduler could not reconcile lock at startup",
        extra={"event": "archive.schedule.lock_reconcile_failed"},
    )


def compute_backoff_delay(
    consecutive_failures: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 300.0,
) -> float:
    """Return the exponential backoff delay for the given failure count.

    Returns 0 when there have been no consecutive failures. Otherwise
    doubles each call, capped at ``cap_seconds``.
    """

    if consecutive_failures <= 0:
        return 0.0
    multiplier = 1 << (consecutive_failures - 1)
    return min(base_seconds * multiplier, cap_seconds)


def run_schedule_loop(
    flag: ShutdownFlag,
    *,
    run_once: Callable[[], None],
    sleep_until_next_tick: Callable[[float], None],
    report_error: Callable[[S3ArchiverError], None],
) -> None:
    """Drive the scheduler's main loop until ``flag`` requests shutdown.

    ``run_once`` raises :class:`S3ArchiverError` to signal that the run
    failed; consecutive failures lengthen the backoff before the next
    sleep, and a successful run resets the counter.
    """

    consecutive_failures = 0
    while True:
        if flag.requested:
            log_schedule_shutdown(flag.signal_name)
            return
        sleep_until_next_tick(compute_backoff_delay(consecutive_failures))
        if flag.requested:
            log_schedule_shutdown(flag.signal_name)
            return
        try:
            run_once()
        except S3ArchiverError as exc:
            consecutive_failures += 1
            report_error(exc)
        else:
            consecutive_failures = 0
