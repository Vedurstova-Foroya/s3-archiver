"""CLI entrypoint."""

from __future__ import annotations

import json
import os
from typing import Annotated

import typer
from s3_archiver_core.errors import S3ArchiverError
from s3_archiver_core.health import run_health_check
from s3_archiver_core.logging_config import configure_logging
from s3_archiver_core.settings import AppSettings

app: typer.Typer = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def root() -> None:
    """Run s3-archiver commands."""


@app.command()
def check(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the health report as JSON."),
    ] = False,
) -> None:
    """Validate configuration, logging, and bucket access."""

    try:
        settings = AppSettings.from_env(os.environ)
        log_file = configure_logging(settings)
        report = run_health_check(settings, log_file)
    except S3ArchiverError as exc:
        error_payload = {"status": "error", "message": str(exc)}
        typer.echo(json.dumps(error_payload, sort_keys=True) if json_output else str(exc), err=True)
        raise typer.Exit(code=1) from exc

    payload = report.as_dict()
    typer.echo(json.dumps(payload, sort_keys=True) if json_output else _render_text(payload))


def main() -> None:
    """Run the CLI app."""

    app()


def _render_text(payload: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in payload.items())
