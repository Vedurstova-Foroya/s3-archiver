"""Run lock and timeout primitives for archive invocations."""

from __future__ import annotations

import json
import os
import socket
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import cast
from uuid import uuid4

LockRecoveryLogger = Callable[[str, Mapping[str, object]], None]
LOCK_OWNER_ENV = "S3_ARCHIVER_LOCK_OWNER"
SCHEDULER_LOCK_OWNER = "scheduler"


class FileArchiveRunLock:
    """File-backed run lock with timeout-based stale lock recovery."""

    _path: Path
    _recovery_logger: LockRecoveryLogger | None

    def __init__(self, path: Path, recovery_logger: LockRecoveryLogger | None = None) -> None:
        self._path = path
        self._recovery_logger = recovery_logger

    def acquire(
        self,
        *,
        run_id: str,
        run_started_at_utc: datetime,
        timeout: timedelta,
        recover_unknown_host: bool = False,
    ) -> bool:
        """Acquire the file lock unless a non-stale run owns it."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = _lock_payload(run_id, run_started_at_utc)
        with _exclusive_lock_guard(self._path):
            for _attempt in range(2):
                if _install_lock_payload(self._path, payload):
                    return True
                if not self._take_over_existing_lock(timeout, recover_unknown_host):
                    return False
            return False

    def release(self, *, run_id: str) -> None:
        """Release the lock only when the expected run owns it."""

        with _exclusive_lock_guard(self._path):
            if not self._path.exists():
                return
            if _lock_run_id(self._path) == run_id:
                _ = _dispose_lock_path(self._path, "released")

    def _take_over_existing_lock(self, timeout: timedelta, recover_unknown_host: bool) -> bool:
        decoded = _lock_json(self._path)
        reason = _stale_lock_reason(decoded, timeout, recover_unknown_host)
        if reason is None:
            return False
        if not _dispose_lock_path(self._path, "stale"):
            return False
        self._log_recovery(reason, decoded)
        return True

    def _log_recovery(self, reason: str, payload: Mapping[str, object]) -> None:
        if self._recovery_logger is not None:
            self._recovery_logger(reason, payload)


def _lock_payload(run_id: str, run_started_at_utc: datetime) -> str:
    payload_fields: dict[str, object] = {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "run_id": run_id,
        "run_started_at_utc": run_started_at_utc.isoformat(),
    }
    if owner := os.environ.get(LOCK_OWNER_ENV):
        payload_fields["owner"] = owner
    return json.dumps(payload_fields, sort_keys=True)


def _stale_lock_reason(
    decoded: Mapping[str, object], timeout: timedelta, recover_unknown_host: bool
) -> str | None:
    started = _lock_started_at(decoded)
    if started is None:
        return "invalid_lock_metadata"
    timed_out = datetime.now(tz=UTC) - started > timeout
    if not timed_out and _lock_process_is_alive_on_this_host(decoded):
        return None
    if not timed_out and not _lock_process_is_dead_on_this_host(decoded):
        if recover_unknown_host and _lock_process_is_on_unknown_host(decoded):
            return "stale_lock_unknown_host"
        return None
    return "stale_lock_timed_out" if timed_out else "stale_lock_abandoned"


@contextmanager
def _exclusive_lock_guard(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(_guard_path(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flock(descriptor, LOCK_EX)
        yield
    finally:
        flock(descriptor, LOCK_UN)
        os.close(descriptor)


def _install_lock_payload(path: Path, payload: str) -> bool:
    temp_path = _write_lock_temp(path, payload)
    try:
        try:
            os.link(temp_path, path)
        except FileExistsError:
            return False
        return True
    finally:
        _safe_unlink(temp_path)


def _write_lock_temp(path: Path, payload: str) -> Path:
    descriptor, raw_temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            _ = lock_file.write(payload)
            lock_file.flush()
            os.fsync(lock_file.fileno())
    except Exception:
        _safe_unlink(temp_path)
        raise
    return temp_path


def _dispose_lock_path(path: Path, label: str) -> bool:
    stale_path = _disposable_path(path, label)
    try:
        _ = path.rename(stale_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    _safe_unlink(stale_path)
    return True


def _disposable_path(path: Path, label: str) -> Path:
    return path.with_name(f"{path.name}.{label}-{uuid4().hex}")


def _guard_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.guard")


def parse_duration(value: str) -> timedelta:
    """Parse archive durations like ``7d``, ``12h``, ``30m``, or ``45s``."""

    if len(value) < 2:
        raise ValueError(f"invalid duration {value!r}")
    stripped = value.strip().lower()
    amount = int(stripped[:-1])
    unit = stripped[-1]
    if amount <= 0:
        raise ValueError(f"invalid duration {value!r}")
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "s":
        return timedelta(seconds=amount)
    raise ValueError(f"invalid duration {value!r}")


def _lock_started_at(decoded: Mapping[str, object]) -> datetime | None:
    value = decoded.get("run_started_at_utc")
    if not isinstance(value, str):
        return None
    try:
        started = datetime.fromisoformat(value)
    except ValueError:
        return None
    if started.tzinfo is None or started.utcoffset() is None:
        return None
    return started.astimezone(UTC)


def _lock_run_id(path: Path) -> str | None:
    value = _lock_json(path).get("run_id")
    if isinstance(value, str):
        return value
    return None


def _lock_process_is_alive_on_this_host(decoded: Mapping[str, object]) -> bool:
    hostname = decoded.get("hostname")
    pid = decoded.get("pid")
    if hostname != socket.gethostname() or type(pid) is not int or pid <= 0:
        return False
    return _process_is_alive(pid)


def _lock_process_is_dead_on_this_host(decoded: Mapping[str, object]) -> bool:
    hostname = decoded.get("hostname")
    pid = decoded.get("pid")
    return (
        hostname == socket.gethostname()
        and type(pid) is int
        and pid > 0
        and not _process_is_alive(pid)
    )


def _lock_process_is_on_unknown_host(decoded: Mapping[str, object]) -> bool:
    hostname = decoded.get("hostname")
    return (
        isinstance(hostname, str)
        and hostname != socket.gethostname()
        and decoded.get("owner") == SCHEDULER_LOCK_OWNER
    )


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_json(path: Path) -> Mapping[str, object]:
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(decoded, dict):
        return cast(Mapping[str, object], decoded)
    return {}


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
