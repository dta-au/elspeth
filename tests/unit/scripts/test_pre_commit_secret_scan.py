"""Regression tests for staged-byte authority in the secret scanner."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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


# One staged sample per scanner pattern, assembled at runtime so this file's own
# source never matches the scanner.
_PATTERN_SAMPLES: dict[str, str] = {
    "JWT": "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
    "OpenAI key": "sk-" + "A" * 40,
    "Anthropic key": "sk-ant-" + "A" * 40,
    "Slack token": "xox" + "b-" + "1" * 20,
    "AWS access key": "AKIA" + "A" * 16,
    "Google API key": "AIza" + "B" * 35,
    "GitHub token": "ghp" + "_" + "C" * 36,
    "Bearer token (high-entropy)": "Bearer " + "D" * 40,
    "Private key block": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "Connection string with password": "postgres" + "://app:" + "pass" + "word@db",
    "High-entropy quoted secret": "api_key" + ' = "' + "E" * 30 + '"',
}


def _scanner_pattern_names() -> set[str]:
    names: set[str] = set()
    in_patterns = False
    for line in SECRET_SCAN.read_text(encoding="utf-8").splitlines():
        if line == "declare -a PATTERNS=(":
            in_patterns = True
            continue
        if in_patterns and line == ")":
            break
        if in_patterns:
            entry = line.strip()
            assert entry.startswith('"') and "|" in entry, line
            names.add(entry[1:].split("|", 1)[0])
    return names


def test_every_scanner_pattern_has_a_sample() -> None:
    assert _scanner_pattern_names() == set(_PATTERN_SAMPLES)


@pytest.mark.parametrize("name", sorted(_PATTERN_SAMPLES))
def test_scanner_rejects_each_pattern_in_the_index(tmp_path: Path, name: str) -> None:
    repository, scanner = _repository(tmp_path)
    candidate = repository / "candidate.txt"
    _commit(repository, candidate, "original\n")
    candidate.write_text(f"{_PATTERN_SAMPLES[name]}\n", encoding="utf-8")
    assert _run(["git", "add", "--", candidate.name], cwd=repository).returncode == 0

    result = _run([str(scanner)], cwd=repository)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{name}\x1b" in result.stderr or f"{name} matched in:" in result.stderr


def test_scanner_refuses_when_git_grep_fails(tmp_path: Path) -> None:
    repository, scanner = _repository(tmp_path)
    candidate = repository / "candidate.txt"
    _commit(repository, candidate, "original\n")
    candidate.write_text("nothing credential-shaped here\n", encoding="utf-8")
    assert _run(["git", "add", "--", candidate.name], cwd=repository).returncode == 0
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(f'#!/bin/sh\nif [ "$1" = grep ]; then exit 128; fi\nexec {real_git} "$@"\n', encoding="utf-8")
    shim.chmod(0o755)

    result = subprocess.run(
        [str(scanner)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "could not check candidate.txt" in result.stderr


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
