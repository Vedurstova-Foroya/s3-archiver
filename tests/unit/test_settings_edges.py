"""Edge-case tests for settings parsing helpers."""

from __future__ import annotations

import pytest
from s3_archiver_core._route_config_fields import (
    addressing_style,
    endpoint,
    object_config,
    optional_string,
    required_string,
)
from s3_archiver_core._settings_parse import (
    EnvDecoder,
    ParseIssue,
    ParseResult,
    normalize_endpoint_url,
    normalize_endpoint_url_result,
    optional_env_result,
    parse_bool_result,
    parse_int_result,
    parse_runtime_duration_result,
    parse_string_array_result,
    require_env_result,
)
from s3_archiver_core.errors import ConfigError
from s3_archiver_core.settings import S3AddressingStyle, S3LocationSettings, S3Provider


@pytest.mark.unit()
def test_oci_endpoint_resolution_requires_namespace() -> None:
    location = S3LocationSettings(
        provider=S3Provider.OCI,
        access_key_id="access",
        secret_access_key="secret",
        region="eu-frankfurt-1",
        bucket="bucket",
        namespace=None,
        iam_user_ocid="ocid1.user.oc1..example",
        endpoint_url=None,
        addressing_style=S3AddressingStyle.PATH,
    )

    with pytest.raises(ConfigError, match="S3_NAMESPACE"):
        _ = location.resolved_endpoint_url()


@pytest.mark.unit()
def test_custom_endpoint_resolution_requires_explicit_endpoint() -> None:
    location = S3LocationSettings(
        provider=S3Provider.CUSTOM,
        access_key_id="access",
        secret_access_key="secret",
        region="us-east-1",
        bucket="bucket",
        namespace=None,
        iam_user_ocid=None,
        endpoint_url=None,
        addressing_style=S3AddressingStyle.PATH,
    )

    with pytest.raises(ConfigError, match="S3_ENDPOINT"):
        _ = location.resolved_endpoint_url()


@pytest.mark.unit()
def test_default_endpoint_resolution_covers_supported_providers() -> None:
    localstack = S3LocationSettings(
        provider=S3Provider.LOCALSTACK,
        access_key_id="access",
        secret_access_key="secret",
        region="us-east-1",
        bucket="bucket",
        namespace=None,
        iam_user_ocid=None,
        endpoint_url=None,
        addressing_style=S3AddressingStyle.PATH,
    )
    oci = S3LocationSettings(
        provider=S3Provider.OCI,
        access_key_id="access",
        secret_access_key="secret",
        region="eu-frankfurt-1",
        bucket="bucket",
        namespace="namespace",
        iam_user_ocid="ocid1.user.oc1..example",
        endpoint_url=None,
        addressing_style=S3AddressingStyle.PATH,
    )

    assert localstack.resolved_endpoint_url() == "http://localstack:4566"
    assert (
        oci.resolved_endpoint_url()
        == "https://namespace.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"
    )


@pytest.mark.unit()
def test_normalize_endpoint_url_raises_on_invalid_value() -> None:
    assert normalize_endpoint_url("http://host:4566") == "http://host:4566"
    with pytest.raises(ConfigError, match="ENDPOINT"):
        _ = normalize_endpoint_url("localhost:4566", field="ENDPOINT")


@pytest.mark.unit()
def test_normalize_endpoint_url_prepends_https_for_bare_hostname() -> None:
    assert (
        normalize_endpoint_url("fre1hvrsz7q6.compat.objectstorage.eu-frankfurt-1.oraclecloud.com")
        == "https://fre1hvrsz7q6.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"
    )
    assert normalize_endpoint_url("s3.example.com") == "https://s3.example.com"
    assert normalize_endpoint_url_result("", field="ENDPOINT").issue is not None


@pytest.mark.unit()
def test_parse_result_boundary_captures_issue_without_raising() -> None:
    result = parse_bool_result({"BOOL": "maybe"}, "BOOL", default=False)

    assert result.ok is False
    assert result.value is None
    assert result.issue is not None
    assert result.issue.field == "BOOL"


@pytest.mark.unit()
def test_parse_result_helpers_cover_error_edges() -> None:
    from datetime import timedelta

    assert parse_bool_result({"BOOL": "true"}, "BOOL", default=False).value is True
    assert parse_bool_result({"BOOL": "false"}, "BOOL", default=True).value is False
    assert parse_bool_result({}, "BOOL", default=True).value is True
    assert parse_int_result({"COUNT": "2"}, "COUNT", default=1, minimum=1).value == 2
    assert parse_int_result({"COUNT": " "}, "COUNT", default=1, minimum=1).value == 1
    assert parse_int_result({"COUNT": "no"}, "COUNT", default=1, minimum=1).issue is not None
    assert parse_int_result({"COUNT": "0"}, "COUNT", default=1, minimum=1).issue is not None
    assert parse_runtime_duration_result("7d", "DURATION").value == timedelta(days=7)
    assert parse_runtime_duration_result("soon", "DURATION").issue is not None
    assert parse_string_array_result({"ARRAY": '["prefix/"]'}, "ARRAY").value == ("prefix/",)
    assert parse_string_array_result({"ARRAY": ""}, "ARRAY").value == ()
    assert parse_string_array_result({"ARRAY": "["}, "ARRAY").issue is not None
    assert require_env_result({"VALUE": "configured"}, "VALUE").value == "configured"
    assert optional_env_result({"OPTIONAL": "set"}, "OPTIONAL").value == "set"
    assert parse_string_array_result({"ARRAY": "{}"}, "ARRAY").issue is not None
    assert parse_string_array_result({"ARRAY": "[1]"}, "ARRAY").issue is not None
    assert normalize_endpoint_url_result("localhost:4566", field="ENDPOINT").issue is not None
    assert normalize_endpoint_url_result("http://host/path?x=1", field="ENDPOINT").issue is not None
    assert normalize_endpoint_url_result("ftp://host", field="ENDPOINT").issue is not None
    assert normalize_endpoint_url_result("http://host:bad", field="ENDPOINT").issue is not None
    assert require_env_result({}, "MISSING").issue is not None
    assert optional_env_result({"OPTIONAL": " "}, "OPTIONAL").value is None


@pytest.mark.unit()
def test_route_config_field_helpers_cover_invalid_shapes() -> None:
    decoder = EnvDecoder({})
    assert object_config(decoder, [], "ROUTE") is None

    decoder = EnvDecoder({})
    assert object_config(decoder, {1: "value"}, "ROUTE") is None

    decoder = EnvDecoder({})
    assert required_string(decoder, {}, "name", "ROUTE.name") is None

    decoder = EnvDecoder({})
    assert required_string(decoder, {"name": 3}, "name", "ROUTE.name") is None

    decoder = EnvDecoder({})
    assert required_string(decoder, {"name": " "}, "name", "ROUTE.name") is None

    decoder = EnvDecoder({})
    assert optional_string(decoder, {"path": 3}, "path", "ROUTE.path") is None

    decoder = EnvDecoder({})
    assert optional_string(decoder, {}, "path", "ROUTE.path") is None

    decoder = EnvDecoder({})
    assert optional_string(decoder, {"path": "${MISSING}"}, "path", "ROUTE.path") == "${MISSING}"

    decoder = EnvDecoder({})
    assert endpoint(decoder, {}, "ROUTE.endpoint_url") is None

    decoder = EnvDecoder({})
    assert addressing_style(decoder, {"addressing_style": 3}, "ROUTE.addressing_style") is None

    decoder = EnvDecoder({})
    assert addressing_style(decoder, {"addressing_style": "bad"}, "ROUTE.addressing_style") is None


@pytest.mark.unit()
def test_env_decoder_preserves_first_issue_when_fail_called_twice() -> None:
    decoder = EnvDecoder({})
    decoder.fail("FIRST", "first failure")
    decoder.fail("SECOND", "second failure")

    with pytest.raises(ConfigError, match="first failure"):
        decoder.finish()


@pytest.mark.unit()
def test_env_decoder_consume_captures_first_parse_issue() -> None:
    decoder = EnvDecoder({})

    assert decoder.consume(ParseResult[str](None, ParseIssue("FIELD", "field failed"))) is None

    with pytest.raises(ConfigError, match="field failed"):
        decoder.finish()
