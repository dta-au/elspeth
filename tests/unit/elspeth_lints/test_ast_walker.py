"""Tests for shared AST walking and parse-error helpers."""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from elspeth_lints.core.ast_walker import (
    PythonFileReadError,
    iter_python_files,
    parse_python_file,
    walk_function_own_scope,
)


def test_parse_python_file_returns_read_error_for_unicode_decode_error(tmp_path: Path) -> None:
    source_path = tmp_path / "invalid_utf8.py"
    source_path.write_bytes(b"\xff")

    result = parse_python_file(source_path)

    assert isinstance(result, PythonFileReadError)
    assert result.path == source_path
    assert result.error_type == "UnicodeDecodeError"
    assert "could not decode as UTF-8" in result.message
    assert "byte 0" in result.message


def test_parse_python_file_returns_read_error_for_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "locked.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")

    def raise_permission_error(self: Path, *args: object, **kwargs: object) -> str:
        assert self == source_path
        _ = (args, kwargs)
        raise PermissionError("synthetic permission fault")

    monkeypatch.setattr(Path, "read_text", raise_permission_error)

    result = parse_python_file(source_path)

    assert isinstance(result, PythonFileReadError)
    assert result.path == source_path
    assert result.error_type == "PermissionError"
    assert "permission denied:" in result.message
    assert "synthetic permission fault" in result.message


def test_parse_python_file_returns_read_error_for_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "vanished.py"

    def raise_os_error(self: Path, *args: object, **kwargs: object) -> str:
        assert self == source_path
        _ = (args, kwargs)
        raise OSError("synthetic I/O fault")

    monkeypatch.setattr(Path, "read_text", raise_os_error)

    result = parse_python_file(source_path)

    assert isinstance(result, PythonFileReadError)
    assert result.path == source_path
    assert result.error_type == "OSError"
    assert result.message == "OSError: synthetic I/O fault"


def test_iter_python_files_skips_dependency_and_cache_directories(tmp_path: Path) -> None:
    source = tmp_path / "src" / "elspeth" / "good.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    for ignored_dir in (".venv", ".uv-cache", ".worktrees", "node_modules", "build", "dist"):
        ignored = tmp_path / ignored_dir / "dependency.py"
        ignored.parent.mkdir(parents=True)
        ignored.write_text("value = 2\n", encoding="utf-8")

    assert list(iter_python_files(tmp_path)) == [source]


def test_iter_python_files_yields_files_not_directories_named_python(tmp_path: Path) -> None:
    python_named_directory = tmp_path / "archive.py"
    nested_source = python_named_directory / "nested.py"
    nested_source.parent.mkdir()
    nested_source.write_text("value = 1\n", encoding="utf-8")

    assert list(iter_python_files(tmp_path)) == [nested_source]


def test_iter_python_files_prunes_only_nested_agent_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        tmp_path / ".claude" / "tooling.py",
        tmp_path / "src" / "local.py",
        tmp_path / "src" / "worktrees" / "tracked.py",
        tmp_path / "worktrees" / "tracked.py",
    }
    ignored_root = tmp_path / ".claude" / "worktrees"
    candidates = [
        *expected,
        ignored_root / "sibling" / "src" / "foreign.py",
    ]
    for candidate in candidates:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("value = 1\n", encoding="utf-8")

    def reject_post_traversal_rglob(self: Path, pattern: str) -> object:
        raise AssertionError(f"walker used post-traversal rglob({pattern!r}) from {self}")

    monkeypatch.setattr(Path, "rglob", reject_post_traversal_rglob)

    assert set(iter_python_files(tmp_path)) == expected


def test_iter_python_files_skips_unreadable_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "good.py"
    source.write_text("value = 1\n", encoding="utf-8")
    unreadable = tmp_path / "private"
    hidden_source = unreadable / "hidden.py"
    unreadable.mkdir()
    hidden_source.write_text("value = 2\n", encoding="utf-8")
    original_scandir = os.scandir

    def deny_unreadable_directory(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        if Path(path) == unreadable:
            raise PermissionError("synthetic traversal fault")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_unreadable_directory)

    assert list(iter_python_files(tmp_path)) == [source]


@pytest.mark.parametrize(
    "hidden_parent",
    (Path(".worktrees") / "feature", Path(".claude") / "worktrees" / "feature"),
    ids=("legacy-worktree-parent", "agent-worktree-parent"),
)
def test_iter_python_files_allows_roots_inside_hidden_worktree_parent(tmp_path: Path, hidden_parent: Path) -> None:
    source = tmp_path / hidden_parent / "fixtures" / "case" / "good.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    nested_ignored = source.parent / ".worktrees" / "ignored.py"
    nested_ignored.parent.mkdir()
    nested_ignored.write_text("value = 2\n", encoding="utf-8")

    assert list(iter_python_files(source.parent)) == [source]


def test_walk_function_own_scope_keeps_comprehension_nodes_visible() -> None:
    tree = ast.parse("""
def handler(arguments):
    values = [item.get("k") for item in arguments]
    return values
""")
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)

    nodes = list(walk_function_own_scope(func_node))

    assert any(isinstance(node, ast.ListComp) for node in nodes)
    assert any(isinstance(node, ast.comprehension) for node in nodes)
    assert any(isinstance(node, ast.Name) and node.id == "item" and isinstance(node.ctx, ast.Store) for node in nodes)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "item"
        for node in nodes
    )
