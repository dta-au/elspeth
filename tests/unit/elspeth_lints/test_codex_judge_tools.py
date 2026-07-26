"""Security-boundary tests for the Codex judge's sealed MCP reader."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth_lints.core.judge import AgentToolScope
from elspeth_lints.mcp.codex_judge_tools import (
    _glob_files,
    _grep_files,
    _read_file,
)


@pytest.fixture
def scope(tmp_path: Path) -> AgentToolScope:
    source = tmp_path / "src"
    allowlists = tmp_path / "allowlists"
    source.mkdir()
    allowlists.mkdir()
    return AgentToolScope(
        allowed_roots=(source.resolve(), allowlists.resolve()),
        cwd=source.resolve(),
        max_turns=4,
    )


def test_read_file_returns_bounded_numbered_source(scope: AgentToolScope) -> None:
    target = scope.cwd / "example.py"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = _read_file(
        scope,
        {"file_path": str(target), "start_line": 2, "line_count": 2},
    )

    assert result == "2: two\n3: three"


def test_read_file_denies_out_of_scope_path(scope: AgentToolScope, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the permitted roots"):
        _read_file(scope, {"file_path": str(outside)})


def test_read_and_grep_skip_secret_scrubbed_source(scope: AgentToolScope) -> None:
    safe = scope.cwd / "safe.py"
    secret = scope.cwd / "secret.py"
    safe.write_text("def public_boundary():\n    pass\n", encoding="utf-8")
    secret.write_text('API_KEY = "sk-' + ("A" * 48) + '"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="secret scrubber"):
        _read_file(scope, {"file_path": str(secret)})

    # Non-content Grep must not become an adaptive oracle over a file that the
    # same boundary refuses to Read.
    result = _grep_files(
        scope,
        {
            "pattern": "sk-",
            "path": str(scope.cwd),
            "glob": "**/*.py",
            "output_mode": "count",
        },
    )
    assert '"count": 0' in result


def test_glob_rejects_parent_escape(scope: AgentToolScope) -> None:
    with pytest.raises(ValueError, match="may not contain"):
        _glob_files(scope, {"pattern": "../*"})
