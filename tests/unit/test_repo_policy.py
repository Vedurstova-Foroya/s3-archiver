"""Repository policy tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHORED_SOURCE_ROOTS = (
    REPO_ROOT / "packages",
    REPO_ROOT / "tests",
    REPO_ROOT / "scripts",
)
AUTHORED_PYTHON_ROOTS = AUTHORED_SOURCE_ROOTS


def _authored_python_files() -> list[Path]:
    return [python_file for root in AUTHORED_PYTHON_ROOTS for python_file in root.rglob("*.py")]


def _authored_source_files() -> list[Path]:
    source_files = [
        file_path
        for root in AUTHORED_SOURCE_ROOTS
        for pattern in ("*.py", "*.sh")
        for file_path in root.rglob(pattern)
    ]
    return sorted(source_files)


@pytest.mark.unit()
def test_authored_source_files_do_not_exceed_300_lines() -> None:
    for source_file in _authored_source_files():
        line_count = len(source_file.read_text(encoding="utf-8").splitlines())
        assert line_count <= 300, f"{source_file} exceeds 300 lines with {line_count}"


@pytest.mark.unit()
def test_authored_python_files_do_not_use_any_annotations() -> None:
    for python_file in _authored_python_files():
        module = ast.parse(python_file.read_text(encoding="utf-8"))
        assert _contains_any_reference(module) is False, f"{python_file} uses typing.Any"


@pytest.mark.unit()
def test_authored_python_functions_are_fully_annotated() -> None:
    for python_file in _authored_python_files():
        module = ast.parse(python_file.read_text(encoding="utf-8"))
        missing_annotations = _missing_annotations(module)
        assert missing_annotations == [], (
            f"{python_file} has untyped definitions: {', '.join(missing_annotations)}"
        )


def _contains_any_reference(module: ast.AST) -> bool:
    typing_aliases = _typing_aliases(module)
    return any(_node_uses_any(node, typing_aliases) for node in ast.walk(module))


def _typing_aliases(module: ast.AST) -> set[str]:
    aliases: set[str] = {"typing", "typing_extensions"}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"typing", "typing_extensions"}:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _node_uses_any(node: ast.AST, typing_aliases: set[str]) -> bool:
    if isinstance(node, ast.ImportFrom) and node.module in {"typing", "typing_extensions"}:
        return any(alias.name == "Any" for alias in node.names)
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id in typing_aliases
            and node.attr == "Any"
        )
    if isinstance(node, ast.Name):
        return node.id == "Any"
    return False


def _missing_annotations(module: ast.Module) -> list[str]:
    missing: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.returns is None:
            missing.append(node.name)
            continue
        if any(_argument_requires_annotation(argument) for argument in _iter_arguments(node)):
            missing.append(node.name)
    return missing


def _iter_arguments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    arguments = [
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
    ]
    if node.args.vararg is not None:
        arguments.append(node.args.vararg)
    if node.args.kwarg is not None:
        arguments.append(node.args.kwarg)
    return arguments


def _argument_requires_annotation(argument: ast.arg) -> bool:
    if argument.arg in {"self", "cls"}:
        return False
    return argument.annotation is None
