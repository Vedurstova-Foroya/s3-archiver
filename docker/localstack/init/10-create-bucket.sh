#!/bin/sh
set -eu

awslocal s3api create-bucket --bucket "${TEST_S3_BUCKET}" >/dev/null 2>&1 || true
