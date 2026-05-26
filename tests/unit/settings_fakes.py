"""Shared settings test fixtures."""

from __future__ import annotations

from pathlib import Path


def dual_env(tmp_path: Path) -> dict[str, str]:
    """Return a complete dual S3 environment for settings tests."""

    return {
        "S3_SOURCE_PROVIDER": "oci",
        "S3_SOURCE_ACCESS_KEY": "source-access",
        "S3_SOURCE_SECRET_KEY": "source-secret",
        "S3_SOURCE_REGION": "eu-frankfurt-1",
        "S3_SOURCE_NAMESPACE": "tenant",
        "S3_SOURCE_BUCKET": "source-bucket",
        "S3_SOURCE_IAM_USER_OCID": "ocid1.user.oc1..source",
        "S3_SOURCE_ENDPOINT": (
            "https://tenant.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"
        ),
        "S3_SOURCE_ADDRESSING_STYLE": "path",
        "S3_DESTINATION_PROVIDER": "localstack",
        "S3_DESTINATION_ACCESS_KEY": "destination-access",
        "S3_DESTINATION_SECRET_KEY": "destination-secret",
        "S3_DESTINATION_REGION": "us-east-1",
        "S3_DESTINATION_BUCKET": "destination-bucket",
        "S3_DESTINATION_ENDPOINT": "http://localstack:4566",
        "S3_DESTINATION_ADDRESSING_STYLE": "path",
        "ARCHIVER_CONFIG_JSON": (
            '[{"name":"default","parser":"filename_timestamp","copy_mode":"daily_tar_gz",'
            '"source":{"provider":"${S3_SOURCE_PROVIDER}",'
            '"endpoint_url":"${S3_SOURCE_ENDPOINT}",'
            '"region":"${S3_SOURCE_REGION}","namespace":"${S3_SOURCE_NAMESPACE}",'
            '"bucket":"${S3_SOURCE_BUCKET}","iam_user_ocid":"${S3_SOURCE_IAM_USER_OCID}",'
            '"path":"","access_key_id":"${S3_SOURCE_ACCESS_KEY}",'
            '"secret_access_key":"${S3_SOURCE_SECRET_KEY}",'
            '"addressing_style":"${S3_SOURCE_ADDRESSING_STYLE}"},'
            '"destination":{"provider":"${S3_DESTINATION_PROVIDER}",'
            '"endpoint_url":"${S3_DESTINATION_ENDPOINT}",'
            '"region":"${S3_DESTINATION_REGION}","bucket":"${S3_DESTINATION_BUCKET}",'
            '"path":"",'
            '"access_key_id":"${S3_DESTINATION_ACCESS_KEY}",'
            '"secret_access_key":"${S3_DESTINATION_SECRET_KEY}",'
            '"addressing_style":"${S3_DESTINATION_ADDRESSING_STYLE}"}}]'
        ),
        "LOG_LEVEL": "INFO",
        "LOG_DIR": str(tmp_path / "logs"),
    }
