#!/usr/bin/env python3
"""Enforce a 100% pyright verifytypes score for both packages."""

from __future__ import annotations

import re
import subprocess
import sys

_PACKAGES = ("s3_archiver_core", "s3_archiver_cli")
_TYPE_COMPLETENESS_TARGET = "Type completeness score: 100%"
_WARNING_COUNT_PATTERN = re.compile(r"^\s{2}[^:]+:\s+([1-9]\d*)$")


def main() -> int:
    for package_name in _PACKAGES:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pyright",
                "--verifytypes",
                package_name,
                "--ignoreexternal",
                "--warnings",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        _ = sys.stdout.write(result.stdout)
        _ = sys.stderr.write(result.stderr)
        if _TYPE_COMPLETENESS_TARGET not in result.stdout:
            _ = sys.stderr.write(f"{package_name} did not reach a 100% type completeness score.\n")
            return 1
        if _has_verifytypes_warnings(result.stdout):
            _ = sys.stderr.write(f"{package_name} emitted verifytypes warnings.\n")
            return 1
        if result.returncode != 0:
            return result.returncode
    return 0


def _has_verifytypes_warnings(output: str) -> bool:
    lines = output.splitlines()
    try:
        start_index = lines.index("Symbols without documentation:") + 1
    except ValueError:
        return False
    for line in lines[start_index:]:
        if line == "":
            break
        if _WARNING_COUNT_PATTERN.match(line) is not None:
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
