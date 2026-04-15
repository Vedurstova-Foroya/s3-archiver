#!/usr/bin/env python3
"""Enforce a 100% pyright verifytypes score for both packages."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    packages = ("s3_archiver_core", "s3_archiver_cli")
    for package_name in packages:
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
        if "Type completeness score: 100%" not in result.stdout:
            _ = sys.stderr.write(f"{package_name} did not reach a 100% type completeness score.\n")
            return 1
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
