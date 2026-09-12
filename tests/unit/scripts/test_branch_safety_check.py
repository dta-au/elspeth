"""Exercise merge completion preflight against actual Git index states."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "branch-safety-check.sh"


def _git(repo: Path, *args: str, expected: int = 0) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    assert result.returncode == expected, result.stdout + result.stderr
    return result.stdout


@pytest.fixture
def merging_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=task")
    _git(repo, "config", "user.name", "Preflight Test")
    _git(repo, "config", "user.email", "preflight@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    for source, package in (("src", "elspeth"), ("elspeth-lints/src", "elspeth_lints")):
        package_dir = repo / source / package
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    tracked = repo / "conflict.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "checkout", "--quiet", "-b", "other")
    tracked.write_text("other\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "other")
    _git(repo, "checkout", "--quiet", "task")
    tracked.write_text("task\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "task")
    _git(repo, "-c", "rerere.enabled=false", "merge", "--no-commit", "other", expected=1)
    assert (repo / ".git/MERGE_HEAD").is_file()
    assert _git(repo, "ls-files", "--unmerged")
    return repo


def _resolve(repo: Path) -> None:
    (repo / "conflict.txt").write_text("resolved\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    assert _git(repo, "ls-files", "--unmerged") == ""


def _preflight(repo: Path, intent: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), "--intent", intent], cwd=repo, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("intent", ["commit", "merge", "rebase", "push"])
def test_unresolved_merge_refused(merging_repo: Path, intent: str) -> None:
    result = _preflight(merging_repo, intent)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] in-progress" in result.stdout
    assert "unmerged index entries remain" in result.stdout


@pytest.mark.parametrize("intent", ["commit", "merge", "rebase", "push"])
def test_resolved_merge_only_allows_completion(merging_repo: Path, intent: str) -> None:
    _resolve(merging_repo)
    result = _preflight(merging_repo, intent)
    assert result.returncode == (0 if intent == "commit" else 1), result.stdout + result.stderr
    if intent == "commit":
        assert "[PASS] in-progress" in result.stdout
        assert "resolved merge ready for completion" in result.stdout
    else:
        assert "[FAIL] in-progress" in result.stdout
        assert "complete the merge before" in result.stdout


@pytest.mark.parametrize("operation", ["CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply", "sequencer"])
def test_resolved_merge_does_not_hide_another_operation(merging_repo: Path, operation: str) -> None:
    _resolve(merging_repo)
    marker = merging_repo / ".git" / operation
    if operation in {"rebase-merge", "rebase-apply", "sequencer"}:
        marker.mkdir()
    else:
        marker.write_text(_git(merging_repo, "rev-parse", "HEAD"), encoding="utf-8")
    result = _preflight(merging_repo, "commit")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] in-progress" in result.stdout
    assert f"unfinished: {operation}" in result.stdout


def test_unmerged_index_without_merge_head_still_refused(merging_repo: Path) -> None:
    (merging_repo / ".git/MERGE_HEAD").unlink()
    result = _preflight(merging_repo, "commit")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unmerged index entries remain" in result.stdout


def test_ordinary_commit_remains_allowed(merging_repo: Path) -> None:
    _git(merging_repo, "merge", "--abort")
    result = _preflight(merging_repo, "commit")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] in-progress" in result.stdout
