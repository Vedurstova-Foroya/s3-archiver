"""Integration test for the folder_timestamp_child parser and tar.gz copy mode."""

from __future__ import annotations

from pathlib import Path

import pytest
from s3_archiver_localstack_support.harness import LocalstackBucketPair
from s3_archiver_localstack_support.objects import (
    listed_keys,
    put_test_object,
    read_object_text,
)

from tests.integration.archive_cli_test_support import archive_client as _client
from tests.integration.archive_cli_test_support import archive_env as _archive_env
from tests.integration.archive_cli_test_support import run_archive_command as _run_archive
from tests.integration.folder_timestamp_archive_support import (
    configure_route,
    expected_payload,
    expected_payloads,
    read_deterministic_archive,
)

SOURCE_PATH = "data/wrf/ecmwf/"
DESTINATION_PATH = "data/wrf/ecmwf/"
HOUR_00_D01_KEYS = frozenset(
    {
        "data/wrf/ecmwf/2026/05/16/00/d01/out.grib",
        "data/wrf/ecmwf/2026/05/16/00/d01/nested/sub.grib",
    }
)
HOUR_00_D02_KEYS = frozenset({"data/wrf/ecmwf/2026/05/16/00/d02/out.grib"})
HOUR_06_D01_KEYS = frozenset({"data/wrf/ecmwf/2026/05/16/06/d01/out.grib"})
ALL_SOURCE_KEYS = HOUR_00_D01_KEYS | HOUR_00_D02_KEYS | HOUR_06_D01_KEYS
EXPECTED_ARCHIVE_KEYS = frozenset(
    {
        "data/wrf/ecmwf/2026-05-16-00-d01.tar.gz",
        "data/wrf/ecmwf/2026-05-16-00-d02.tar.gz",
        "data/wrf/ecmwf/2026-05-16-06-d01.tar.gz",
    }
)
HOUR_00_D01_ARCHIVE_KEY = "data/wrf/ecmwf/2026-05-16-00-d01.tar.gz"


@pytest.mark.integration()
def test_archive_command_groups_wrf_layout_by_timestamp_child_folder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    configure_route(
        env,
        name="localstack-folder-timestamp-child",
        parser="folder_timestamp_child",
        copy_mode="timestamp_child_tar_gz",
        source_path=SOURCE_PATH,
        destination_path=DESTINATION_PATH,
    )
    source_client = _client(env, "source")
    destination_client = _client(env, "destination")
    source_bucket = localstack_bucket_pair.source
    destination_bucket = localstack_bucket_pair.destination
    for key in ALL_SOURCE_KEYS:
        _ = put_test_object(source_client, source_bucket, key)
    source_bodies_before = {
        key: read_object_text(source_client, source_bucket, key) for key in ALL_SOURCE_KEYS
    }

    payload = _run_archive(monkeypatch, env)

    assert payload["status"] == "ok"
    assert payload["source_object_count"] == len(ALL_SOURCE_KEYS)
    assert listed_keys(destination_client, destination_bucket) == EXPECTED_ARCHIVE_KEYS

    d01_hour_00 = read_deterministic_archive(
        destination_client, destination_bucket, HOUR_00_D01_ARCHIVE_KEY
    )

    assert d01_hour_00.members == expected_payloads(HOUR_00_D01_KEYS)
    assert d01_hour_00.gzip_mtime == 0
    assert set(d01_hour_00.member_mtimes) == {0}
    assert set(d01_hour_00.member_uids) == {0}
    assert set(d01_hour_00.member_gids) == {0}
    assert set(d01_hour_00.member_modes) == {0o644}
    other_archives = EXPECTED_ARCHIVE_KEYS - {HOUR_00_D01_ARCHIVE_KEY}
    leaked = set(d01_hour_00.members) - HOUR_00_D01_KEYS
    assert leaked == set()
    for archive_key in other_archives:
        other = read_deterministic_archive(destination_client, destination_bucket, archive_key)
        cross_leak = set(other.members) & HOUR_00_D01_KEYS
        assert cross_leak == set()
    assert listed_keys(source_client, source_bucket) == ALL_SOURCE_KEYS
    source_bodies_after = {
        key: read_object_text(source_client, source_bucket, key) for key in ALL_SOURCE_KEYS
    }
    assert source_bodies_after == source_bodies_before
    assert source_bodies_before == {key: expected_payload(key) for key in ALL_SOURCE_KEYS}
