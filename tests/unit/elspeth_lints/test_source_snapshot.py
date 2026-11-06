"""Exact Git/source binding for review bundles."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth_lints.core.source_snapshot import SourceSnapshotError, capture_source_snapshot


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    source_root = repo / "src" / "elspeth"
    allowlist_dir = repo / "config" / "cicd" / "enforce_tier_model"
    source_root.mkdir(parents=True)
    allowlist_dir.mkdir(parents=True)
    (source_root / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    (allowlist_dir / "_defaults.yaml").write_text("version: 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "snapshot@example.invalid")
    _git(repo, "config", "user.name", "Snapshot Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo, source_root, allowlist_dir


def test_capture_source_snapshot_records_clean_head_and_digest(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)

    binding = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)

    assert binding.source_rev == _git(repo, "rev-parse", "HEAD")
    assert binding.source_dirty is False
    assert len(binding.source_snapshot_sha256) == 64


def test_capture_source_snapshot_records_tracked_dirty_source(tmp_path: Path) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    (source_root / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")

    binding = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)

    assert binding.source_dirty is True


@pytest.mark.parametrize("relative", ("new.py", "ignored.py"))
def test_capture_source_snapshot_rejects_relevant_untracked_python(tmp_path: Path, relative: str) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    if relative == "ignored.py":
        (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
    (source_root / relative).write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="tracked Git path"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_rejects_relevant_untracked_allowlist(tmp_path: Path) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    (allowlist_dir / "new.yaml").write_text("allow_hits: []\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="tracked Git path"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


@pytest.mark.parametrize("ignored", (False, True))
def test_capture_source_snapshot_rejects_untracked_python_symlink_alias(tmp_path: Path, ignored: bool) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    alias = source_root / "alias.py"
    if ignored:
        (repo / ".gitignore").write_text("alias.py\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore alias")
    alias.symlink_to("widget.py")

    with pytest.raises(SourceSnapshotError, match="symbolic link"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


@pytest.mark.parametrize("ignored", (False, True))
def test_capture_source_snapshot_rejects_untracked_allowlist_symlink_alias(tmp_path: Path, ignored: bool) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    alias = allowlist_dir / "alias.yaml"
    if ignored:
        (repo / ".gitignore").write_text("alias.yaml\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore alias")
    alias.symlink_to("_defaults.yaml")

    with pytest.raises(SourceSnapshotError, match="symbolic link"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_rejects_tracked_scanner_symlink(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    alias = source_root / "alias.py"
    alias.symlink_to("widget.py")
    _git(repo, "add", "src/elspeth/alias.py")
    _git(repo, "commit", "-qm", "track alias")

    with pytest.raises(SourceSnapshotError, match="symbolic link"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_ignores_yml_not_consumed_by_allowlist_loader(tmp_path: Path) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    before = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)
    yml = allowlist_dir / "ignored.yml"
    yml.write_text("allow_hits: []\n", encoding="utf-8")

    after = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)

    assert after == before


def test_capture_source_snapshot_refuses_nested_yaml_rather_than_ignoring_it(tmp_path: Path) -> None:
    """A nested allowlist document has no meaning; silently ignoring one is how a decoy hides (elspeth-3262174e37)."""
    from elspeth_lints.core.allowlist import NestedAllowlistDocumentError

    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    nested = allowlist_dir / "nested" / "ignored.yaml"
    nested.parent.mkdir()
    nested.write_text("allow_hits: []\n", encoding="utf-8")

    with pytest.raises(NestedAllowlistDocumentError, match=r"nested/ignored\.yaml"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_ignores_ambient_git_index_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    expected = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "empty-alternate-index"))

    assert capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir) == expected


def test_capture_source_snapshot_ignores_ambient_git_retargeting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path / "target")
    other_repo, _other_source, _other_allowlist = _repo(tmp_path / "other")
    (other_repo / "other.txt").write_text("different history\n", encoding="utf-8")
    _git(other_repo, "add", "other.txt")
    _git(other_repo, "commit", "-qm", "different head")
    expected_head = _git(repo, "rev-parse", "HEAD")
    assert _git(other_repo, "rev-parse", "HEAD") != expected_head
    monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))

    binding = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)

    assert binding.source_rev == expected_head


def test_capture_source_snapshot_supports_linked_worktree_metadata(tmp_path: Path) -> None:
    repo, _source_root, _allowlist_dir = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked), "HEAD")

    binding = capture_source_snapshot(
        source_root=linked / "src" / "elspeth",
        allowlist_dir=linked / "config" / "cicd" / "enforce_tier_model",
    )

    assert binding.source_rev == _git(repo, "rev-parse", "HEAD")
    assert binding.source_dirty is False


def test_capture_source_snapshot_wraps_input_read_oserror(tmp_path: Path) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    from elspeth_lints.core.trusted_git import read_regular_file

    original = read_regular_file

    def fail_widget(path: Path) -> bytes:
        if path.name == "widget.py":
            raise OSError("simulated read failure")
        return original(path)

    with (
        patch("elspeth_lints.core.source_snapshot.read_regular_file", fail_widget),
        pytest.raises(SourceSnapshotError, match=r"cannot read scanner input.*widget.py"),
    ):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_wraps_git_launch_oserror(tmp_path: Path) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)

    with (
        patch("elspeth_lints.core.trusted_git.subprocess.Popen", side_effect=OSError("git unavailable")),
        pytest.raises(SourceSnapshotError, match="cannot execute Git"),
    ):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_allows_irrelevant_untracked_file(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    (repo / "notes.txt").write_text("not a scanner input\n", encoding="utf-8")

    binding = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)

    assert binding.source_dirty is False


def test_capture_source_snapshot_hashes_candidate_at_logical_allowlist_identity(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    candidate = repo / ".elspeth" / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "_defaults.yaml").write_text("version: 2\n", encoding="utf-8")

    public = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)
    candidate_binding = capture_source_snapshot(
        source_root=source_root,
        allowlist_dir=candidate,
        logical_allowlist_dir=allowlist_dir,
    )

    assert candidate_binding.source_snapshot_sha256 != public.source_snapshot_sha256
    assert candidate_binding.source_rev == public.source_rev
    assert candidate_binding.source_dirty == public.source_dirty


def test_capture_source_snapshot_rejects_unborn_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source_root = repo / "src"
    allowlist_dir = repo / "allowlist"
    source_root.mkdir(parents=True)
    allowlist_dir.mkdir()
    _git(repo, "init", "-q")

    with pytest.raises(SourceSnapshotError, match="Git HEAD"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_rejects_roots_in_different_repositories(tmp_path: Path) -> None:
    _repo_path, source_root, _allowlist_dir = _repo(tmp_path / "one")
    _other_repo, _other_source, other_allowlist = _repo(tmp_path / "two")

    with pytest.raises(SourceSnapshotError, match="same Git repository"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=other_allowlist)


def test_capture_source_snapshot_does_not_use_candidate_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    expected = capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "executed"
    fake_git = bin_dir / "git"
    fake_git.write_text(f"#!/bin/sh\nprintf attacked > '{marker}'\nexit 1\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    assert capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir) == expected
    assert not marker.exists()


@pytest.mark.parametrize("callback", ("core.fsmonitor", "filter.hostile.clean", "filter.hostile.process"))
def test_capture_source_snapshot_never_runs_repository_callbacks(tmp_path: Path, callback: str) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    marker = tmp_path / "callback-executed"
    callback_script = tmp_path / "callback"
    callback_script.write_text(f"#!/bin/sh\nprintf attacked > '{marker}'\nexit 1\n", encoding="utf-8")
    callback_script.chmod(0o755)
    _git(repo, "config", callback, str(callback_script))
    if callback.startswith("filter."):
        (repo / ".gitattributes").write_text("*.py filter=hostile\n", encoding="utf-8")
    (source_root / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")

    if callback.startswith("filter."):
        with pytest.raises(SourceSnapshotError, match="attributes"):
            capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)
    else:
        assert capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir).source_dirty
    assert not marker.exists()


def test_capture_source_snapshot_refuses_config_includes_without_opening_target(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    include = tmp_path / "private-config"
    _git(repo, "config", "include.path", str(include))

    with pytest.raises(SourceSnapshotError, match="includes"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


@pytest.mark.parametrize("change", ("staged", "deleted", "staged-deleted", "mode", "non-python", "utf8"))
def test_capture_source_snapshot_observes_whole_tracked_source(tmp_path: Path, change: str) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    if change in {"non-python", "utf8"}:
        target = source_root / ("résumé.txt" if change == "utf8" else "schema.json")
        target.write_bytes(b"initial\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "track auxiliary source")
        target.write_bytes(b"changed\n")
    elif change == "mode":
        (source_root / "widget.py").chmod(0o755)
    elif change in {"deleted", "staged-deleted"}:
        (source_root / "widget.py").unlink()
        if change == "staged-deleted":
            _git(repo, "add", "-u")
    else:
        (source_root / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
        _git(repo, "add", ".")

    assert capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir).source_dirty


def test_capture_source_snapshot_preserves_net_head_dirty_semantics(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    widget = source_root / "widget.py"
    original = widget.read_bytes()
    widget.write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    widget.write_bytes(original)

    assert subprocess.run(["/usr/bin/git", "-C", str(repo), "diff", "--quiet", "HEAD"], check=False).returncode == 0
    assert capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir).source_dirty is False


def test_capture_source_snapshot_children_get_only_closed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo_path, source_root, allowlist_dir = _repo(tmp_path)
    monkeypatch.setenv("OBSERVER_SYNTHETIC_SECRET", "synthetic-test-value")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "candidate-imports"))
    original_popen = subprocess.Popen
    with patch("subprocess.Popen", wraps=original_popen) as run:
        assert capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir).source_dirty is False

    assert run.call_args_list
    for call in run.call_args_list:
        assert call.args[0][0] == "/usr/bin/git"
        child_env = call.kwargs["env"]
        assert "OBSERVER_SYNTHETIC_SECRET" not in child_env
        assert "PYTHONPATH" not in child_env
        assert "PATH" in child_env
        assert call.kwargs["close_fds"] is True


@pytest.mark.parametrize("attribute", ("text", "eol=crlf", "ident", "working-tree-encoding=UTF-16"))
def test_capture_source_snapshot_refuses_byte_transform_attributes(tmp_path: Path, attribute: str) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    (repo / ".gitattributes").write_text(f"*.py {attribute}\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="attributes"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_staged_delete_with_untracked_replacement_is_dirty(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    widget = source_root / "widget.py"
    original = widget.read_bytes()
    widget.unlink()
    _git(repo, "add", "-u")
    widget.write_bytes(original)

    with pytest.raises(SourceSnapshotError, match="tracked Git path"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


@pytest.mark.parametrize("flag", ("--skip-worktree", "--assume-unchanged"))
def test_capture_source_snapshot_refuses_index_flags_that_hide_source_changes(tmp_path: Path, flag: str) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    _git(repo, "update-index", flag, "src/elspeth/widget.py")
    (source_root / "widget.py").write_text("VALUE = 99\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="index"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_refuses_object_alternates(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    (repo / ".git" / "objects" / "info" / "alternates").write_text(str(tmp_path / "private-objects") + "\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="alternates"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_refuses_configured_newline_transforms(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")

    with pytest.raises(SourceSnapshotError, match="autocrlf"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


def test_capture_source_snapshot_refuses_intent_to_add(tmp_path: Path) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    (source_root / "pending.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "--intent-to-add", "src/elspeth/pending.py")

    with pytest.raises(SourceSnapshotError, match="intent-to-add"):
        capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir)


@pytest.mark.parametrize("suffix", ("py", "txt"))
def test_capture_source_snapshot_staged_addition_removed_again_is_net_clean(tmp_path: Path, suffix: str) -> None:
    repo, source_root, allowlist_dir = _repo(tmp_path)
    pending = source_root / f"pending.{suffix}"
    pending.write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    pending.unlink()

    assert capture_source_snapshot(source_root=source_root, allowlist_dir=allowlist_dir).source_dirty is False
