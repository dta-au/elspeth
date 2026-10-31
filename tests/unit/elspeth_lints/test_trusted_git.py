"""Foreign metadata and filesystem controls for callback-free observation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from elspeth_lints.core.operator_runtime import OperatorRuntimeEvidence
from elspeth_lints.core.source_snapshot import SourceSnapshotError, capture_source_snapshot, tracked_relative_paths
from elspeth_lints.core.trusted_git import TrustedGitRepository, read_regular_file


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["/usr/bin/git", "-C", str(repo), *arguments], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _repo(tmp_path: Path, *, algorithm: str = "sha1") -> Path:
    repo = tmp_path / "repo"
    (repo / "source").mkdir(parents=True)
    (repo / "allowlist").mkdir()
    (repo / "source" / "widget.py").write_bytes(b"VALUE = 1\n")
    (repo / "allowlist" / "_defaults.yaml").write_bytes(b"version: 1\n")
    _git(repo, "init", "-q", f"--object-format={algorithm}")
    _git(repo, "-c", "user.name=Observer", "-c", "user.email=observer@example.invalid", "add", ".")
    _git(repo, "-c", "user.name=Observer", "-c", "user.email=observer@example.invalid", "commit", "-qm", "initial")
    return repo


def test_tracked_relative_paths_uses_index_and_validates_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "source" / "résumé.py").write_bytes(b"VALUE = 2\n")
    _git(repo, "add", ".")

    assert tracked_relative_paths(repo) == ("allowlist/_defaults.yaml", "source/résumé.py", "source/widget.py")
    with pytest.raises(SourceSnapshotError, match="canonical repository root"):
        tracked_relative_paths(repo / "source")


@pytest.mark.parametrize("packed", (False, True))
@pytest.mark.parametrize("algorithm", ("sha1", "sha256"))
def test_raw_observation_supports_packed_objects_and_refs(tmp_path: Path, packed: bool, algorithm: str) -> None:
    repo = _repo(tmp_path, algorithm=algorithm)
    if packed:
        _git(repo, "gc", "--prune=now")
        _git(repo, "pack-refs", "--all")

    binding = capture_source_snapshot(source_root=repo / "source", allowlist_dir=repo / "allowlist")

    assert binding.source_rev == _git(repo, "rev-parse", "HEAD")
    assert binding.source_dirty is False


def test_private_metadata_is_copied_and_retains_no_original_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    expected_head = _git(repo, "rev-parse", "HEAD")
    original = repo / ".git" / "objects" / expected_head[:2] / expected_head[2:]
    with TrustedGitRepository(repo) as observed:
        copied = observed.private / "objects" / expected_head[:2] / expected_head[2:]
        assert copied.stat().st_ino != original.stat().st_ino
        assert "Observer" not in (observed.private / "config").read_text()
        original.chmod(0o600)
        original.write_bytes(b"corrupt mutable candidate object")
        assert observed.head() == expected_head
        private = observed.private
    assert not private.exists()


@pytest.mark.parametrize("kind", ("leaf", "ancestor", "fifo"))
def test_regular_reader_refuses_symlinks_and_special_files(tmp_path: Path, kind: str) -> None:
    original = tmp_path / "original"
    original.mkdir()
    (original / "bytes").write_bytes(b"bounded candidate bytes")
    assert read_regular_file(original / "bytes") == b"bounded candidate bytes"
    if kind == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(original, target_is_directory=True)
        path = alias / "bytes"
    elif kind == "leaf":
        path = tmp_path / "alias"
        path.symlink_to(original / "bytes")
    else:
        path = tmp_path / "pipe"
        os.mkfifo(path)

    with pytest.raises(SourceSnapshotError, match=r"symbolic links|regular file"):
        read_regular_file(path)


def test_regular_reader_enforces_byte_limit(tmp_path: Path) -> None:
    path = tmp_path / "bytes"
    path.write_bytes(b"abcd")
    assert read_regular_file(path, limit=4) == b"abcd"
    with pytest.raises(SourceSnapshotError, match="byte limit"):
        read_regular_file(path, limit=3)


def test_private_copy_budget_refuses_before_unbounded_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr("elspeth_lints.core.trusted_git._COPY_LIMIT", 1)
    with pytest.raises(SourceSnapshotError, match=r"byte limit|resource limit"):
        tracked_relative_paths(repo)


def test_candidate_config_symlink_is_not_opened(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = repo / ".git" / "config"
    config.unlink()
    config.symlink_to(tmp_path / "private-config")

    with pytest.raises(SourceSnapshotError, match="symbolic links"):
        tracked_relative_paths(repo)


def test_split_index_refuses_explicitly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "update-index", "--split-index")

    with pytest.raises(SourceSnapshotError, match=r"split|index"):
        tracked_relative_paths(repo)


@pytest.mark.parametrize("stream", ("stdout", "stderr"))
def test_raw_git_output_budget_kills_and_reaps_before_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: str) -> None:
    repo = _repo(tmp_path)
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def noisy_process(
        arguments: list[str], *, cwd: Path, env: dict[str, str], stdin: int, stdout: int, stderr: int, close_fds: bool
    ) -> subprocess.Popen[bytes]:
        script = f"import sys,time; sys.{stream}.write('x'*8192); sys.{stream}.flush(); time.sleep(2)"
        child = real_popen([sys.executable, "-c", script], cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, close_fds=close_fds)
        children.append(child)
        return child

    with TrustedGitRepository(repo) as observed:
        monkeypatch.setattr("elspeth_lints.core.trusted_git._FILE_LIMIT", 1024)
        monkeypatch.setattr("elspeth_lints.core.trusted_git.subprocess.Popen", noisy_process)
        started = time.monotonic()
        with pytest.raises(SourceSnapshotError, match=r"output.*limit"):
            observed._run("rev-parse", "HEAD")
        assert time.monotonic() - started < 1.5
        assert children and children[0].poll() is not None


def test_protected_runtime_uses_validated_scratch_instead_of_ambient_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    scratch = tmp_path / "protected-scratch"
    scratch.mkdir()
    evidence = OperatorRuntimeEvidence(repo, "synthetic-artifact", 1001, 1002, repo, ())
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.current_operator_runtime", lambda: evidence)
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.runtime_private_directory", lambda: scratch)
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.validate_protected_path", lambda path: None)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "candidate-scratch"))
    with TrustedGitRepository(repo) as observed:
        assert observed.private.parent == scratch


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Observer", "-c", "user.email=observer@example.invalid", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _replace_directory(repo: Path, kind: str) -> str:
    (repo / "real").mkdir()
    (repo / "real" / "y.py").write_bytes(b"y\n")
    (repo / "d").mkdir()
    (repo / "d" / "x.py").write_bytes(b"x\n")
    baseline = _commit_all(repo, "directory")
    (repo / "d" / "x.py").unlink()
    (repo / "d").rmdir()
    if kind == "symlink":
        os.symlink("real", repo / "d")
    else:
        (repo / "d").write_bytes(b"now a file\n")
    _commit_all(repo, "replace directory")
    return baseline


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_changed_paths_survive_a_baseline_directory_replaced_by_a_non_directory(tmp_path: Path, kind: str) -> None:
    # Git never reads d/.gitattributes once d is a symlink or a file, so the
    # attribute probe for the baseline-only path d/x.py must not abort.
    repo = _repo(tmp_path)
    baseline = _replace_directory(repo, kind)
    expected = tuple(_git(repo, "diff", "--name-only", "--diff-filter=ACMR", baseline, "--").splitlines())
    with TrustedGitRepository(repo) as observed:
        assert observed.changed_worktree_paths(baseline) == expected == ("d",)


def test_attributes_beyond_a_replaced_directory_are_still_read_at_their_real_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    baseline = _replace_directory(repo, "symlink")
    (repo / "real" / ".gitattributes").write_bytes(b"*.py filter=candidate\n")
    with TrustedGitRepository(repo) as observed, pytest.raises(SourceSnapshotError, match="attributes are unsupported"):
        observed.changed_worktree_paths(baseline)


def test_symlinked_attributes_file_in_a_real_directory_still_refuses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "elsewhere").write_bytes(b"*.py filter=candidate\n")
    os.symlink("../elsewhere", repo / "source" / ".gitattributes")
    with TrustedGitRepository(repo) as observed, pytest.raises(SourceSnapshotError, match="without symbolic links"):
        observed.changed_worktree_paths(_git(repo, "rev-parse", "HEAD"))


def test_symlinked_repository_info_attributes_still_refuse(tmp_path: Path) -> None:
    # $GIT_DIR/info/attributes is read through a symlinked info directory, so
    # the replaced-directory exemption must never cover the fixed locations.
    repo = _repo(tmp_path)
    shutil.rmtree(repo / ".git" / "info")
    (tmp_path / "info").mkdir()
    (tmp_path / "info" / "attributes").write_bytes(b"*.py text eol=crlf\n")
    os.symlink(tmp_path / "info", repo / ".git" / "info")
    with pytest.raises(SourceSnapshotError), TrustedGitRepository(repo) as observed:
        observed.changed_worktree_paths(_git(repo, "rev-parse", "HEAD"))
