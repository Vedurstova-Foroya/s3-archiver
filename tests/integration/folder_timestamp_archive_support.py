"""Shared helpers for folder timestamp archive integration tests."""

from __future__ import annotations

import gzip
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO

from s3_archiver_core.s3 import S3Client
from s3_archiver_localstack_support.objects import read_object_bytes

from tests.integration.archive_cli_test_support import update_single_route_config

_GZIP_MAGIC = b"\x1f\x8b"


@dataclass(frozen=True, slots=True)
class DeterministicArchive:
    """Captured tar.gz state used by determinism assertions."""

    members: dict[str, str]
    member_mtimes: tuple[int, ...]
    member_uids: tuple[int, ...]
    member_gids: tuple[int, ...]
    member_modes: tuple[int, ...]
    gzip_mtime: int


def configure_route(
    env: dict[str, str],
    *,
    name: str,
    parser: str,
    copy_mode: str,
    source_path: str,
    destination_path: str,
) -> None:
    """Replace the default route config with the provided parser/copy-mode."""

    update_single_route_config(
        env,
        name=name,
        parser=parser,
        copy_mode=copy_mode,
        source_path=source_path,
        destination_path=destination_path,
    )


def read_deterministic_archive(client: S3Client, bucket: str, key: str) -> DeterministicArchive:
    """Return tar.gz contents plus determinism metadata for a LocalStack object."""

    payload = read_object_bytes(client, bucket, key)
    if not payload.startswith(_GZIP_MAGIC):
        raise AssertionError(f"expected gzip-framed payload for {key}")
    gzip_mtime = int.from_bytes(payload[4:8], byteorder="little", signed=False)
    members: dict[str, str] = {}
    mtimes: list[int] = []
    uids: list[int] = []
    gids: list[int] = []
    modes: list[int] = []
    with (
        gzip.GzipFile(fileobj=BytesIO(payload), mode="rb") as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="r:") as archive,
    ):
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            members[member.name] = extracted.read().decode()
            mtimes.append(int(member.mtime))
            uids.append(member.uid)
            gids.append(member.gid)
            modes.append(member.mode)
    return DeterministicArchive(
        members=members,
        member_mtimes=tuple(mtimes),
        member_uids=tuple(uids),
        member_gids=tuple(gids),
        member_modes=tuple(modes),
        gzip_mtime=gzip_mtime,
    )


def expected_payload(key: str) -> str:
    """Return the canonical body produced by put_test_object for a key."""

    return f"payload for {key}\n"


def expected_payloads(keys: Iterable[str]) -> dict[str, str]:
    """Return key -> expected body mapping for keys seeded via put_test_object."""

    return {key: expected_payload(key) for key in keys}
