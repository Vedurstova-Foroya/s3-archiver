"""Tests for the verifytypes coverage gate."""

from __future__ import annotations

import importlib.util
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol, cast

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_type_coverage.py"


class CoverageScriptModule(Protocol):
    """Typed view of the verifytypes gate script module."""

    __dict__: dict[str, object]

    def main(self) -> int:
        """Run the verifytypes gate."""
        ...


def _load_coverage_script() -> CoverageScriptModule:
    spec = importlib.util.spec_from_file_location("check_type_coverage", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(CoverageScriptModule, cast(object, module))


coverage_script = _load_coverage_script()
HAS_VERIFYTYPES_WARNINGS = cast(
    Callable[[str], bool],
    coverage_script.__dict__["_has_verifytypes_warnings"],
)


@pytest.mark.unit()
def test_has_verifytypes_warnings_detects_documentation_counts() -> None:
    output = "\n".join(
        (
            'Module name: "s3_archiver_core"',
            "",
            "Symbols without documentation:",
            "  Functions without docstring: 1",
            "  Functions without default param: 0",
            "  Classes without docstring: 0",
            "",
            "Type completeness score: 100%",
        )
    )

    assert HAS_VERIFYTYPES_WARNINGS(output) is True


@pytest.mark.unit()
def test_has_verifytypes_warnings_ignores_clean_output() -> None:
    output = "\n".join(
        (
            'Module name: "s3_archiver_cli"',
            "",
            "Symbols without documentation:",
            "  Functions without docstring: 0",
            "  Functions without default param: 0",
            "  Classes without docstring: 0",
            "",
            "Type completeness score: 100%",
        )
    )

    assert HAS_VERIFYTYPES_WARNINGS(output) is False


@pytest.mark.unit()
def test_main_fails_when_verifytypes_emits_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = {
        "s3_archiver_core": "\n".join(
            (
                "Symbols without documentation:",
                "  Functions without docstring: 1",
                "  Functions without default param: 0",
                "  Classes without docstring: 0",
                "",
                "Type completeness score: 100%",
            )
        ),
        "s3_archiver_cli": "\n".join(
            (
                "Symbols without documentation:",
                "  Functions without docstring: 0",
                "  Functions without default param: 0",
                "  Classes without docstring: 0",
                "",
                "Type completeness score: 100%",
            )
        ),
    }

    def fake_run(
        cmd: Sequence[str], *, check: bool, capture_output: bool, text: bool
    ) -> CompletedProcess[str]:
        _ = (check, capture_output, text)
        package_name = cmd[4]
        return CompletedProcess(cmd, 0, stdout=outputs[package_name], stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert coverage_script.main() == 1


@pytest.mark.unit()
def test_main_passes_when_verifytypes_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_output = "\n".join(
        (
            "Symbols without documentation:",
            "  Functions without docstring: 0",
            "  Functions without default param: 0",
            "  Classes without docstring: 0",
            "",
            "Type completeness score: 100%",
        )
    )

    def fake_run(
        cmd: Sequence[str], *, check: bool, capture_output: bool, text: bool
    ) -> CompletedProcess[str]:
        _ = (check, capture_output, text)
        return CompletedProcess(cmd, 0, stdout=clean_output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert coverage_script.main() == 0
