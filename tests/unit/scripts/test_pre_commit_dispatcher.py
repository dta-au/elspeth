"""Regression tests for the repository-local pre-commit dispatcher."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DISPATCHER = PROJECT_ROOT / "scripts" / "git-hooks" / "pre-commit-dispatcher.sh"
SECRET_SCAN = PROJECT_ROOT / "scripts" / "git-hooks" / "pre-commit-secret-scan.sh"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=False, capture_output=True, text=True, timeout=30)


def _dispatcher_repo(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run(["git", "init", "--quiet"], cwd=repo).returncode == 0
    capture = tmp_path / "hook-arguments"
    hook_command = tmp_path / "capture-hook-command"
    hook_command.write_text('#!/usr/bin/env bash\nprintf "%s\\0" "$@" > "$DISPATCH_CAPTURE"\n', encoding="utf-8")
    hook_command.chmod(0o755)
    env = {
        **os.environ,
        "ELSPETH_PRE_COMMIT_PYTHON": str(hook_command),
        "DISPATCH_CAPTURE": str(capture),
    }
    return repo, env, capture


def _assert_dispatched(capture: Path, filenames: list[str]) -> None:
    arguments = [os.fsdecode(value) for value in capture.read_bytes().split(b"\0")[:-1]]
    assert arguments == ["-mpre_commit", "run", "--files", *filenames]


def test_partial_staging_refuses_before_hooks_then_complete_staging_passes(tmp_path: Path) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    selected = repo / "selected.py"
    selected.write_text("invalid staged Python!\n", encoding="utf-8")
    assert _run(["git", "add", "--", selected.name], cwd=repo).returncode == 0
    selected.write_text("valid_working_value = 1\n", encoding="utf-8")
    foreign = repo / "foreign.txt"
    foreign.write_bytes(b"unowned working content\n")

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert refused.returncode != 0
    assert "Staged bytes/mode differ" in refused.stderr
    assert not capture.exists()
    assert selected.read_text() == "valid_working_value = 1\n"
    assert foreign.read_bytes() == b"unowned working content\n"
    staged = _run(["git", "show", ":selected.py"], cwd=repo)
    assert staged.returncode == 0 and staged.stdout == "invalid staged Python!\n"

    assert _run(["git", "add", "--", selected.name], cwd=repo).returncode == 0
    accepted = _run([str(DISPATCHER)], cwd=repo, env=env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    _assert_dispatched(capture, [selected.name])
    assert foreign.read_bytes() == b"unowned working content\n"


@pytest.mark.parametrize("filename", ["ordinary.bin", "space and\ttab\nnewline.bin", ":(glob)*", "snowman-☃.bin"])
def test_exact_staged_bytes_preserve_literal_filenames(tmp_path: Path, filename: str) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    selected = repo / filename
    selected.write_bytes(b"raw\x00binary\xffbytes\n")
    assert _run(["git", "--literal-pathspecs", "add", "--", filename], cwd=repo).returncode == 0
    # A glob-looking name must not select this other, unstaged file.
    (repo / "other.bin").write_bytes(b"different\n")

    result = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_dispatched(capture, [filename])
    assert selected.read_bytes() == b"raw\x00binary\xffbytes\n"


@pytest.mark.parametrize("filename", ["--all-files", "-directory/selected.txt"])
def test_leading_dash_path_refuses_before_real_pre_commit_hooks_without_mutation(tmp_path: Path, filename: str) -> None:
    repo, env, _capture = _dispatcher_repo(tmp_path)
    env["ELSPETH_PRE_COMMIT_PYTHON"] = sys.executable
    env["PRE_COMMIT_HOME"] = str(tmp_path / "pre-commit-home")
    env.pop("SKIP", None)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: observe-filenames\n"
        "        name: Observe actual hook filenames\n"
        f"        entry: {json.dumps(shlex.quote(sys.executable) + ' hook_probe.py')}\n"
        "        language: system\n"
        "        always_run: true\n",
        encoding="utf-8",
    )
    (repo / "hook_probe.py").write_text(
        "import json, sys\nfrom pathlib import Path\nPath('hook-dispatched').write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    ordinary = repo / "ordinary.txt"
    ordinary.write_text("real filename-hook positive\n", encoding="utf-8")
    assert _run(["git", "add", "--", ordinary.name], cwd=repo).returncode == 0
    positive = _run([str(DISPATCHER)], cwd=repo, env=env)
    assert positive.returncode == 0, positive.stdout + positive.stderr
    marker = repo / "hook-dispatched"
    assert json.loads(marker.read_text()) == [ordinary.name]
    marker.unlink()

    selected = repo / filename
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_bytes(b"unsupported name, unchanged bytes\n")
    assert _run(["git", "--literal-pathspecs", "add", "--", filename], cwd=repo).returncode == 0
    index_path = _run(["git", "rev-parse", "--git-path", "index"], cwd=repo)
    assert index_path.returncode == 0
    index = repo / index_path.stdout.strip()
    before_index = index.read_bytes()
    before_files = {path: path.read_bytes() for path in repo.iterdir() if path.is_file()}
    before_selected = selected.read_bytes()

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert refused.returncode != 0
    assert "Unsupported leading-dash staged path" in refused.stderr
    assert not marker.exists()
    assert index.read_bytes() == before_index
    assert selected.read_bytes() == before_selected
    assert {path: path.read_bytes() for path in repo.iterdir() if path.is_file()} == before_files


@pytest.mark.parametrize("staged_mode,working_mode", [(0o644, 0o755), (0o755, 0o644)])
def test_executable_mode_mismatch_refuses_even_when_git_ignores_filemode(tmp_path: Path, staged_mode: int, working_mode: int) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    selected = repo / "selected.sh"
    selected.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    selected.chmod(staged_mode)
    assert _run(["git", "config", "core.fileMode", "true"], cwd=repo).returncode == 0
    assert _run(["git", "add", "--", selected.name], cwd=repo).returncode == 0
    assert _run(["git", "config", "core.fileMode", "false"], cwd=repo).returncode == 0
    selected.chmod(working_mode)

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)
    assert refused.returncode != 0 and "Staged bytes/mode differ" in refused.stderr
    assert not capture.exists()

    selected.chmod(staged_mode)
    accepted = _run([str(DISPATCHER)], cwd=repo, env=env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    _assert_dispatched(capture, [selected.name])


@pytest.mark.parametrize("replacement", ["missing", "directory", "symlink", "fifo"])
def test_nonregular_or_missing_working_input_refuses_without_opening_it(tmp_path: Path, replacement: str) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    selected = repo / "selected.txt"
    selected.write_text("staged\n", encoding="utf-8")
    assert _run(["git", "add", "--", selected.name], cwd=repo).returncode == 0
    selected.unlink()
    if replacement == "directory":
        selected.mkdir()
    elif replacement == "symlink":
        target = repo / "target.txt"
        target.write_text("staged\n", encoding="utf-8")
        selected.symlink_to(target.name)
    elif replacement == "fifo":
        os.mkfifo(selected)

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert refused.returncode != 0
    assert "Working input is not a regular file" in refused.stderr
    assert not capture.exists()


def test_staged_symlink_is_explicitly_unsupported(tmp_path: Path) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    (repo / "target.txt").write_text("target bytes are not indexed link bytes\n", encoding="utf-8")
    (repo / "selected.txt").symlink_to("target.txt")
    assert _run(["git", "add", "--", "selected.txt"], cwd=repo).returncode == 0

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert refused.returncode != 0
    assert "Unsupported staged file mode 120000" in refused.stderr
    assert not capture.exists()


def test_unmerged_index_entries_refuse_before_filename_hooks(tmp_path: Path) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    selected = repo / "selected.txt"
    selected.write_text("same bytes cannot resolve an index conflict\n", encoding="utf-8")
    assert _run(["git", "add", "--", selected.name], cwd=repo).returncode == 0
    blob = _run(["git", "rev-parse", ":selected.txt"], cwd=repo)
    assert blob.returncode == 0
    oid = blob.stdout.strip()
    conflict = subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=repo,
        input=f"0 {'0' * len(oid)}\tselected.txt\n100644 {oid} 1\tselected.txt\n100644 {oid} 2\tselected.txt\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert conflict.returncode == 0, conflict.stderr

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert refused.returncode != 0
    assert "Expected one resolved index entry" in refused.stderr
    assert not capture.exists()


def test_empty_index_does_not_start_filename_hooks(tmp_path: Path) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)

    result = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not capture.exists()


def test_clean_filters_do_not_substitute_normalized_bytes_for_hook_input(tmp_path: Path) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.txt text eol=crlf\n", encoding="utf-8")
    selected = repo / "selected.txt"
    selected.write_bytes(b"line\r\n")
    assert _run(["git", "add", "--", selected.name], cwd=repo).returncode == 0

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert refused.returncode != 0 and "Staged bytes/mode differ" in refused.stderr
    assert selected.read_bytes() == b"line\r\n"
    assert not capture.exists()


@pytest.mark.parametrize("operation", ["diff", "ls-files", "cat-file", "hash-object", "stat"])
def test_failed_reads_refuse_even_after_emitting_plausible_output(tmp_path: Path, operation: str) -> None:
    repo, env, capture = _dispatcher_repo(tmp_path)
    selected = repo / "selected.txt"
    selected.write_text("staged\n", encoding="utf-8")
    assert _run(["git", "add", "--", selected.name], cwd=repo).returncode == 0
    binary_name = "stat" if operation == "stat" else "git"
    original_binary = shutil.which(binary_name)
    assert original_binary is not None
    bin_dir = tmp_path / "controlled-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / binary_name
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        # Delegate first, so the controlled error can follow valid output.
        f'{shlex.quote(original_binary)} "$@"\n'
        "native_status=$?\n"
        + (
            "exit 79\n"
            if operation == "stat"
            else f'for argument in "$@"; do\n    if [[ "$argument" == {shlex.quote(operation)} ]]; then exit 79; fi\ndone\n'
            'exit "$native_status"\n'
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]

    refused = _run([str(DISPATCHER)], cwd=repo, env=env)

    assert refused.returncode != 0
    assert "Cannot read" in refused.stderr
    assert not capture.exists()
    assert selected.read_text() == "staged\n"


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
