"""Repository policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest

PACKAGE_SRC = Path(__file__).resolve().parents[2] / "packages"


@pytest.mark.unit()
def test_authored_python_files_do_not_exceed_300_lines() -> None:
    for python_file in PACKAGE_SRC.rglob("*.py"):
        line_count = len(python_file.read_text(encoding="utf-8").splitlines())
        assert line_count <= 300, f"{python_file} exceeds 300 lines with {line_count}"
