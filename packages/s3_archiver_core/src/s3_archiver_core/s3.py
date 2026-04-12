"""Typed S3 client construction."""

from __future__ import annotations

from typing import Literal, Protocol, cast

from boto3.session import Session
from botocore.config import Config
from mypy_boto3_s3.client import S3Client

from s3_archiver_core.settings import AppSettings


class S3ClientFactory(Protocol):
    """Typed callable wrapper around the boto3 client factory boundary."""

    def __call__(
        self, *, service_name: Literal["s3"], endpoint_url: str, config: Config
    ) -> S3Client:
        """Build an S3 client."""
        ...


def build_s3_client(settings: AppSettings) -> S3Client:
    """Create a configured S3 client for the current runtime settings."""

    session = Session(
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name=settings.region,
    )
    service_name: Literal["s3"] = "s3"
    client_factory = cast(S3ClientFactory, session.client)
    return client_factory(
        service_name=service_name,
        endpoint_url=settings.resolved_endpoint_url(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.addressing_style.value},
        ),
    )
