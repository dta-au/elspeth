"""Regression tests for the repository-local pre-commit dispatcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DISPATCHER = PROJECT_ROOT / "scripts" / "git-hooks" / "pre-commit-dispatcher.sh"
SECRET_SCAN = PROJECT_ROOT / "scripts" / "git-hooks" / "pre-commit-secret-scan.sh"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=False, capture_output=True, text=True)


def test_deletion_only_index_still_runs_always_run_secret_scan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    hook_dir = repo / "scripts" / "git-hooks"
    hook_dir.mkdir(parents=True)
    shutil.copy2(DISPATCHER, hook_dir / DISPATCHER.name)
    shutil.copy2(SECRET_SCAN, hook_dir / SECRET_SCAN.name)
    (hook_dir / DISPATCHER.name).chmod(0o755)
    (hook_dir / SECRET_SCAN.name).chmod(0o755)
    (repo / ".pre-commit-config.yaml").write_text(
        """\
repos:
  - repo: local
    hooks:
      - id: secret-scan
        name: Secret-pattern scan
        entry: scripts/git-hooks/pre-commit-secret-scan.sh
        language: system
        pass_filenames: false
        always_run: true
      - id: ordinary-text
        name: Ordinary text hook
        entry: "false"
        language: system
        files: \\.txt$
      - id: no-stash-sentinel
        name: No-stash sentinel
        entry: grep -q working-tree-value sentinel.txt
        language: system
        pass_filenames: false
        always_run: true
""",
        encoding="utf-8",
    )

    assert _run(["git", "init", "--quiet"], cwd=repo).returncode == 0
    assert _run(["git", "config", "user.name", "ELSPETH Test"], cwd=repo).returncode == 0
    assert _run(["git", "config", "user.email", "elspeth-test@example.invalid"], cwd=repo).returncode == 0
    deleted = repo / "deleted.txt"
    deleted.write_text("tracked before deletion\n", encoding="utf-8")
    sentinel = repo / "sentinel.txt"
    sentinel.write_text("committed-value\n", encoding="utf-8")
    assert _run(["git", "add", ".pre-commit-config.yaml", "scripts", "deleted.txt", "sentinel.txt"], cwd=repo).returncode == 0
    assert _run(["git", "commit", "--quiet", "--no-verify", "-m", "seed"], cwd=repo).returncode == 0
    assert _run(["git", "rm", "--cached", "--quiet", "--", "deleted.txt"], cwd=repo).returncode == 0
    sentinel.write_text("working-tree-value\n", encoding="utf-8")
    staged = _run(["git", "diff", "--cached", "--name-status"], cwd=repo)
    assert staged.returncode == 0
    assert staged.stdout == "D\tdeleted.txt\n"

    env = {
        **os.environ,
        "ELSPETH_PRE_COMMIT_PYTHON": sys.executable,
        "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-home"),
    }
    env.pop("SKIP", None)
    result = _run([str(hook_dir / DISPATCHER.name)], cwd=repo, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Secret-pattern scan" in result.stdout + result.stderr
    assert "Ordinary text hook" in result.stdout + result.stderr
    assert "No-stash sentinel" in result.stdout + result.stderr
    assert "Passed" in result.stdout + result.stderr
    assert "Unstaged files detected" not in result.stdout + result.stderr
