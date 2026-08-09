"""Tests for the key-free tier-model rejudge helper."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_ROOT = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS_ROOT))

import codex_tier_model_rejudge as rejudge  # type: ignore[import-not-found]  # noqa: E402


def test_scope_index_selects_the_innermost_nested_scope(tmp_path: Path) -> None:
    source = tmp_path / "nested.py"
    source.write_text(
        "\n".join(
            [
                "def outer():",
                "    def inner():",
                "        return 1",
                "    return inner()",
                "",
            ]
        )
    )

    index = rejudge._ScopeIndex(tmp_path)

    assert index.symbol("nested.py", 3) == "outer:inner"
    assert index.symbol("nested.py", 4) == "outer"


def test_scope_index_falls_back_to_lineno_when_ast_end_lineno_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("def placeholder():\n    pass\n")
    function = ast.FunctionDef(
        name="synthetic",
        args=ast.arguments(
            posonlyargs=[],
            args=[],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[ast.Pass(lineno=7, col_offset=4)],
        decorator_list=[],
        lineno=7,
        col_offset=0,
        end_lineno=None,
        end_col_offset=None,
    )
    tree = ast.Module(body=[function], type_ignores=[])
    monkeypatch.setattr(
        rejudge,
        "ast",
        SimpleNamespace(
            AST=ast.AST,
            AsyncFunctionDef=ast.AsyncFunctionDef,
            ClassDef=ast.ClassDef,
            FunctionDef=ast.FunctionDef,
            iter_child_nodes=ast.iter_child_nodes,
            parse=lambda _source: tree,
        ),
    )

    index = rejudge._ScopeIndex(tmp_path)

    assert index.symbol("synthetic.py", 7) == "synthetic"
