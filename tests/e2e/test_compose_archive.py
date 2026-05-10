"""End-to-end tests for archive runs through the Docker Compose app service."""

from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

import pytest
from s3_archiver_core.s3 import S3Client

from tests.e2e.compose_helpers import run_compose
from tests.integration.localstack_harness import (
    LOCALSTACK_COMPOSE_ENDPOINT,
    LOCALSTACK_HOST_ENDPOINT,
    LocalstackBucketPair,
    compose_runtime_log_dir,
    localstack_test_env,
)
from tests.integration.localstack_object_helpers import (
    listed_keys,
    localstack_s3_client,
    put_test_object,
    read_tar_gz_members_text,
)

_COMPOSE_RETRYABLE_MESSAGES = (
    "HeadBucket operation: Not Found",
    "Connection was closed before we received a valid response",
    'optional dependency "localstack" failed to start',
    "exited (137)",
    "unable to upgrade to tcp, received 409",
    "app is missing dependency localstack",
    "network s3-archiver_default not found",
    'container name "/s3-archiver-localstack-1" is already in use',
)
_COMPOSE_RETRYABLE_RETURNCODES = (4, 137)


class ArchivePhasePayload(TypedDict):
    status: str


class ArchivePayload(TypedDict):
    status: str
    source_bucket: str
    destination_bucket: str
    destination_archive_keys: list[str]
    manifest: dict[str, object]
    phases: dict[str, ArchivePhasePayload]


@pytest.mark.e2e()
@pytest.mark.parametrize(
    ("cleanup_value", "expected_cleanup_status", "source_should_remain"),
    [
        (None, "skipped", True),
        ("false", "skipped", True),
        ("true", "ok", False),
    ],
)
def test_compose_archive_writes_daily_archives_and_honors_cleanup_gate(
    tmp_path: Path,
    compose_env: dict[str, str],
    localstack_bucket_pair: LocalstackBucketPair,
    cleanup_value: str | None,
    expected_cleanup_status: str,
    source_should_remain: bool,
) -> None:
    bucket_pair = localstack_bucket_pair
    source_client = _client(tmp_path, bucket_pair, "source")
    destination_client = _client(tmp_path, bucket_pair, "destination")
    source_prefix = _case_source_prefix(cleanup_value)
    target_day = _target_day()
    archive_key = f"{source_prefix}/{target_day}.tar.gz"
    source_keys = _case_source_keys(source_prefix, target_day)
    for key in source_keys:
        _ = put_test_object(source_client, bucket_pair.source, key)
    assert listed_keys(source_client, bucket_pair.source) == source_keys
    env_file = _write_archive_env_file(tmp_path, bucket_pair, cleanup_value)
    run_env = dict(compose_env)
    run_env["APP_ENV_FILE"] = str(env_file)

    result = _run_compose(run_env, "run", "--rm", "app", "archive")
    payload = cast(ArchivePayload, cast(object, _payload(result.stdout)))

    assert payload["status"] == "ok"
    assert payload["source_bucket"] == bucket_pair.source
    assert payload["destination_bucket"] == bucket_pair.destination
    assert payload["manifest"]["object_count"] == len(source_keys)
    assert _phase_statuses(payload) == {
        "list": "ok",
        "copy": "ok",
        "verify": "ok",
        "cleanup": expected_cleanup_status,
    }
    assert payload["destination_archive_keys"] == [archive_key]
    assert listed_keys(destination_client, bucket_pair.destination) == {archive_key}
    assert read_tar_gz_members_text(destination_client, bucket_pair.destination, archive_key) == {
        key: f"payload for {key}\n" for key in source_keys
    }
    expected_source_keys = source_keys if source_should_remain else set[str]()
    assert listed_keys(source_client, bucket_pair.source) == expected_source_keys


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
    source_client = _client(tmp_path, bucket_pair, "source")
    destination_client = _client(tmp_path, bucket_pair, "destination")
    target_day = _target_day()
    key = f"compose-debug/{target_day}T00-00-00-strategy.txt"
    archive_key = f"compose-debug/{target_day}.tar.gz"
    _ = put_test_object(
        source_client,
        bucket_pair.source,
        key,
        body=b"strategy\n",
    )
    env_file = _write_archive_env_file(tmp_path, bucket_pair, None)
    env_text = env_file.read_text(encoding="utf-8").replace("LOG_LEVEL=INFO", "LOG_LEVEL=DEBUG")
    env_text = env_text.replace(
        f"S3_DESTINATION_ENDPOINT_URL={LOCALSTACK_COMPOSE_ENDPOINT}",
        f"S3_DESTINATION_ENDPOINT_URL={destination_endpoint}",
    )
    _ = env_file.write_text(env_text, encoding="utf-8")
    run_env = dict(compose_env)
    run_env["APP_ENV_FILE"] = str(env_file)

    result = _run_compose(run_env, "run", "--rm", "app", "archive")
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
        from s3_archiver_core.archive_options import ArchiveOptions
        from s3_archiver_core.archive_transfer import select_transfer_strategy
        from s3_archiver_core.settings import AppSettings

        settings = AppSettings.from_env(cli._load_runtime_env())
        capabilities = ArchiveOptions.from_settings(settings).transfer_capabilities
        print(
            json.dumps(
                {
                    "multipart_copy": capabilities.multipart_copy,
                    "native_copy": capabilities.native_copy,
                    "source_endpoint": settings.source.resolved_endpoint_url(),
                    "destination_endpoint": settings.destination.resolved_endpoint_url(),
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
    result = _run_compose(
        compose_env,
        "run",
        "--rm",
        "--no-deps",
        "-e",
        "APP_ENV_FILE=/dev/null",
        "-e",
        "S3_SOURCE_ENDPOINT_URL=http://localstack:4566",
        "-e",
        "S3_DESTINATION_ENDPOINT_URL=http://localstack-alt:4566",
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


def _case_source_prefix(cleanup_value: str | None) -> str:
    name = "unset" if cleanup_value is None else cleanup_value
    return f"compose-archive/{name}"


def _case_source_keys(prefix: str, target_day: date) -> set[str]:
    return {
        f"{prefix}/{target_day}T00-00-00-a.txt",
        f"{prefix}/{target_day}T01-00-00-b.txt",
    }


def _target_day() -> date:
    return datetime.now(tz=UTC).date() - timedelta(days=1)


def _write_archive_env_file(
    tmp_path: Path,
    bucket_pair: LocalstackBucketPair,
    cleanup_value: str | None,
) -> Path:
    env = localstack_test_env(
        bucket_pair,
        endpoint=LOCALSTACK_COMPOSE_ENDPOINT,
        log_dir=compose_runtime_log_dir(bucket_pair),
    )
    env["ARCHIVER_RETENTION_DAYS"] = "1"
    env["ARCHIVER_MAX_WORKERS"] = "1"
    if cleanup_value is None:
        del env["ARCHIVER_ENABLE_CLEANUP"]
    else:
        env["ARCHIVER_ENABLE_CLEANUP"] = cleanup_value
    env_file = tmp_path / "compose-archive.env"
    _ = env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(env.items())),
        encoding="utf-8",
    )
    return env_file


def _client(
    tmp_path: Path,
    bucket_pair: LocalstackBucketPair,
    side: Literal["source", "destination"],
) -> S3Client:
    env = localstack_test_env(
        bucket_pair,
        endpoint=LOCALSTACK_HOST_ENDPOINT,
        log_dir=str(tmp_path / "host-logs"),
    )
    return localstack_s3_client(env, side)


def _run_compose(
    env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_compose(
        env,
        *args,
        check=check,
        retryable_messages=_COMPOSE_RETRYABLE_MESSAGES,
        retryable_returncodes=_COMPOSE_RETRYABLE_RETURNCODES,
    )


def _phase_statuses(payload: ArchivePayload) -> dict[str, str]:
    return {
        name: phase["status"]
        for name, phase in payload["phases"].items()
        if name in {"list", "copy", "verify", "cleanup"}
    }


def _payload(output: str) -> dict[str, object]:
    json_line = next(line for line in reversed(output.splitlines()) if line.startswith("{"))
    return cast(dict[str, object], json.loads(json_line))
