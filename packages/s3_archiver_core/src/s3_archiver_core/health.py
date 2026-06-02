"""Health-check execution against S3 and runtime sinks."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from s3_archiver_core._archive_s3_helpers import parse_versioning_state
from s3_archiver_core.archive_s3 import S3ArchiveBucket
from s3_archiver_core.errors import HealthCheckError
from s3_archiver_core.parsers.protocol import ParserContext
from s3_archiver_core.parsers.registry import parser_for_kind
from s3_archiver_core.parsers.results import SelectedObject
from s3_archiver_core.s3 import S3Client, VersioningState, build_s3_client
from s3_archiver_core.settings import AppSettings, RouteSettings, S3LocationSettings

PARSER_SAMPLE_LIMIT = 25
PARSER_SKIP_EXAMPLE_LIMIT = 3


@dataclass(frozen=True, slots=True)
class _ParserSample:
    sampled: int
    matched: int
    skip_examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteHealthReport:
    """Serializable per-route health-check details."""

    name: str
    source_provider: str
    source_bucket: str
    source_endpoint_url: str
    source_path: str
    destination_provider: str
    destination_bucket: str
    destination_endpoint_url: str
    destination_path: str
    parser: str
    copy_mode: str
    source_versioning: VersioningState
    parser_sample_count: int = 0
    parser_match_count: int = 0
    parser_skip_examples: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return one JSON-serializable route health payload."""

        return {
            "name": self.name,
            "source_provider": self.source_provider,
            "source_bucket": self.source_bucket,
            "source_endpoint_url": self.source_endpoint_url,
            "source_path": self.source_path,
            "destination_provider": self.destination_provider,
            "destination_bucket": self.destination_bucket,
            "destination_endpoint_url": self.destination_endpoint_url,
            "destination_path": self.destination_path,
            "parser": self.parser,
            "copy_mode": self.copy_mode,
            "source_versioning": self.source_versioning,
            "parser_sample_count": self.parser_sample_count,
            "parser_match_count": self.parser_match_count,
            "parser_skip_examples": list(self.parser_skip_examples),
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Serializable output for the check command."""

    status: str
    source_provider: str
    source_bucket: str
    source_endpoint_url: str
    source_versioning: VersioningState
    destination_provider: str
    destination_bucket: str
    destination_endpoint_url: str
    log_file: str
    checked_at: str
    route_count: int
    routes: tuple[RouteHealthReport, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable health report."""

        return {
            "status": self.status,
            "source_provider": self.source_provider,
            "source_bucket": self.source_bucket,
            "source_endpoint_url": self.source_endpoint_url,
            "source_versioning": self.source_versioning,
            "destination_provider": self.destination_provider,
            "destination_bucket": self.destination_bucket,
            "destination_endpoint_url": self.destination_endpoint_url,
            "log_file": self.log_file,
            "checked_at": self.checked_at,
            "route_count": str(self.route_count),
            "routes": [route.as_dict() for route in self.routes],
        }


def run_health_check(settings: AppSettings, log_file: Path) -> HealthReport:
    """Validate bucket access and report the current runtime shape."""

    logger = logging.getLogger("s3_archiver.health")
    routes = settings.routes
    first_route = routes[0]
    source = first_route.source
    destination = first_route.destination
    source_endpoint_url = source.resolved_endpoint_url()
    destination_endpoint_url = destination.resolved_endpoint_url()
    logger.info(
        "running s3 health check",
        extra={
            "event": "health.started",
            "bucket": source.bucket,
            "endpoint_url": source_endpoint_url,
            "destination_bucket": destination.bucket,
            "destination_endpoint_url": destination_endpoint_url,
            "route_count": len(routes),
        },
    )
    route_reports = _check_routes(routes)
    source_versioning = route_reports[0].source_versioning
    logger.info(
        "s3 health check succeeded",
        extra={
            "event": "health.succeeded",
            "bucket": source.bucket,
            "destination_bucket": destination.bucket,
            "source_versioning": source_versioning,
            "route_count": len(routes),
        },
    )
    return HealthReport(
        status="ok",
        source_provider=source.provider.value,
        source_bucket=source.bucket,
        source_endpoint_url=source_endpoint_url,
        source_versioning=source_versioning,
        destination_provider=destination.provider.value,
        destination_bucket=destination.bucket,
        destination_endpoint_url=destination_endpoint_url,
        log_file=str(log_file),
        checked_at=datetime.now(tz=UTC).isoformat(),
        route_count=len(routes),
        routes=route_reports,
    )


def _check_routes(routes: tuple[RouteSettings, ...]) -> tuple[RouteHealthReport, ...]:
    reports: list[RouteHealthReport] = []
    for route in routes:
        source_versioning, sample = _check_route(route)
        reports.append(_route_report(route, source_versioning, sample))
    return tuple(reports)


def _check_route(route: RouteSettings) -> tuple[VersioningState, _ParserSample]:
    source_client = build_s3_client(route.source)
    _check_bucket_access(source_client, route.source, f"route {route.name!r} source")
    source_versioning = _source_versioning(source_client, route.source)
    destination_client = build_s3_client(route.destination)
    _check_bucket_access(destination_client, route.destination, f"route {route.name!r} destination")
    sample = _check_route_parser(route, source_client, source_versioning)
    return source_versioning, sample


def _route_report(
    route: RouteSettings, source_versioning: VersioningState, sample: _ParserSample
) -> RouteHealthReport:
    return RouteHealthReport(
        name=route.name,
        source_provider=route.source.provider.value,
        source_bucket=route.source.bucket,
        source_endpoint_url=route.source.resolved_endpoint_url(),
        source_path=route.source.path,
        destination_provider=route.destination.provider.value,
        destination_bucket=route.destination.bucket,
        destination_endpoint_url=route.destination.resolved_endpoint_url(),
        destination_path=route.destination.path,
        parser=route.parser.value,
        copy_mode=route.copy_mode.value,
        source_versioning=source_versioning,
        parser_sample_count=sample.sampled,
        parser_match_count=sample.matched,
        parser_skip_examples=sample.skip_examples,
    )


def _check_route_parser(
    route: RouteSettings, client: S3Client, versioning_state: VersioningState
) -> _ParserSample:
    """Sample up to PARSER_SAMPLE_LIMIT keys and verify the parser matches them.

    Fails when the source has objects under the configured prefix but none of
    the sampled keys produce a SelectedObject. An empty prefix passes through
    silently (the bucket may not have been populated yet).
    """

    parser = parser_for_kind(route.parser)
    bucket = S3ArchiveBucket(client, route.source.bucket)
    sampled = 0
    matched = 0
    skip_examples: list[str] = []
    try:
        for listed in bucket.list_source_objects(versioning_state, prefix=route.source.path):
            if sampled >= PARSER_SAMPLE_LIMIT:
                break
            sampled += 1
            context = ParserContext(listed, listed.properties)
            try:
                result = parser.parse(listed, context)
            except ValueError as exc:
                if len(skip_examples) < PARSER_SKIP_EXAMPLE_LIMIT:
                    skip_examples.append(f"{listed.key}: parser error: {exc}")
                continue
            if isinstance(result, SelectedObject):
                matched += 1
            elif len(skip_examples) < PARSER_SKIP_EXAMPLE_LIMIT:
                skip_examples.append(f"{listed.key}: {result.reason}")
    except (BotoCoreError, ClientError) as exc:
        raise HealthCheckError(
            f"Failed to sample source objects for route {route.name!r}: {exc}"
        ) from exc
    if sampled > 0 and matched == 0:
        examples = "; ".join(skip_examples)
        location = f"{route.source.bucket}/{route.source.path}"
        message = (
            f"route {route.name!r} parser {route.parser.value!r} matched 0 of "
            f"{sampled} sampled object(s) under {location}; examples: {examples}"
        )
        raise HealthCheckError(message)
    return _ParserSample(sampled, matched, tuple(skip_examples))


def _check_bucket_access(client: S3Client, location: S3LocationSettings, side: str) -> None:
    try:
        _ = client.head_bucket(Bucket=location.bucket)
    except (BotoCoreError, ClientError) as exc:
        raise HealthCheckError(
            f"Failed to access {side} bucket {location.bucket!r}: {exc}"
        ) from exc


def _source_versioning(client: S3Client, location: S3LocationSettings) -> VersioningState:
    try:
        response = client.get_bucket_versioning(Bucket=location.bucket)
    except (BotoCoreError, ClientError) as exc:
        raise HealthCheckError(
            f"Failed to read source bucket versioning for {location.bucket!r}: {exc}"
        ) from exc
    return parse_versioning_state(response.get("Status"))
