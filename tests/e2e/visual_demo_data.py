"""Shared production-shaped data fixtures for visual demo e2e tests."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, date, datetime, timedelta

from s3_archiver_core.archive_tar import ORIGINAL_KEY_PAX_HEADER
from s3_archiver_core.s3 import S3Client

from tests.integration.localstack_object_helpers import listed_keys, put_test_object

DEMO_RETENTION_DAYS = 60
DEMO_ARCHIVE_DAY_COUNT = 365
DEMO_ARCHIVE_ROOT_COUNT = 12
DEMO_FILES_PER_PATH_DAY = 2
DEMO_SKIPPED_OBJECT_COUNT = 4
DEMO_ARCHIVE_COUNT = DEMO_ARCHIVE_DAY_COUNT * DEMO_ARCHIVE_ROOT_COUNT
DEMO_ARCHIVED_OBJECT_COUNT = DEMO_ARCHIVE_COUNT * DEMO_FILES_PER_PATH_DAY
DEMO_SEEDED_OBJECT_COUNT = DEMO_ARCHIVED_OBJECT_COUNT + DEMO_SKIPPED_OBJECT_COUNT


def seed_daily_demo_objects(
    client: S3Client,
    bucket: str,
    *,
    prefix: str,
    seed_now: datetime,
) -> None:
    seeded_now = seed_now.astimezone(UTC).replace(microsecond=0)
    target_day = archive_demo_days(seeded_now)[0]
    metadata_by_key: dict[str, dict[str, str]] = {}
    keys_by_age = _archive_demo_keys_by_age(prefix, seeded_now) | {
        DEMO_RETENTION_DAYS - 1: retained_demo_keys(prefix, target_day),
        0: invalid_demo_keys(prefix, target_day),
    }
    for age_days, keys in keys_by_age.items():
        target = seeded_now - timedelta(days=age_days)
        for key in keys:
            metadata = {
                "s3-archiver-test-age-days": str(age_days),
                "s3-archiver-test-last-modified": target.isoformat(),
            }
            metadata_by_key[key] = metadata
            _ = put_test_object(
                client,
                bucket,
                key,
                metadata=metadata,
            )
    _verify_seeded_keys(client, bucket, metadata_by_key)


def archive_demo_days(seed_now: datetime) -> tuple[date, ...]:
    target_day = seed_now.astimezone(UTC).date() - timedelta(days=DEMO_RETENTION_DAYS)
    return tuple(target_day - timedelta(days=offset) for offset in range(DEMO_ARCHIVE_DAY_COUNT))


def target_day_demo_cases(prefix: str, target_day: date) -> tuple[tuple[str, str], ...]:
    day = target_day.isoformat()
    compact = target_day.strftime("%Y%m%d")
    path_day = target_day.strftime("%Y/%m/%d")
    underscore = target_day.strftime("%Y_%m_%d")
    return (
        (f"{prefix}/fae", f"{prefix}/fae/{path_day}/07/{day}T07-00-00Z.xml"),
        (f"{prefix}/fae", f"{prefix}/fae/{path_day}/08/{day}T08-15-00Z.xml"),
        (
            f"{prefix}/harmonie",
            f"{prefix}/harmonie/HARMONIE_DINI_SF_{day}T000000Z_{day}T000000Z.bz2",
        ),
        (f"{prefix}/harmonie", f"{prefix}/harmonie/HARMONIE_DINI_SF_{day}T060000Z.bz2"),
        (f"{prefix}/metar", f"{prefix}/metar/{day}/METAR_{compact}120000Z.json"),
        (f"{prefix}/metar", f"{prefix}/metar/{day}/METAR_{compact}121500Z.json"),
        (f"{prefix}/radar", f"{prefix}/radar/{path_day}/radar_{compact}-130000.bin"),
        (f"{prefix}/radar", f"{prefix}/radar/{path_day}/radar_{compact}-131500.bin"),
        (f"{prefix}/satellite/flat", f"{prefix}/satellite/flat/sat_{day}T14:30:00Z.png"),
        (f"{prefix}/satellite/flat", f"{prefix}/satellite/flat/sat_{day}T14:45:00Z.png"),
        (f"{prefix}/observations", f"{prefix}/observations/obs_{underscore}_153000.txt"),
        (f"{prefix}/observations", f"{prefix}/observations/obs_{underscore}_154500.txt"),
        (f"{prefix}/models", f"{prefix}/models/{path_day}/model_{day}T16:45:00+00:00.grib"),
        (f"{prefix}/models", f"{prefix}/models/{path_day}/model_{day}T17:45:00+00:00.grib"),
        (f"{prefix}/lightning", f"{prefix}/lightning/{day}T17-00-00Z/lightning-latest.csv"),
        (f"{prefix}/lightning", f"{prefix}/lightning/{day}T17-05-00Z/lightning-batch.csv"),
        (f"{prefix}/ocean", f"{prefix}/ocean/{path_day}/wave_{day}T18:15:00+0100.nc"),
        (f"{prefix}/ocean", f"{prefix}/ocean/{path_day}/wave_{day}T21:15:00+0000.nc"),
        (f"{prefix}/climate", f"{prefix}/climate/{compact}/climate-summary.txt"),
        (f"{prefix}/climate", f"{prefix}/climate/{compact}/climate-hourly.txt"),
        ("C:/compose-demo/unsafe-drive", f"C:/compose-demo/unsafe-drive/{day}T19-00-00Z.txt"),
        ("C:/compose-demo/unsafe-drive", f"C:/compose-demo/unsafe-drive/{day}T19-15-00Z.txt"),
        (
            f"s3-archiver-safe/{prefix}/reserved",
            f"s3-archiver-safe/{prefix}/reserved/{day}T20-00-00Z.txt",
        ),
        (
            f"s3-archiver-safe/{prefix}/reserved",
            f"s3-archiver-safe/{prefix}/reserved/{day}T20-15-00Z.txt",
        ),
    )


def retained_demo_keys(prefix: str, target_day: date) -> tuple[str, str]:
    next_day = target_day + timedelta(days=1)
    later_day = target_day + timedelta(days=2)
    return (
        f"{prefix}/fae/{next_day:%Y/%m/%d}/00/{next_day.isoformat()}T00-00-00Z.xml",
        f"{prefix}/harmonie/HARMONIE_DINI_SF_{later_day.isoformat()}T230000Z.bz2",
    )


def invalid_demo_keys(prefix: str, target_day: date) -> tuple[str, str]:
    return (
        f"{prefix}/invalid/no-timestamp-latest.txt",
        f"{prefix}/invalid/{target_day.isoformat()}T99-00-00Z.txt",
    )


def expected_archive_members(prefix: str, archive_days: tuple[date, ...]) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for target_day in archive_days:
        for root, key in target_day_demo_cases(prefix, target_day):
            members.setdefault(f"{root}/{target_day.isoformat()}.tar.gz", set()).add(key)
    return members


def archive_member_name(key: str) -> str:
    if key.startswith(("C:", "s3-archiver-safe/")):
        return f"s3-archiver-safe/{hashlib.sha256(key.encode()).hexdigest()}"
    return key


def sampled_archive_members(archive_members: dict[str, set[str]]) -> dict[str, set[str]]:
    keys = sorted(archive_members)
    return {key: archive_members[key] for key in (keys[0], keys[len(keys) // 2], keys[-1])}


def expected_pax_headers(source_keys: set[str]) -> dict[str, dict[str, str]]:
    return {
        archive_member_name(key): {ORIGINAL_KEY_PAX_HEADER: key}
        for key in source_keys
        if archive_member_name(key) != key
    }


def _verify_seeded_keys(
    client: S3Client, bucket: str, metadata_by_key: dict[str, dict[str, str]]
) -> None:
    expected = set(metadata_by_key)
    for _ in range(30):
        missing = expected - listed_keys(client, bucket)
        if not missing:
            return
        for key in sorted(missing):
            _ = put_test_object(client, bucket, key, metadata=metadata_by_key[key])
        time.sleep(1.0)
    missing = sorted(expected - listed_keys(client, bucket))
    sample = [*missing[:3], "...", *missing[-3:]] if len(missing) > 6 else missing
    raise AssertionError(f"LocalStack did not list {len(missing)} seeded demo objects: {sample}")


def _archive_demo_keys_by_age(prefix: str, seed_now: datetime) -> dict[int, tuple[str, ...]]:
    target_day = seed_now.astimezone(UTC).date() - timedelta(days=DEMO_RETENTION_DAYS)
    return {
        DEMO_RETENTION_DAYS + offset: tuple(
            key for _, key in target_day_demo_cases(prefix, target_day - timedelta(days=offset))
        )
        for offset in range(DEMO_ARCHIVE_DAY_COUNT)
    }
