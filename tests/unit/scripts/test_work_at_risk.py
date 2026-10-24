"""Exercise the work-at-risk report against real Git worktrees."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "work-at-risk.sh"
THIRTEEN_HOURS = 13 * 3600


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Temporary repositories must not inherit operator configuration."""
    for scope in ("GLOBAL", "SYSTEM"):
        config = tmp_path / f"git-{scope.lower()}.config"
        config.write_text("", encoding="utf-8")
        monkeypatch.setenv(f"GIT_CONFIG_{scope}", str(config))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A main checkout with a tracked src/elspeth and one clean linked worktree."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "--quiet", "--initial-branch=main")
    _git(main, "config", "user.name", "Work At Risk Test")
    _git(main, "config", "user.email", "war@example.invalid")
    _git(main, "config", "commit.gpgsign", "false")
    _git(main, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    package = main / "src" / "elspeth"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (main / ".gitignore").write_text(".claude/worktrees/\n.claude/lanes/\nsnap/\n", encoding="utf-8")
    _git(main, "add", ".gitignore", "src/elspeth/__init__.py")
    _git(main, "commit", "--quiet", "-m", "base")
    _git(main, "worktree", "add", "--quiet", "-b", "lane", str(main / ".claude" / "worktrees" / "lane"))
    return main


def _lane(repo: Path) -> Path:
    return repo / ".claude" / "worktrees" / "lane"


def _report(repo: Path, *options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *options], cwd=repo, capture_output=True, text=True, check=False)


def _row(output: str, path: Path) -> list[str]:
    rows = [line.split() for line in output.splitlines() if line.endswith(f"  {path}")]
    assert len(rows) == 1, output
    return rows[0]


def _age(path: Path, seconds: int) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_clean_worktrees_report_no_work_at_risk(repo: Path) -> None:
    """Negative control: a tracked src/ and a registered worktree under an ignored dir are not copies."""
    result = _report(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[WARN]" not in result.stdout
    assert "no work at risk" in result.stdout
    # The report runs from the main checkout, so its own process makes that tree IN-USE.
    for tree, use_expected in ((repo, "yes"), (_lane(repo), "-")):
        state, use, dirty, _oldest, _commit, copies, _lanes = _row(result.stdout, tree)[:7]
        assert (state, use, dirty, copies) == ("CLEAN", use_expected, "0", "0")


def test_uncommitted_work_older_than_twelve_hours_warns(repo: Path) -> None:
    """Positive control: an untracked file last modified 13 h ago."""
    stale = _lane(repo) / "notes.md"
    stale.write_text("unfinished\n", encoding="utf-8")
    _age(stale, THIRTEEN_HOURS)
    result = _report(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    state, _use, dirty, oldest = _row(result.stdout, _lane(repo))[:4]
    assert (state, dirty, oldest) == ("DIRTY", "1", "13")
    assert f"[WARN] {_lane(repo)} — 1 uncommitted path(s), oldest modified 13 h ago" in result.stdout


def test_fresh_uncommitted_work_is_dirty_without_warning(repo: Path) -> None:
    tracked = _lane(repo) / "src" / "elspeth" / "__init__.py"
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    result = _report(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _row(result.stdout, _lane(repo))[:3] == ["DIRTY", "-", "1"]
    assert "[WARN]" not in result.stdout


def test_the_oldest_path_decides_the_age(repo: Path) -> None:
    fresh = _lane(repo) / "fresh.md"
    fresh.write_text("new\n", encoding="utf-8")
    stale = _lane(repo) / "deep" / "stale.md"
    stale.parent.mkdir()
    stale.write_text("old\n", encoding="utf-8")
    _age(stale, THIRTEEN_HOURS)
    result = _report(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _row(result.stdout, _lane(repo))[2:4] == ["2", "13"]


@pytest.fixture
def process_inside_lane(repo: Path) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(["sleep", "60"], cwd=_lane(repo))
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def test_dirty_is_reported_even_when_the_tree_is_in_use(repo: Path, process_inside_lane: subprocess.Popen[bytes]) -> None:
    stale = _lane(repo) / "notes.md"
    stale.write_text("unfinished\n", encoding="utf-8")
    _age(stale, THIRTEEN_HOURS)
    result = _report(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _row(result.stdout, _lane(repo))[:3] == ["DIRTY", "yes", "1"]
    assert "(tree is IN-USE)" in result.stdout


def test_ignored_source_copy_warns_although_git_status_is_clean(repo: Path) -> None:
    copy = _lane(repo) / "snap" / "v1" / "src" / "elspeth" / "__init__.py"
    copy.parent.mkdir(parents=True)
    copy.write_text("", encoding="utf-8")
    assert _git(_lane(repo), "status", "--porcelain") == ""
    result = _report(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    row = _row(result.stdout, _lane(repo))
    assert (row[0], row[5]) == ("CLEAN", "1")
    assert f"[WARN] {_lane(repo)} — 1 src/elspeth/__init__.py under gitignored paths" in result.stdout


def test_source_copy_in_lane_notes_is_counted_for_the_tree_that_holds_it(repo: Path) -> None:
    copy = repo / ".claude" / "lanes" / "run" / "snapshot" / "src" / "elspeth" / "__init__.py"
    copy.parent.mkdir(parents=True)
    copy.write_text("", encoding="utf-8")
    result = _report(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _row(result.stdout, repo)[5] == "1"
    assert _row(result.stdout, _lane(repo))[5] == "0"


def test_copy_count_is_capped_unless_count_all(repo: Path) -> None:
    for index in range(51):
        copy = _lane(repo) / "snap" / f"v{index}" / "src" / "elspeth" / "__init__.py"
        copy.parent.mkdir(parents=True)
        copy.write_text("", encoding="utf-8")
    capped = _report(repo)
    assert capped.returncode == 1, capped.stdout + capped.stderr
    assert _row(capped.stdout, _lane(repo))[5] == "50+"
    exact = _report(repo, "--count-all")
    assert exact.returncode == 1, exact.stdout + exact.stderr
    assert _row(exact.stdout, _lane(repo))[5] == "51"


def test_lanes_over_ten_gigabytes_warns(repo: Path) -> None:
    lanes = _lane(repo) / ".claude" / "lanes" / "run"
    lanes.mkdir(parents=True)
    with (lanes / "store.db").open("wb") as handle:
        handle.truncate(11 * 1024**3)  # sparse: apparent size is what the report measures
    result = _report(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _row(result.stdout, _lane(repo))[6] == ">10.0G"
    assert f"[WARN] {_lane(repo)} — .claude/lanes exceeds 10.0G" in result.stdout


def test_failed_git_status_is_unknown_and_warns(repo: Path) -> None:
    (_lane(repo) / ".git").write_text("gitdir: /nonexistent/worktree\n", encoding="utf-8")
    result = _report(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _row(result.stdout, _lane(repo))[0] == "UNKNOWN"
    assert f"[WARN] {_lane(repo)} — git status failed" in result.stdout


def test_missing_worktree_is_reported_without_warning(repo: Path) -> None:
    subprocess.run(["rm", "-rf", str(_lane(repo))], check=True)
    result = _report(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _row(result.stdout, _lane(repo))[0] == "MISSING"


def test_report_does_not_touch_any_index(repo: Path) -> None:
    (_lane(repo) / "src" / "elspeth" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    indexes = [repo / ".git" / "index", repo / ".git" / "worktrees" / "lane" / "index"]
    before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in indexes]
    _report(repo)
    assert [(path.stat().st_mtime_ns, path.read_bytes()) for path in indexes] == before


def test_unknown_option_is_a_usage_error(repo: Path) -> None:
    result = _report(repo, "--execute")
    assert result.returncode == 2
    assert "unknown option: --execute" in result.stderr
