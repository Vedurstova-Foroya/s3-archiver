"""Integration test for the folder_timestamp parser with daily tar.gz copy mode."""

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

SOURCE_PATH = "data/wrf/gfs/"
DESTINATION_PATH = "data/wrf/gfs/"
DAY_FEB_01_KEYS = frozenset(
    {
        "data/wrf/gfs/2025/02/01/out_a.bin",
        "data/wrf/gfs/2025/02/01/out_b.bin",
    }
)
DAY_FEB_02_KEYS = frozenset({"data/wrf/gfs/2025/02/02/out_c.bin"})
ALL_SOURCE_KEYS = DAY_FEB_01_KEYS | DAY_FEB_02_KEYS
EXPECTED_ARCHIVE_KEYS = frozenset(
    {
        "data/wrf/gfs/2025-02-01.tar.gz",
        "data/wrf/gfs/2025-02-02.tar.gz",
    }
)
DAY_FEB_01_ARCHIVE_KEY = "data/wrf/gfs/2025-02-01.tar.gz"


@pytest.mark.integration()
def test_archive_command_groups_folder_timestamped_layout_into_daily_archives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    env = _archive_env(tmp_path, localstack_bucket_pair)
    configure_route(
        env,
        name="localstack-folder-timestamp-daily",
        parser="folder_timestamp",
        copy_mode="daily_tar_gz",
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

    feb_01 = read_deterministic_archive(
        destination_client, destination_bucket, DAY_FEB_01_ARCHIVE_KEY
    )

    assert feb_01.members == expected_payloads(DAY_FEB_01_KEYS)
    assert feb_01.gzip_mtime == 0
    assert set(feb_01.member_mtimes) == {0}
    assert set(feb_01.member_uids) == {0}
    assert set(feb_01.member_gids) == {0}
    assert set(feb_01.member_modes) == {0o644}
    assert set(feb_01.members) - DAY_FEB_01_KEYS == set()
    feb_02 = read_deterministic_archive(
        destination_client, destination_bucket, "data/wrf/gfs/2025-02-02.tar.gz"
    )
    assert feb_02.members == expected_payloads(DAY_FEB_02_KEYS)
    assert set(feb_02.members) & DAY_FEB_01_KEYS == set()
    assert listed_keys(source_client, source_bucket) == ALL_SOURCE_KEYS
    source_bodies_after = {
        key: read_object_text(source_client, source_bucket, key) for key in ALL_SOURCE_KEYS
    }
    assert source_bodies_after == source_bodies_before
    assert source_bodies_before == {key: expected_payload(key) for key in ALL_SOURCE_KEYS}
