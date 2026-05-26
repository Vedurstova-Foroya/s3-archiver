"""Unit tests for manual visual demo compose orchestration."""

# pyright: reportAny=false, reportPrivateImportUsage=false, reportPrivateLocalImportUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
# pyright: reportUnnecessaryCast=false

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import cast

import pytest
import s3_archiver_visual_demo.compose as compose_module
from s3_archiver_localstack_support.harness import LocalstackBucketPair

pytestmark = pytest.mark.unit()


def test_run_orchestrates_compose_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = LocalstackBucketPair("source", "destination")
    calls: list[tuple[str, ...]] = []

    def fake_run_demo_compose(
        _env: dict[str, str],
        *args: str,
        check: bool = True,
        repo_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        _ = check, repo_root
        calls.append(args)
        return subprocess.CompletedProcess(("docker",), 0, stdout="", stderr="")

    monkeypatch.setattr(compose_module, "find_repo_root", lambda: Path.cwd())
    monkeypatch.setattr(compose_module, "new_localstack_bucket_pair", lambda: pair)
    monkeypatch.setitem(
        compose_module.__dict__,
        "_compose_env",
        lambda _bucket_pair, _app_env_file: {"LOCALSTACK_S3_URL": "http://127.0.0.1:4566"},
    )
    monkeypatch.setattr(compose_module, "run_demo_compose", fake_run_demo_compose)
    monkeypatch.setattr(compose_module, "wait_for_localstack_readiness", lambda **_kwargs: None)
    monkeypatch.setitem(compose_module.__dict__, "_ensure_bucket_pair", lambda *_args: None)
    monkeypatch.setitem(compose_module.__dict__, "_seed_run_and_verify", lambda *_args: None)
    monkeypatch.setitem(compose_module.__dict__, "_delete_bucket_pair", lambda *_args: None)

    compose_module.run()

    assert calls == [
        ("down", "-v", "--remove-orphans"),
        ("up", "-d", "localstack"),
        ("down", "-v", "--remove-orphans"),
    ]


def test_run_surfaces_cleanup_error_unless_demo_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    pair = LocalstackBucketPair("source", "destination")
    monkeypatch.setattr(compose_module, "find_repo_root", lambda: Path.cwd())
    monkeypatch.setattr(compose_module, "new_localstack_bucket_pair", lambda: pair)
    monkeypatch.setitem(
        compose_module.__dict__,
        "_compose_env",
        lambda _bucket_pair, _app_env_file: {"LOCALSTACK_S3_URL": "http://127.0.0.1:4566"},
    )
    monkeypatch.setattr(
        compose_module,
        "run_demo_compose",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(("docker",), 0),
    )
    monkeypatch.setattr(compose_module, "wait_for_localstack_readiness", lambda **_kwargs: None)
    monkeypatch.setitem(compose_module.__dict__, "_ensure_bucket_pair", lambda *_args: None)
    monkeypatch.setitem(compose_module.__dict__, "_seed_run_and_verify", lambda *_args: None)
    monkeypatch.setitem(
        compose_module.__dict__,
        "_delete_bucket_pair",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        compose_module.run(keep_compose=True)

    monkeypatch.setitem(
        compose_module.__dict__,
        "_seed_run_and_verify",
        lambda *_args: (_ for _ in ()).throw(ValueError("demo failed")),
    )
    with pytest.raises(ValueError, match="demo failed"):
        compose_module.run(keep_compose=True)


def test_compose_env_writes_demo_file_and_custom_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pair = LocalstackBucketPair("source", "destination")
    monkeypatch.setenv("LOCALSTACK_S3_URL", "http://127.0.0.1:4666")

    context = compose_module.__dict__["_demo_compose_context"](tmp_path, pair)
    env = context.compose_env

    assert env["LOCALSTACK_S3_URL"] == "http://127.0.0.1:4666"
    assert env["LOCALSTACK_HOST_PORT"] == "4666"
    assert env["TEST_S3_SOURCE_BUCKET"] == "source"
    assert Path(cast(dict[str, str], env)["APP_ENV_FILE"]).name == "compose-demo.env"

    monkeypatch.setenv("LOCALSTACK_S3_URL", "http://localstack")
    env_without_port = compose_module.__dict__["_demo_compose_context"](tmp_path, pair).compose_env
    assert "LOCALSTACK_HOST_PORT" not in cast(dict[str, str], env_without_port)


def test_demo_client_and_bucket_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pair = LocalstackBucketPair("source", "destination")
    clients: list[object] = []

    def fake_localstack_s3_client(env: dict[str, str], side: str) -> object:
        clients.append((env["S3_SOURCE_ENDPOINT"], side))
        return object()

    def fake_admin_client(_settings: object) -> object:
        return "admin"

    ensure_calls: list[object] = []
    delete_calls: list[object] = []
    monkeypatch.setattr(compose_module, "localstack_s3_client", fake_localstack_s3_client)
    monkeypatch.setattr(compose_module, "localstack_admin_client", fake_admin_client)
    monkeypatch.setattr(
        compose_module,
        "ensure_localstack_bucket_pair",
        lambda client, bucket_pair: ensure_calls.append((client, bucket_pair)),
    )
    monkeypatch.setattr(
        compose_module,
        "delete_localstack_bucket_pair",
        lambda client, bucket_pair, *, context: delete_calls.append((client, bucket_pair, context)),
    )

    context = compose_module.__dict__["_DemoComposeContext"](
        tmp_path=tmp_path,
        bucket_pair=pair,
        config_json=compose_module.demo_config_json(pair, prefix="compose-demo"),
        app_env_file=tmp_path / "compose-demo.env",
        compose_env={},
        localstack_endpoint="http://127.0.0.1:4566",
    )

    assert compose_module.__dict__["_demo_client"](context, "source")
    compose_module.__dict__["_ensure_bucket_pair"](context)
    compose_module.__dict__["_delete_bucket_pair"](context)

    assert clients == [("http://127.0.0.1:4566", "source")]
    assert ensure_calls == [("admin", pair)]
    assert delete_calls == [("admin", pair, "visual demo buckets")]


def test_seed_run_and_verify_success_and_source_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pair = LocalstackBucketPair("source", "destination")
    archive_day = date(2026, 2, 23)
    cases = (
        compose_module.target_day_demo_cases("compose-demo", archive_day)[0],
        compose_module.target_day_demo_cases("compose-demo", archive_day)[4],
    )
    source_keys = {case.key for case in cases} | {"newer", "invalid"}

    monkeypatch.setitem(compose_module.__dict__, "_demo_client", lambda *_args: object())
    monkeypatch.setattr(compose_module, "archive_demo_days", lambda _seed_now: (archive_day,))
    monkeypatch.setattr(compose_module, "target_day_demo_cases", lambda *_args: cases)
    monkeypatch.setattr(compose_module, "skipped_demo_keys", lambda *_args: ("newer", "invalid"))
    monkeypatch.setattr(
        compose_module, "expected_archive_members", lambda *_args: {"archive": {"a"}}
    )
    monkeypatch.setattr(
        compose_module, "expected_direct_destination_keys", lambda *_args: {"direct"}
    )
    monkeypatch.setattr(compose_module, "seed_daily_demo_objects", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        compose_module.__dict__,
        "_run_archive",
        lambda *_args: {"status": "ok"},
    )
    monkeypatch.setattr(
        compose_module,
        "run_demo_compose",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(("docker",), 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(compose_module.terminal, "print_image_build_intro", lambda: None)
    monkeypatch.setattr(compose_module.terminal, "print_demo_intro", lambda **_kwargs: None)

    class FakePrinter:
        def __init__(self, _archive_start_age_days: int) -> None:
            pass

        def emit(self, _line: str) -> None:
            pass

        def finish(self) -> None:
            pass

    monkeypatch.setattr(compose_module.terminal, "VisualDemoPrinter", FakePrinter)

    def fake_run_visual_walkthrough(
        settings: object,
        log_file: Path,
        *,
        archive_runner: Callable[[object, Path], dict[str, object]],
        emit: Callable[[str], None],
    ) -> dict[str, object]:
        _ = settings, emit
        emit("== S3 Archiver Visual Demo ==")
        return archive_runner(object(), log_file)

    monkeypatch.setattr(compose_module, "run_visual_walkthrough", fake_run_visual_walkthrough)
    verify_calls: list[dict[str, object]] = []

    def fake_verify_demo_result(**kwargs: object) -> None:
        verify_calls.append(kwargs)

    summaries: list[dict[str, object]] = []
    monkeypatch.setattr(compose_module, "verify_demo_result", fake_verify_demo_result)
    monkeypatch.setattr(compose_module, "listed_keys", lambda *_args: source_keys)
    monkeypatch.setattr(
        compose_module,
        "print_verified_summary",
        lambda payload, **kwargs: summaries.append({"payload": payload, **kwargs}),
    )

    context = compose_module.__dict__["_DemoComposeContext"](
        tmp_path=tmp_path,
        bucket_pair=pair,
        config_json=compose_module.demo_config_json(pair, prefix="compose-demo"),
        app_env_file=tmp_path / "compose-demo.env",
        compose_env={},
        localstack_endpoint="http://127.0.0.1:4566",
    )

    compose_module.__dict__["_seed_run_and_verify"](Path.cwd(), context)

    assert cast(dict[str, object], verify_calls[0])["source_keys"] == source_keys
    assert cast(dict[str, object], verify_calls[0])["skipped_count"] == 2
    assert summaries[0]["copied_count"] == 2

    monkeypatch.setattr(compose_module, "listed_keys", lambda *_args: {"wrong"})
    with pytest.raises(RuntimeError, match="unexpected source keys"):
        compose_module.__dict__["_seed_run_and_verify"](Path.cwd(), context)


def test_run_demo_compose_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[dict[str, str], tuple[str, ...], bool, Path]] = []

    def fake_run_app_compose(
        env: dict[str, str],
        *args: str,
        check: bool = True,
        repo_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((env, args, check, cast(Path, repo_root)))
        return subprocess.CompletedProcess(("docker",), 0, stdout="ok", stderr="")

    monkeypatch.setattr(compose_module, "find_repo_root", lambda: Path.cwd())
    monkeypatch.setattr(compose_module, "run_app_compose", fake_run_app_compose)

    assert compose_module.run_demo_compose({"A": "B"}, "ps", check=False).stdout == "ok"
    assert calls == [({"A": "B"}, ("ps",), False, Path.cwd())]
    assert compose_module.__dict__["_demo_payload"]('{"status":"ok"}\n') == {"status": "ok"}


def test_run_archive_invokes_app_archive_and_reads_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_demo_compose(
        env: dict[str, str],
        *args: str,
        check: bool = True,
        repo_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "env": env,
                "args": args,
                "check": check,
                "repo_root": repo_root,
            }
        )
        return subprocess.CompletedProcess(("docker",), 0, stdout='{"status":"ok"}\n', stderr="")

    monkeypatch.setattr(compose_module, "run_demo_compose", fake_run_demo_compose)
    result = compose_module.__dict__["_run_archive"]({"A": "B"}, Path.cwd())

    assert result == {"status": "ok"}
    args = ("run", "--rm", "-e", "ARCHIVER_CONFIG_JSON")
    assert calls == [
        {
            "env": {"A": "B"},
            "args": (*args, "-e", "ARCHIVER_PAYLOAD_DETAIL", "app", "archive"),
            "check": False,
            "repo_root": Path.cwd(),
        }
    ]
