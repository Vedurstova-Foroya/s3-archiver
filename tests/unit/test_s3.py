"""Tests for the typed boto3 adapter boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

import pytest
import s3_archiver_core.s3 as s3_module
from botocore.response import StreamingBody
from s3_archiver_core.settings import AppSettings


class FakeS3Client:
    """Minimal S3 client test double."""

    def head_bucket(self, *, Bucket: str) -> object:  # noqa: N803
        return {"Bucket": Bucket}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> object:  # noqa: N803
        return {"Bucket": Bucket, "Key": Key, "Body": Body}

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, StreamingBody]:  # noqa: N803
        raise AssertionError(f"unexpected get_object call for {Bucket=} {Key=}")


class RecordingSession:
    """Captures Session(...) initialization and client wiring."""

    init_args: dict[str, str]
    client_call: dict[str, object]
    returned_client: FakeS3Client

    def __init__(
        self,
        *,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
    ) -> None:
        self.init_args = {
            "aws_access_key_id": aws_access_key_id,
            "aws_secret_access_key": aws_secret_access_key,
            "region_name": region_name,
        }
        self.client_call = {}
        self.returned_client = FakeS3Client()

    def client(
        self,
        *,
        service_name: Literal["s3"],
        endpoint_url: str,
        config: object,
    ) -> FakeS3Client:
        self.client_call = {
            "service_name": service_name,
            "endpoint_url": endpoint_url,
            "config": config,
        }
        return self.returned_client


class RecordingConfig:
    """Captures Config(...) construction."""

    signature_version: str
    s3: dict[str, str]

    def __init__(
        self,
        *,
        signature_version: str,
        s3: dict[str, str],
    ) -> None:
        self.signature_version = signature_version
        self.s3 = s3


@pytest.mark.unit()
def test_build_s3_client_wires_derived_oci_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    settings = AppSettings.from_env(base_env)
    sessions: list[RecordingSession] = []

    def fake_session(
        *,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
    ) -> RecordingSession:
        session = RecordingSession(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
        )
        sessions.append(session)
        return session

    monkeypatch.setattr(s3_module, "Session", fake_session)
    monkeypatch.setattr(s3_module, "Config", RecordingConfig)

    client = s3_module.build_s3_client(settings)

    session = sessions[0]
    config = cast(RecordingConfig, session.client_call["config"])
    assert client is session.returned_client
    assert session.init_args == {
        "aws_access_key_id": "access-key",
        "aws_secret_access_key": "secret-key",
        "region_name": "eu-frankfurt-1",
    }
    assert session.client_call["service_name"] == "s3"
    assert session.client_call["endpoint_url"] == settings.resolved_endpoint_url()
    assert config.signature_version == "s3v4"
    assert config.s3 == {"addressing_style": "path"}


@pytest.mark.unit()
def test_build_s3_client_honors_endpoint_override_and_addressing_style(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
) -> None:
    base_env["S3_ENDPOINT_URL"] = "https://override.example.invalid"
    base_env["S3_ADDRESSING_STYLE"] = "virtual"
    settings = AppSettings.from_env(base_env)
    sessions: list[RecordingSession] = []

    def fake_session(
        *,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        region_name: str,
    ) -> RecordingSession:
        session = RecordingSession(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
        )
        sessions.append(session)
        return session

    monkeypatch.setattr(s3_module, "Session", fake_session)
    monkeypatch.setattr(s3_module, "Config", RecordingConfig)

    _ = s3_module.build_s3_client(settings)

    session = sessions[0]
    config = cast(RecordingConfig, session.client_call["config"])
    assert session.client_call["endpoint_url"] == "https://override.example.invalid"
    assert config.s3 == {"addressing_style": "virtual"}
