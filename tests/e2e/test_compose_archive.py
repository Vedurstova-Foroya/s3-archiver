"""End-to-end tests for archive runs through the Docker Compose app service."""

from __future__ import annotations

import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

import pytest
from s3_archiver_localstack_support import last_json_object
from s3_archiver_localstack_support.harness import (
    LOCALSTACK_COMPOSE_ENDPOINT,
    LocalstackBucketPair,
)
from s3_archiver_localstack_support.objects import (
    listed_keys,
    put_test_object,
    read_tar_gz_members_text,
)

from tests.e2e.archive_compose_support import (
    compose_archive_client,
    run_archive_compose,
    write_archive_env_file,
)


class ArchivePhasePayload(TypedDict):
    status: str


class ArchivePayload(TypedDict):
    status: str
    source_bucket: str
    destination_bucket: str
    destination_archive_keys: list[str]
    source_object_count: int
    phases: dict[str, ArchivePhasePayload]


@pytest.mark.e2e()
def test_compose_archive_writes_daily_archives_without_cleanup_payload(
    tmp_path: Path,
    compose_env: dict[str, str],
    localstack_bucket_pair: LocalstackBucketPair,
) -> None:
    bucket_pair = localstack_bucket_pair
    source_client = compose_archive_client(tmp_path, compose_env, bucket_pair, "source")
    destination_client = compose_archive_client(tmp_path, compose_env, bucket_pair, "destination")
    source_prefix = "compose-archive/default"
    target_day = _target_day()
    archive_key = f"{source_prefix}/{target_day}.tar.gz"
    source_keys = _case_source_keys(source_prefix, target_day)
    for key in source_keys:
        _ = put_test_object(source_client, bucket_pair.source, key)
    assert listed_keys(source_client, bucket_pair.source) == source_keys
    env_file = write_archive_env_file(tmp_path, bucket_pair)
    run_env = dict(compose_env)
    run_env["APP_ENV_FILE"] = str(env_file)

    result = run_archive_compose(run_env, "run", "--rm", "app", "archive")
    payload = cast(ArchivePayload, cast(object, _payload(result.stdout)))

    assert payload["status"] == "ok"
    assert payload["source_bucket"] == bucket_pair.source
    assert payload["destination_bucket"] == bucket_pair.destination
    assert payload["source_object_count"] == len(source_keys)
    assert _phase_statuses(payload) == {
        "list": "ok",
        "copy": "ok",
        "verify": "ok",
    }
    assert payload["destination_archive_keys"] == [archive_key]
    assert listed_keys(destination_client, bucket_pair.destination) == {archive_key}
    assert read_tar_gz_members_text(destination_client, bucket_pair.destination, archive_key) == {
        key: f"payload for {key}\n" for key in source_keys
    }
    assert listed_keys(source_client, bucket_pair.source) == source_keys


@pytest.mark.e2e()
@pytest.mark.parametrize(
    "destination_endpoint",
    [
        LOCALSTACK_COMPOSE_ENDPOINT,
        "http://localstack-alt:4566",
    ],
)
def test_compose_archive_debug_logs_deterministic_tar_archive_metadata(
    tmp_path: Path,
    compose_env: dict[str, str],
    localstack_bucket_pair: LocalstackBucketPair,
    destination_endpoint: str,
) -> None:
    bucket_pair = localstack_bucket_pair
    source_client = compose_archive_client(tmp_path, compose_env, bucket_pair, "source")
    destination_client = compose_archive_client(tmp_path, compose_env, bucket_pair, "destination")
    target_day = _target_day()
    key = f"compose-debug/{target_day}T00-00-00-strategy.txt"
    archive_key = f"compose-debug/{target_day}.tar.gz"
    _ = put_test_object(
        source_client,
        bucket_pair.source,
        key,
        body=b"strategy\n",
    )
    env_file = write_archive_env_file(
        tmp_path,
        bucket_pair,
        overrides={
            "LOG_LEVEL": "DEBUG",
            "S3_DESTINATION_ENDPOINT": destination_endpoint,
        },
    )
    run_env = dict(compose_env)
    run_env["APP_ENV_FILE"] = str(env_file)

    result = run_archive_compose(run_env, "run", "--rm", "app", "archive")
    destination_head = destination_client.head_object(
        Bucket=bucket_pair.destination, Key=archive_key
    )
    metadata = cast(dict[str, str], destination_head["Metadata"])

    assert '"event": "archive.transfer.strategy_selected"' in result.stdout
    assert '"strategy": "deterministic_tar_gzip"' in result.stdout
    assert destination_head["ContentType"] == "application/gzip"
    assert metadata["s3-archiver-target-day"] == str(target_day)
    assert metadata["s3-archiver-source-count"] == "1"
    assert "s3-archiver-archive-sha256" in metadata
    assert read_tar_gz_members_text(destination_client, bucket_pair.destination, archive_key) == {
        key: "strategy\n"
    }


@pytest.mark.e2e()
def test_compose_archive_runtime_probe_uses_streaming_for_cross_endpoint_settings(
    compose_env: dict[str, str],
) -> None:
    probe = textwrap.dedent(
        """
        /opt/venv/bin/python - <<'PY'
        import json

        from s3_archiver_cli import main as cli
        from s3_archiver_core.archive_transfer import select_transfer_strategy
        from s3_archiver_core.s3 import transfer_capabilities_for_locations
        from s3_archiver_core.settings import AppSettings

        settings = AppSettings.from_env(cli._load_runtime_env())
        route = settings.routes[0]
        capabilities = transfer_capabilities_for_locations(route.source, route.destination)
        print(
            json.dumps(
                {
                    "multipart_copy": capabilities.multipart_copy,
                    "native_copy": capabilities.native_copy,
                    "source_endpoint": settings.routes[0].source.resolved_endpoint_url(),
                    "destination_endpoint": settings.routes[0].destination.resolved_endpoint_url(),
                    "strategy": select_transfer_strategy(
                        11,
                        capabilities,
                        simple_copy_limit_bytes=10,
                    ),
                },
                sort_keys=True,
            )
        )
        PY
        """
    ).strip()
    result = run_archive_compose(
        compose_env,
        "run",
        "--rm",
        "--no-deps",
        "-e",
        "APP_ENV_FILE=/dev/null",
        "-e",
        "S3_SOURCE_ENDPOINT=http://localstack:4566",
        "-e",
        "S3_DESTINATION_ENDPOINT=http://localstack-alt:4566",
        "--entrypoint",
        "sh",
        "app",
        "-lc",
        probe,
    )
    payload = _payload(result.stdout)

    assert payload["source_endpoint"] == "http://localstack:4566"
    assert payload["destination_endpoint"] == "http://localstack-alt:4566"
    assert payload["native_copy"] is False
    assert payload["multipart_copy"] is False
    assert payload["strategy"] == "multipart_streaming"


def _case_source_keys(prefix: str, target_day: date) -> set[str]:
    return {
        f"{prefix}/{target_day}T00-00-00-a.txt",
        f"{prefix}/{target_day}T01-00-00-b.txt",
    }


def _target_day() -> date:
    return datetime.now(tz=UTC).date() - timedelta(days=60)


def _phase_statuses(payload: ArchivePayload) -> dict[str, str]:
    return {
        name: phase["status"]
        for name, phase in payload["phases"].items()
        if name in {"list", "copy", "verify"}
    }


def _payload(output: str) -> dict[str, object]:
    return last_json_object(output)
