"""Security-boundary tests for the Codex judge's sealed MCP reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import elspeth_lints.mcp.codex_judge_tools as judge_tools
from elspeth_lints.core.judge import AgentToolScope, build_readonly_tool_scope
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


def test_read_and_grep_see_scrubbed_source_not_raw_bytes(scope: AgentToolScope) -> None:
    safe = scope.cwd / "safe.py"
    secret = scope.cwd / "secret.py"
    safe.write_text("def public_boundary():\n    pass\n", encoding="utf-8")
    secret.write_text('def before():\n    pass\nAPI_KEY = "sk-' + ("A" * 48) + '"\ndef after():\n    pass\n', encoding="utf-8")

    # The file stays readable — a redaction-bearing file must not go dark to
    # the judge — but the matched line comes back as its marker, never as the
    # raw bytes, and the surrounding lines keep their numbers.
    rendered = _read_file(scope, {"file_path": str(secret)})
    assert "sk-" not in rendered
    assert "AAAA" not in rendered
    assert "3: [REDACTED-SECRET-" in rendered
    assert "1: def before():" in rendered
    assert "4: def after():" in rendered

    # Non-content Grep counts over the same scrubbed text, so it cannot become
    # an adaptive oracle over the redacted bytes.
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


def test_whole_checkout_search_reaches_evidence_without_searching_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "src" / "elspeth"
    source.mkdir(parents=True)
    scope = build_readonly_tool_scope(root=source, allowlist_dir=tmp_path / "config")
    evidence = (
        "src/elspeth/helper.py",
        "tests/unit/test_helper.py",
        "docs/control.md",
        "scripts/verify.py",
        "config/control.yaml",
        ".github/workflows/ci.yaml",
        ".agents/skills/review/SKILL.md",
        "README.md",
    )
    artifacts = (
        ".git/config",
        ".venv/vendor.py",
        "node_modules/pkg/index.js",
        ".claude/worktrees/old/tests/test_old.py",
        ".elspeth/staged-reviews/old.json",
        "config/.sign-bundle-transactions/old/candidate.yaml",
    )
    for name in (*evidence, *artifacts):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("control_evidence\n", encoding="utf-8")
    expected = sorted(str(tmp_path / name) for name in evidence)
    result = json.loads(_grep_files(scope, {"pattern": "control_evidence", "output_mode": "files_with_matches"}))
    assert result["files"] == expected
    assert result["scanned_files"] == len(evidence)
    assert result["truncated"] is False
    assert json.loads(_glob_files(scope, {"pattern": "**/*"}))["files"] == expected
    assert json.loads(_glob_files(scope, {"pattern": "tests/**/*.py"}))["files"] == [str(tmp_path / "tests/unit/test_helper.py")]
    assert json.loads(_glob_files(scope, {"pattern": "*.md"}))["files"] == [str(tmp_path / "README.md")]
    assert json.loads(_glob_files(scope, {"pattern": "**/helper.py"}))["files"] == [str(source / "helper.py")]
    assert json.loads(_glob_files(scope, {"pattern": "*.py", "path": "src/elspeth"}))["files"] == [str(source / "helper.py")]
    assert json.loads(_glob_files(scope, {"pattern": "**/*.py", "path": "src/elspeth"}))["files"] == [str(source / "helper.py")]
    for name in evidence:
        assert _read_file(scope, {"file_path": name}) == "1: control_evidence"


def test_whole_checkout_reader_keeps_secret_and_escape_guards(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "src" / "elspeth"
    source.mkdir(parents=True)
    scope = build_readonly_tool_scope(root=source, allowlist_dir=repo / "config")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside_evidence\n")
    (repo / "escape.txt").symlink_to(outside)
    (repo / "linked_directory").symlink_to(tmp_path, target_is_directory=True)
    for path in ("escape.txt", "linked_directory/outside.txt", "../outside.txt", ".env", ".env.local", ".git/config"):
        with pytest.raises(ValueError):
            _read_file(scope, {"file_path": path})
    assert json.loads(_grep_files(scope, {"pattern": "outside_evidence", "output_mode": "count"}))["count"] == 0
    assert json.loads(_glob_files(scope, {"pattern": "**/*"}))["files"] == []


def test_tooling_artifacts_cannot_spend_the_codebase_search_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "src" / "elspeth"
    source.mkdir(parents=True)
    scope = build_readonly_tool_scope(root=source, allowlist_dir=tmp_path / "config")
    artifacts = (
        ".mypy_cache",
        ".uv-cache",
        ".hypothesis",
        ".pytest_cache",
        ".ruff_cache",
        ".e2e-data",
        ".scratch",
        ".weft",
        ".benchmarks",
        ".playwright-cli",
        ".playwright-mcp",
    )
    for directory in artifacts:
        artifact = tmp_path / directory / "generated.txt"
        artifact.parent.mkdir()
        artifact.write_text("generated runtime evidence\n")
    evidence = (
        ".github/workflows/ci.yaml",
        ".agents/skills/review/SKILL.md",
        ".githooks/pre-commit",
        ".claude/commands/review.md",
        "src/elspeth/control.py",
        "tests/unit/test_control.py",
    )
    for name in evidence:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("current code evidence\n")
    monkeypatch.setattr(judge_tools, "_MAX_SCANNED_FILES", len(evidence) + 1)
    result = json.loads(_grep_files(scope, {"pattern": "current code evidence", "output_mode": "files_with_matches"}))
    assert result["files"] == sorted(str(tmp_path / name) for name in evidence)
    assert result["scanned_files"] == len(evidence)
    assert result["truncated"] is False
    # Artifact exclusion is a search default, not a new read prohibition.
    assert _read_file(scope, {"file_path": ".scratch/generated.txt"}) == "1: generated runtime evidence"
