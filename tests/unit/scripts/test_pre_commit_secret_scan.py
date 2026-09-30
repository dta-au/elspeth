"""Regression tests for staged-byte authority in the secret scanner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_SCAN = PROJECT_ROOT / "scripts" / "git-hooks" / "pre-commit-secret-scan.sh"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    hook_dir = repository / "scripts" / "git-hooks"
    hook_dir.mkdir(parents=True)
    scanner = hook_dir / SECRET_SCAN.name
    shutil.copy2(SECRET_SCAN, scanner)
    scanner.chmod(0o755)
    assert _run(["git", "init", "--quiet"], cwd=repository).returncode == 0
    assert _run(["git", "config", "user.name", "ELSPETH Test"], cwd=repository).returncode == 0
    assert _run(["git", "config", "user.email", "elspeth-test@example.invalid"], cwd=repository).returncode == 0
    return repository, scanner


def _commit(repository: Path, path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    assert _run(["git", "add", "--", path.name], cwd=repository).returncode == 0
    assert _run(["git", "commit", "--quiet", "--no-verify", "-m", "seed"], cwd=repository).returncode == 0


def test_scanner_rejects_secret_in_index_when_working_tree_is_safe(tmp_path: Path) -> None:
    repository, scanner = _repository(tmp_path)
    candidate = repository / "candidate.txt"
    _commit(repository, candidate, "original\n")
    fake_token = "sk-" + "A" * 40
    candidate.write_text(f"api_key = {fake_token}\n", encoding="utf-8")
    assert _run(["git", "add", "--", candidate.name], cwd=repository).returncode == 0
    candidate.write_text("safe working-tree replacement\n", encoding="utf-8")

    assert _run(["git", "status", "--short", "--", candidate.name], cwd=repository).stdout == "MM candidate.txt\n"
    assert fake_token in _run(["git", "show", f":{candidate.name}"], cwd=repository).stdout
    assert fake_token not in candidate.read_text(encoding="utf-8")

    result = _run([str(scanner)], cwd=repository)

    assert result.returncode == 1
    assert "candidate.txt:1" in result.stderr
    assert fake_token in result.stderr


def test_scanner_ignores_unstaged_secret_when_index_is_safe(tmp_path: Path) -> None:
    repository, scanner = _repository(tmp_path)
    candidate = repository / "candidate.txt"
    _commit(repository, candidate, "original\n")
    candidate.write_text("safe staged replacement\n", encoding="utf-8")
    assert _run(["git", "add", "--", candidate.name], cwd=repository).returncode == 0
    fake_token = "sk-" + "B" * 40
    candidate.write_text(f"api_key = {fake_token}\n", encoding="utf-8")

    assert _run(["git", "status", "--short", "--", candidate.name], cwd=repository).stdout == "MM candidate.txt\n"
    assert fake_token not in _run(["git", "show", f":{candidate.name}"], cwd=repository).stdout
    assert fake_token in candidate.read_text(encoding="utf-8")

    result = _run([str(scanner)], cwd=repository)

    assert result.returncode == 0, result.stdout + result.stderr
