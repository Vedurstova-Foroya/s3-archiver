#!/usr/bin/env bash
set -euo pipefail

env_file="${APP_ENV_FILE:-${ENV_FILE:-.env}}"
uv_bin="${UV:-uv}"
command="${1:-archive}"

if [[ $# -gt 0 ]]; then
  shift
fi

if [[ ! -f "${env_file}" ]]; then
  echo "missing env file: ${env_file}" >&2
  exit 1
fi

if [[ "${command}" != "archive" && "${command}" != "schedule" && "${command}" != "cleanup" ]]; then
  echo "unsupported s3-archiver command: ${command}" >&2
  exit 1
fi

export APP_ENV_FILE="${env_file}"
exec "${uv_bin}" run s3-archiver "${command}" "$@"
