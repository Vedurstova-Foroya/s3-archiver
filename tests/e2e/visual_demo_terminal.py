"""Terminal renderer for the visual e2e demo."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class ComposeRunner(Protocol):
    """Callable shape for the shared compose helper."""

    def __call__(
        self,
        env: dict[str, str],
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]: ...


def run_visual_demo(
    env: dict[str, str],
    *,
    repo_root: Path,
    cli_command: str = "demo",
    compose_runner: ComposeRunner,
    retryable_messages: Collection[str],
    retryable_returncodes: Collection[int],
    retries: int,
    retry_delay_seconds: float,
    retention_days: int,
    seeded_count: int,
) -> subprocess.CompletedProcess[str]:
    _print_demo_header("Preparing the runtime image")
    print("  Building the app image quietly. Build logs are shown only if the build fails.")
    build_result = compose_runner(env, "build", "app", check=False)
    if build_result.returncode != 0:
        raise AssertionError(
            "\n".join(
                (
                    "failed to build the app image",
                    f"stdout:\n{build_result.stdout}",
                    f"stderr:\n{build_result.stderr}",
                )
            )
        )

    _print_demo_header("Running the compose-backed demo")
    print("  LocalStack has fresh source and destination buckets for this test run.")
    print(
        "  Seeded "
        + f"{seeded_count} source objects: "
        + "valid, invalid, and unsafe-key timestamp examples."
    )
    print(f"  Retention policy: archive objects older than {retention_days} days.")
    print(
        f"  The next lines are live output from `s3-archiver {cli_command}`, "
        + "with JSON logs hidden."
    )
    print()

    for attempt in range(retries + 1):
        result = _run_visual_demo_once(
            env,
            repo_root=repo_root,
            retention_days=retention_days,
            cli_command=cli_command,
        )
        if result.returncode == 0:
            return result
        if attempt == retries or _is_non_retryable_visual_demo_error(
            result,
            retryable_messages=retryable_messages,
            retryable_returncodes=retryable_returncodes,
        ):
            raise AssertionError(
                "\n".join(
                    (
                        f"visual demo failed with exit code {result.returncode}",
                        f"stdout:\n{result.stdout}",
                        f"stderr:\n{result.stderr}",
                    )
                )
            )
        print()
        print("  Compose reported a retryable startup issue; retrying the demo command.")
        time.sleep(retry_delay_seconds)

    raise AssertionError("visual demo retry loop exhausted without returning")


def _run_visual_demo_once(
    env: dict[str, str],
    *,
    repo_root: Path,
    retention_days: int,
    cli_command: str,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--profile", "test", "run", "--rm", "app", cli_command]
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_lines: list[str] = []
    if process.stdout is None:
        raise AssertionError("visual demo process stdout was not captured")
    printer = _SampledDemoPrinter(retention_days)
    with process.stdout:
        for raw_line in process.stdout:
            output_lines.append(raw_line)
            printer.print_line(raw_line.rstrip("\n"))
    printer.finish()
    return_code = process.wait()
    output = "".join(output_lines)
    return subprocess.CompletedProcess(command, return_code, stdout=output, stderr="")


class _SampledDemoPrinter:
    def __init__(self, retention_days: int) -> None:
        self.retention_days: int = retention_days
        self.object_count: int = 0
        self.tail: list[str] = []

    def print_line(self, line: str) -> None:
        if line.startswith(("SOURCE ", "DEST   ", "COPY   ", "DELETE ", "GROUP  ")):
            formatted = _friendly_demo_line(line)
            self.object_count += 1
            if self.object_count <= 3:
                print(f"  {formatted}")
                return
            self.tail = [*self.tail[-2:], formatted]
            return
        self.finish()
        _print_visual_demo_line(line, retention_days=self.retention_days)

    def finish(self) -> None:
        if self.object_count > 3:
            omitted = self.object_count - 6
            if omitted > 0:
                print(f"  ... {omitted} rows omitted; showing the last 3 rows ...")
            for line in self.tail:
                print(f"  {line}")
        self.object_count = 0
        self.tail = []


def _print_visual_demo_line(line: str, *, retention_days: int) -> None:
    stripped = line.strip()
    if not stripped:
        print()
        return
    if (
        stripped.startswith("{")
        or stripped == "Demo summary JSON follows on the next line."
        or stripped.startswith("Container ")
        or stripped.startswith("Volume ")
    ):
        return
    match line:
        case "== S3 Archiver Visual Demo ==":
            _print_demo_header("S3 Archiver visual e2e demo")
            print("  This is a real Docker Compose run against LocalStack S3.")
        case "== S3 Archiver Cleanup Visual Demo ==":
            _print_demo_header("S3 Archiver cleanup visual e2e demo")
            print("  This is a real Docker Compose run against LocalStack S3.")
        case "== Preflight ==":
            _print_step("1/5", "Preflight checks")
            print("  Confirming configuration, logging, and bucket access before archiving.")
        case "== Before archive ==":
            _print_step("2/5", "Starting bucket state")
            print("  s3 ls-style view before archive: timestamped source; empty destination.")
        case "== Archive Candidates ==":
            _print_step("3/5", "Archive selection")
            print(
                "  Applying retention-window selection from filenames and path folders "
                + f"using {retention_days} retention days, grouped by each data day."
            )
            print(
                "  The seed includes flat filenames, YYYY/MM/DD folders, compact dates, "
                + "offsets, Z suffixes, and newer retained objects."
            )
        case "Running archive workflow against the configured buckets...":
            _print_step("4/5", "Archive execution")
            print("  The app is listing, copying, verifying, and applying the cleanup policy.")
        case "Running archive workflow with cleanup enabled against the configured buckets...":
            _print_step("4/4", "Archive execution and cleanup")
            print("  The app is listing, copying, verifying, and deleting verified source objects.")
        case "== Archive Result ==":
            print()
            print("  Archive phase results")
        case "== After archive ==":
            print()
            print("  s3 ls-style view after archive")
        case "== After cleanup ==":
            print()
            print("  s3 ls-style view after verified cleanup")
            print(
                "  Archived source objects have been deleted; retained and skipped objects remain."
            )
        case "Running cleanup preview without deleting source objects...":
            _print_step("5/5", "Cleanup preview")
            print("  Cleanup is disabled, so this shows what would be deleted without deleting it.")
        case "== Cleanup Preview ==":
            print()
            print("  Cleanup preview result")
        case "== After cleanup preview ==":
            print()
            print("  s3 ls-style view after cleanup preview")
            print(
                "  This is the real unchanged bucket state; preview mode wrote a manifest "
                + "but did not delete source objects."
            )
        case _:
            print(f"  {_friendly_demo_line(line)}")


def _friendly_demo_line(line: str) -> str:
    object_line_prefixes = {
        "SOURCE ": "source",
        "DEST   ": "dest",
        "COPY   ": "copy",
        "DELETE ": "delete",
    }
    for prefix, label in object_line_prefixes.items():
        if line.startswith(prefix):
            return _s3_ls_style_line(label, line[len(prefix) :])
    return line


def _s3_ls_style_line(label: str, fields: str) -> str:
    key = _field(fields, "key")
    size = _field(fields, "size")
    last_modified = _field(fields, "last_modified")
    if key is None or size is None or last_modified is None:
        return f"{label:<6} | {fields}"
    timestamp = datetime.fromisoformat(last_modified).astimezone(UTC)
    row = f"{label:<6} | {timestamp:%Y-%m-%d %H:%M:%S} {int(size):>10} {key}"
    details: list[str] = []
    eligible = _field(fields, "eligible")
    if eligible is not None:
        details.append(f"archive_candidate={eligible.lower()}")
    present_in_destination = _field(fields, "present_in_destination")
    if present_in_destination is not None:
        details.append(f"in_destination={present_in_destination.lower()}")
    for name in ("version_id", "source_last_modified"):
        value = _field(fields, name)
        if value is not None:
            details.append(f"{name}={value}")
    if details:
        return f"{row} | {', '.join(details)}"
    return row


def _field(fields: str, name: str) -> str | None:
    prefix = f"{name}="
    for part in fields.split():
        if part.startswith(prefix):
            return part.removeprefix(prefix)
    return None


def _print_demo_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title.upper())
    print("=" * 78)


def _print_step(number: str, title: str) -> None:
    print()
    print(f"[{number}] {title}")
    print("-" * (len(number) + len(title) + 4))


def _is_non_retryable_visual_demo_error(
    result: subprocess.CompletedProcess[str],
    *,
    retryable_messages: Collection[str],
    retryable_returncodes: Collection[int],
) -> bool:
    if result.returncode in retryable_returncodes:
        return False
    return not any(
        message in result.stderr or message in result.stdout for message in retryable_messages
    )
