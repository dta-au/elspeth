"""All audit Git helpers share callback-free operation-scoped metadata reads."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from elspeth_lints.core import judge_coverage, per_file_blanket_ratchet, trust_boundary_diff
from elspeth_lints.core.operator_runtime import OperatorRuntimeEvidence, OperatorRuntimeUnavailableError
from elspeth_lints.core.source_snapshot import SourceSnapshotError
from elspeth_lints.core.trusted_git import TrustedGitRepository, git_metadata_operation, run_git_metadata
from elspeth_lints.rules.trust_tier.tier_model import rotate


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["/usr/bin/git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "source").mkdir(parents=True)
    (repo / "config" / "enforce_test").mkdir(parents=True)
    (repo / "source" / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "config" / "enforce_test" / "rules.yaml").write_text("version: 1\nallow_hits: []\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Metadata Test")
    _git(repo, "config", "user.email", "metadata@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


_RUNNERS = (
    trust_boundary_diff._run_git,
    judge_coverage._run_git,
    per_file_blanket_ratchet._run_git,
    rotate._run_git,
)
_Runner = Callable[..., subprocess.CompletedProcess[str]]


@pytest.mark.parametrize("runner", _RUNNERS)
def test_audit_git_helpers_ignore_hostile_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: _Runner) -> None:
    repo = _repo(tmp_path)
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    marker = tmp_path / "executed"
    executable = executable_dir / "git"
    executable.write_text(f"#!/bin/sh\nprintf attacked > '{marker}'\nexit 1\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable_dir))
    monkeypatch.setenv("METADATA_SYNTHETIC_KEY_REFERENCE", "synthetic-observation-value")

    result = runner(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], repo_root=repo)

    assert result.returncode == 0
    assert not marker.exists()


@pytest.mark.parametrize("runner", _RUNNERS)
def test_audit_git_helpers_refuse_promisor_callbacks(tmp_path: Path, runner: _Runner) -> None:
    repo = _repo(tmp_path)
    marker = tmp_path / "promisor-executed"
    callback = tmp_path / "callback"
    callback.write_text(f"#!/bin/sh\nprintf attacked > '{marker}'\nexit 1\n", encoding="utf-8")
    callback.chmod(0o755)
    blob = _git(repo, "rev-parse", "HEAD:source/widget.py")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    _git(repo, "config", "extensions.partialClone", "origin")
    _git(repo, "config", "remote.origin.promisor", "true")
    _git(repo, "config", "remote.origin.url", f"ext::{callback}")
    _git(repo, "config", "protocol.ext.allow", "always")
    errors = (
        trust_boundary_diff.TrustBoundaryDiffError,
        judge_coverage.JudgeCoverageError,
        per_file_blanket_ratchet.PerFileBlanketRatchetError,
        rotate.RotationAuditError,
    )

    with pytest.raises(errors, match=r"partial|promisor|extension"):
        runner(["show", "HEAD:source/widget.py"], repo_root=repo)
    assert not marker.exists()


@pytest.mark.parametrize("runner", _RUNNERS)
def test_audit_git_helpers_preserve_returncodes_for_missing_data(tmp_path: Path, runner: _Runner) -> None:
    repo = _repo(tmp_path)
    valid = runner(["show", "HEAD:source/widget.py"], repo_root=repo)
    assert valid.returncode == 0 and valid.stdout == "VALUE = 1\n"
    missing = runner(["show", "HEAD:source/absent.py"], repo_root=repo)
    assert missing.returncode != 0
    commit = runner(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], repo_root=repo)
    assert commit.returncode == 0
    absent = runner(["rev-parse", "--verify", "--quiet", "absent^{commit}"], repo_root=repo)
    assert absent.returncode == 1


@pytest.mark.parametrize("runner", _RUNNERS)
def test_audit_git_helpers_reject_command_and_revision_option_injection(tmp_path: Path, runner: _Runner) -> None:
    repo = _repo(tmp_path)
    errors = (
        trust_boundary_diff.TrustBoundaryDiffError,
        judge_coverage.JudgeCoverageError,
        per_file_blanket_ratchet.PerFileBlanketRatchetError,
        rotate.RotationAuditError,
    )
    with pytest.raises(errors, match=r"unsupported|revision|command"):
        runner(["config", "--global", "core.pager", "unsafe"], repo_root=repo)
    with pytest.raises(errors, match=r"unsupported|revision|option"):
        runner(["show", "--textconv:source/widget.py"], repo_root=repo)


@pytest.mark.parametrize("show", (trust_boundary_diff._git_show, judge_coverage._git_show, rotate._git_show))
def test_baseline_helpers_keep_absent_path_distinct_from_absent_baseline(tmp_path: Path, show: Callable[..., str | None]) -> None:
    repo = _repo(tmp_path)
    assert show(baseline_ref="HEAD", rel_path="source/absent.py", repo_root=repo) is None
    with pytest.raises((trust_boundary_diff.TrustBoundaryDiffError, judge_coverage.JudgeCoverageError, rotate.RotationAuditError)):
        show(baseline_ref="absent", rel_path="source/absent.py", repo_root=repo)


def test_blanket_changed_paths_still_include_unstaged_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "source" / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert per_file_blanket_ratchet._load_changed_paths(baseline_ref="HEAD", repo_root=repo) == ("source/widget.py",)


def test_blanket_worktree_comparison_preserves_missing_baseline_returncode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    arguments = ["diff", "--name-only", "--diff-filter=ACMR", "-z", "absent", "--"]
    expected = subprocess.run(["/usr/bin/git", "-C", str(repo), *arguments], check=False, capture_output=True)
    actual = per_file_blanket_ratchet._run_git(arguments, repo_root=repo)
    assert actual.returncode == expected.returncode == 128


def test_blanket_worktree_comparison_reads_symlink_bytes_without_target_access(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    link = repo / "skill"
    link.symlink_to("source/widget.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "tracked link")
    assert per_file_blanket_ratchet._load_changed_paths(baseline_ref="HEAD", repo_root=repo) == ()
    pipe = tmp_path / "private-pipe"
    os.mkfifo(pipe)
    link.unlink()
    link.symlink_to(pipe)

    assert per_file_blanket_ratchet._load_changed_paths(baseline_ref="HEAD", repo_root=repo) == ("skill",)


def test_top_level_audit_reuses_one_private_metadata_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD")
    for name in ("widget.py", "other.py"):
        (repo / "source" / name).write_text(
            "from elspeth.contracts import trust_boundary\n"
            "@trust_boundary(source_param='row', suppresses=('trust_tier.tier_model',))\n"
            "def convert(row):\n    return row\n",
            encoding="utf-8",
        )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "changed source")
    observations: list[Path] = []
    original = TrustedGitRepository._prepare

    def record_prepare(observed: TrustedGitRepository) -> None:
        observations.append(observed.private)
        original(observed)

    monkeypatch.setattr(TrustedGitRepository, "_prepare", record_prepare)
    trust_boundary_diff.find_new_trust_boundary_decorators(root=repo / "source", baseline_ref=baseline, repo_root=repo)

    assert len(observations) == 1
    assert not observations[0].exists()


def test_metadata_operations_do_not_share_private_views_between_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    rendezvous = Barrier(2)
    observations: list[Path] = []
    original = TrustedGitRepository._prepare

    def record_prepare(observed: TrustedGitRepository) -> None:
        observations.append(observed.private)
        original(observed)

    def inspect() -> str:
        with git_metadata_operation(repo):
            rendezvous.wait(timeout=10)
            first = run_git_metadata(["show", "HEAD:source/widget.py"], repo_root=repo)
            second = run_git_metadata(["show", "HEAD:source/widget.py"], repo_root=repo)
            assert first.returncode == second.returncode == 0
            return first.stdout

    monkeypatch.setattr(TrustedGitRepository, "_prepare", record_prepare)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(inspect) for _ in range(2)]
        assert [future.result(timeout=15) for future in futures] == ["VALUE = 1\n", "VALUE = 1\n"]
    assert len(observations) == len(set(observations)) == 2
    assert all(not path.exists() for path in observations)


def test_protected_git_executable_refusal_precedes_process_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    scratch = tmp_path / "protected-scratch"
    scratch.mkdir()
    evidence = OperatorRuntimeEvidence(repo, "synthetic-artifact", 1001, 1002, repo, ())
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.current_operator_runtime", lambda: evidence)
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.runtime_private_directory", lambda: scratch)

    def refuse(path: Path, *, directory: bool = False) -> None:
        raise OperatorRuntimeUnavailableError("synthetic Git ownership refusal")

    def forbidden_launch(*args: object, **kwargs: object) -> None:
        pytest.fail("unprotected Git was executed")

    monkeypatch.setattr("elspeth_lints.core.operator_runtime.validate_protected_path", refuse)
    monkeypatch.setattr("elspeth_lints.core.trusted_git.subprocess.Popen", forbidden_launch)
    with pytest.raises(SourceSnapshotError, match="Git executable"), TrustedGitRepository(repo):
        pass


def test_protected_git_executable_validation_has_a_positive_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    scratch = tmp_path / "protected-scratch"
    scratch.mkdir()
    evidence = OperatorRuntimeEvidence(repo, "synthetic-artifact", 1001, 1002, repo, ())
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.current_operator_runtime", lambda: evidence)
    monkeypatch.setattr("elspeth_lints.core.operator_runtime.runtime_private_directory", lambda: scratch)
    validated: list[Path] = []

    def allow(path: Path, *, directory: bool = False) -> None:
        validated.append(path)

    monkeypatch.setattr("elspeth_lints.core.operator_runtime.validate_protected_path", allow)
    with TrustedGitRepository(repo) as observed:
        assert observed.head()
    assert validated and all(path == Path("/usr/bin/git") for path in validated)
