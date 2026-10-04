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


def _preflight(repo: Path, intent: str, *options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), "--intent", intent, *options], cwd=repo, capture_output=True, text=True, check=False)


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


@pytest.fixture
def fetch_repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """A main-only clone with a stale release ref and an advanced remote tip."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main")
    _git(seed, "config", "user.name", "Preflight Test")
    _git(seed, "config", "user.email", "preflight@example.invalid")
    _git(seed, "config", "commit.gpgsign", "false")
    _git(seed, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (seed / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "tracked.txt")
    _git(seed, "commit", "--quiet", "-m", "base")
    _git(seed, "branch", "release/0.8.2")
    remote = tmp_path / "remote.git"
    _git(seed, "clone", "--quiet", "--bare", str(seed), str(remote))
    repo = tmp_path / "clone"
    _git(seed, "clone", "--quiet", "--single-branch", "--branch", "main", str(remote), str(repo))
    _git(repo, "checkout", "--quiet", "-b", "task")
    old = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/release/0.8.2", old)
    for source, package in (("src", "elspeth"), ("elspeth-lints/src", "elspeth_lints")):
        package_dir = repo / source / package
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    _git(seed, "checkout", "--quiet", "release/0.8.2")
    (seed / "tracked.txt").write_text("release advanced\n", encoding="utf-8")
    _git(seed, "commit", "--quiet", "-am", "advance release")
    new = _git(seed, "rev-parse", "HEAD").strip()
    _git(seed, "push", "--quiet", str(remote), "release/0.8.2")
    return repo, remote, old, new


@pytest.mark.parametrize("mapping", ["single", "wildcard", "explicit", "alias", "foreign-alias"])
def test_fetch_refreshes_actual_named_base(fetch_repo: tuple[Path, Path, str, str], mapping: str) -> None:
    repo, _, old, new = fetch_repo
    base = "origin/release/0.8.2"
    if mapping == "wildcard":
        _git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    elif mapping in {"explicit", "alias", "foreign-alias"}:
        if mapping in {"alias", "foreign-alias"}:
            base = "cache/rc-candidate" if mapping == "foreign-alias" else "origin/rc-candidate"
            _git(repo, "update-ref", f"refs/remotes/{base}", old)
        _git(repo, "config", "remote.origin.fetch", f"+refs/heads/release/0.8.2:refs/remotes/{base}")
    config = _git(repo, "config", "--get-all", "remote.origin.fetch")
    assert _git(repo, "rev-parse", base).strip() == old

    result = _preflight(repo, "merge", "--base", base, "--fetch")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "rev-parse", base).strip() == new
    assert "[WARN] base" in result.stdout
    assert _git(repo, "config", "--get-all", "remote.origin.fetch") == config


def test_without_fetch_leaves_tracking_refs_and_config_untouched(fetch_repo: tuple[Path, Path, str, str]) -> None:
    repo, _, old, _ = fetch_repo
    refs = _git(repo, "show-ref")
    config = _git(repo, "config", "--local", "--list")
    result = _preflight(repo, "merge", "--base", "origin/release/0.8.2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "show-ref") == refs
    assert _git(repo, "config", "--local", "--list") == config
    assert f"origin/release/0.8.2 ({old[:9]})" in result.stdout
    assert "not fetched" in result.stdout


@pytest.mark.parametrize("qualified", [False, True])
def test_fetch_creates_missing_release_tracking_ref(fetch_repo: tuple[Path, Path, str, str], qualified: bool) -> None:
    repo, _, _, new = fetch_repo
    ref = "refs/remotes/origin/release/0.8.2"
    _git(repo, "update-ref", "-d", ref)
    base = ref if qualified else "origin/release/0.8.2"
    result = _preflight(repo, "merge", "--base", base, "--fetch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "rev-parse", ref).strip() == new


def test_failed_base_fetch_is_reported_and_refused(fetch_repo: tuple[Path, Path, str, str]) -> None:
    repo, remote, old, _ = fetch_repo
    _git(repo, "remote", "set-url", "origin", str(remote.parent / "missing.git"))
    result = _preflight(repo, "merge", "--base", "origin/release/0.8.2", "--fetch")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] fetch-base" in result.stdout
    assert _git(repo, "rev-parse", "origin/release/0.8.2").strip() == old


@pytest.mark.parametrize("remote_name", ["upstream", "team/origin"])
def test_named_base_uses_its_actual_remote(fetch_repo: tuple[Path, Path, str, str], remote_name: str) -> None:
    repo, _, _, new = fetch_repo
    _git(repo, "remote", "rename", "origin", remote_name)
    result = _preflight(repo, "merge", "--base", f"{remote_name}/release/0.8.2", "--fetch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "rev-parse", f"{remote_name}/release/0.8.2").strip() == new


def test_wildcard_alias_preserves_literal_branch_characters(fetch_repo: tuple[Path, Path, str, str]) -> None:
    repo, remote, old, new = fetch_repo
    _git(remote, "branch", "release/feature&fix", "release/0.8.2")
    _git(repo, "config", "remote.origin.fetch", "+refs/heads/release/*:refs/remotes/origin/rel/*")
    base = "origin/rel/feature&fix"
    _git(repo, "update-ref", f"refs/remotes/{base}", old)
    result = _preflight(repo, "merge", "--base", base, "--fetch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "rev-parse", base).strip() == new


def test_ambiguous_base_mapping_is_refused(fetch_repo: tuple[Path, Path, str, str]) -> None:
    repo, remote, _, _ = fetch_repo
    mapping = "+refs/heads/release/0.8.2:refs/remotes/origin/release/0.8.2"
    _git(repo, "config", "remote.origin.fetch", mapping)
    _git(repo, "remote", "add", "mirror", str(remote))
    _git(repo, "config", "remote.mirror.fetch", mapping)
    result = _preflight(repo, "merge", "--base", "origin/release/0.8.2", "--fetch")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] fetch-base" in result.stdout
    assert "ambiguous fetch mappings" in result.stdout


@pytest.mark.parametrize("exclusion", ["^refs/heads/release/*", "^refs/heads/release/0.8.2"])
def test_negative_refspec_leaves_one_actual_mapping_owner(fetch_repo: tuple[Path, Path, str, str], exclusion: str) -> None:
    repo, remote, _, new = fetch_repo
    _git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(repo, "config", "--add", "remote.origin.fetch", exclusion)
    _git(repo, "remote", "add", "mirror", str(remote))
    _git(repo, "config", "remote.mirror.fetch", "+refs/heads/release/*:refs/remotes/origin/release/*")
    config = _git(repo, "config", "--local", "--list")
    result = _preflight(repo, "merge", "--base", "origin/release/0.8.2", "--fetch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "rev-parse", "origin/release/0.8.2").strip() == new
    assert "refreshed from mirror:refs/heads/release/0.8.2" in result.stdout
    assert _git(repo, "config", "--local", "--list") == config


@pytest.mark.parametrize("positive_mapping", ["+refs/heads/main:refs/remotes/origin/main", "+refs/heads/*:refs/remotes/origin/*"])
def test_explicit_refresh_does_not_override_a_negative_refspec(fetch_repo: tuple[Path, Path, str, str], positive_mapping: str) -> None:
    repo, _, old, _ = fetch_repo
    _git(repo, "config", "remote.origin.fetch", positive_mapping)
    _git(repo, "config", "--add", "remote.origin.fetch", "^refs/heads/release/*")
    result = _preflight(repo, "merge", "--base", "origin/release/0.8.2", "--fetch")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] fetch-base" in result.stdout
    assert "excluded" in result.stdout
    assert _git(repo, "rev-parse", "origin/release/0.8.2").strip() == old


@pytest.mark.parametrize("has_mirror_owner", [False, True])
def test_excluded_alias_never_falls_back_to_an_unrelated_branch(fetch_repo: tuple[Path, Path, str, str], has_mirror_owner: bool) -> None:
    repo, remote, old, new = fetch_repo
    base = "origin/rc-candidate"
    _git(remote, "branch", "rc-candidate", old)
    _git(repo, "update-ref", f"refs/remotes/{base}", old)
    mapping = f"+refs/heads/release/0.8.2:refs/remotes/{base}"
    _git(repo, "config", "remote.origin.fetch", mapping)
    _git(repo, "config", "--add", "remote.origin.fetch", "^refs/heads/release/*")
    if has_mirror_owner:
        _git(repo, "remote", "add", "mirror", str(remote))
        _git(repo, "config", "remote.mirror.fetch", mapping)
    result = _preflight(repo, "merge", "--base", base, "--fetch")
    assert result.returncode == (0 if has_mirror_owner else 1), result.stdout + result.stderr
    assert _git(repo, "rev-parse", base).strip() == (new if has_mirror_owner else old)
    assert "refreshed from origin:refs/heads/rc-candidate" not in result.stdout


@pytest.mark.parametrize("base", ["origin/HEAD", "refs/remotes/origin/HEAD", "HEAD"])
def test_fetch_preserves_symbolic_and_local_base_semantics(fetch_repo: tuple[Path, Path, str, str], base: str) -> None:
    repo, _, old, _ = fetch_repo
    result = _preflight(repo, "merge", "--base", base, "--fetch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "rev-parse", base).strip() == old
    assert _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD").strip() == "refs/remotes/origin/main"
