"""Health-check execution against S3 and runtime sinks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from s3_archiver_core.errors import HealthCheckError
from s3_archiver_core.s3 import build_s3_client
from s3_archiver_core.settings import AppSettings


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Serializable output for the check command."""

    status: str
    provider: str
    bucket: str
    endpoint_url: str
    log_file: str
    checked_at: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serializable health report."""

        return {
            "status": self.status,
            "provider": self.provider,
            "bucket": self.bucket,
            "endpoint_url": self.endpoint_url,
            "log_file": self.log_file,
            "checked_at": self.checked_at,
        }


def run_health_check(settings: AppSettings, log_file: Path) -> HealthReport:
    """Validate bucket access and report the current runtime shape."""

    logger = logging.getLogger("s3_archiver.health")
    endpoint_url = settings.resolved_endpoint_url()
    logger.info(
        "running s3 health check",
        extra={"event": "health.started", "bucket": settings.bucket, "endpoint_url": endpoint_url},
    )
    client = build_s3_client(settings)
    try:
        _ = client.head_bucket(Bucket=settings.bucket)
    except (BotoCoreError, ClientError) as exc:
        raise HealthCheckError(f"Failed to access bucket {settings.bucket!r}: {exc}") from exc
    logger.info(
        "s3 health check succeeded",
        extra={"event": "health.succeeded", "bucket": settings.bucket},
    )
    return HealthReport(
        status="ok",
        provider=settings.provider.value,
        bucket=settings.bucket,
        endpoint_url=endpoint_url,
        log_file=str(log_file),
        checked_at=datetime.now(tz=UTC).isoformat(),
    )
