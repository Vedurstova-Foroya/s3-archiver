"""Deterministic tar.gz archive creation."""

from __future__ import annotations

import gzip
import hashlib
import tarfile
from collections.abc import Callable
from pathlib import Path

from s3_archiver_core._archive_object_activity import (
    entry_activity_watchdog,
    log_large_entry,
)
from s3_archiver_core._archive_protocols import ArchiveBucket
from s3_archiver_core.archive_manifest import ArchiveGroup
from s3_archiver_core.s3 import S3_CHUNK_BYTES

ORIGINAL_KEY_PAX_HEADER = "s3-archiver.original-key"
_SAFE_MEMBER_PREFIX = "s3-archiver-safe/"


def write_tar_gz_archive(
    source: ArchiveBucket,
    group: ArchiveGroup,
    path: Path,
    *,
    progress_logger: Callable[[], None] | None = None,
) -> None:
    """Write a deterministic tar.gz archive for one archive group."""

    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="w") as tar,
    ):
        for entry in group.entries:
            log_large_entry(
                operation="archive_member_write",
                entry=entry,
                destination_bucket=group.destination_bucket,
                destination_key=group.destination_archive_key,
            )
            with entry_activity_watchdog(
                operation="archive_member_write",
                entry=entry,
                destination_bucket=group.destination_bucket,
                destination_key=group.destination_archive_key,
            ):
                body = source.read_source_stream(entry.key, entry.version_id)
                try:
                    member_name, pax_headers = _member_name(entry.key)
                    info = tarfile.TarInfo(member_name)
                    info.size = entry.size
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.pax_headers = dict(pax_headers)
                    tar.addfile(info, body)
                    if progress_logger is not None:
                        progress_logger()
                finally:
                    body.close()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file."""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(S3_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _member_name(key: str) -> tuple[str, dict[str, str]]:
    if _safe_member_name(key):
        return key, {}

    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"{_SAFE_MEMBER_PREFIX}{digest}", {ORIGINAL_KEY_PAX_HEADER: key}


def _safe_member_name(key: str) -> bool:
    return (
        key != ""
        and not key.startswith(_SAFE_MEMBER_PREFIX)
        and not key.startswith("/")
        and "\\" not in key
        and not _has_windows_drive(key)
        and ".." not in key.split("/")
    )


def _has_windows_drive(key: str) -> bool:
    return len(key) >= 2 and key[1] == ":" and key[0].isalpha()
