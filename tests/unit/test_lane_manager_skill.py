"""Contract tests for the lane-manager skill mechanism.

Two lanes are simulated with real temporary git repositories — never mocks:

* a **lying** lane that reports "done" while its branch carries no commits,
  or commits that do not touch the expected files, or commits whose tests fail;
* an **idle-but-alive** lane whose agent is still listed by ListAgents (or
  whose worktree is still moving) and must NOT be declared dead.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "lane-manager"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "lane_manager.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lane_manager_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lm = _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"},
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _make_state(repo: Path, tmp_path: Path, *, worktree: str = ".claude/worktrees/lane-t1") -> tuple[Path, object]:
    state_path = tmp_path / "lanes" / "run.json"
    state = lm.init_run(
        state_path=state_path,
        run_id="run",
        repo=repo,
        base_ref="main",
        tickets=[
            {
                "ticket": "t1",
                "title": "touch target",
                "files": ["src/target.py"],
                "test_command": f"{sys.executable} -c \"import sys; sys.exit(0 if open('src/target.py').read().strip() == 'VALUE = 2' else 1)\"",
                "worktree": worktree,
            }
        ],
    )
    return state_path, state


# ----------------------------------------------------------------- state file


def test_init_records_expected_artifact_and_dispatch_stamp(repo: Path, tmp_path: Path) -> None:
    state_path, state = _make_state(repo, tmp_path)
    lane = state.lanes[0]
    assert lane.lane_id == "lane-01-t1"
    assert lane.expected.branch == "lane/t1"
    assert lane.expected.files == ["src/target.py"]
    assert lane.status == lm.STATUS_PENDING
    lm.record_dispatch(state, lane.lane_id, "lane-01-t1", dry_run=False)
    state.save(state_path)
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    row = on_disk["lanes"][0]
    assert row["dispatched_at"] is not None
    assert row["status"] == lm.STATUS_DISPATCHED
    assert row["attempts"][0]["agent_name"] == "lane-01-t1"
    assert row["expected"]["test_command"].startswith(sys.executable)


# ----------------------------------------------------------------- lying lane


def test_lying_lane_with_no_branch_is_not_landed(repo: Path, tmp_path: Path) -> None:
    """The lane says 'done'. There is no branch. verify must say NOT landed."""
    _, state = _make_state(repo, tmp_path)
    lm.record_dispatch(state, "lane-01-t1", "lane-01-t1", dry_run=False)
    result = lm.verify_lane(state, "lane-01-t1")
    assert result.landed is False
    assert result.branch_exists is False
    assert result.test_ran is False
    assert any("does not exist" in r for r in result.reasons)
    assert state.lanes[0].status == lm.STATUS_DISPATCHED


def test_lying_lane_with_empty_branch_is_not_landed(repo: Path, tmp_path: Path) -> None:
    _git(repo, "branch", "lane/t1")
    _, state = _make_state(repo, tmp_path)
    result = lm.verify_lane(state, "lane-01-t1")
    assert result.landed is False
    assert result.branch_exists is True
    assert result.commits_ahead == []
    assert any("no commits ahead" in r for r in result.reasons)


def test_lying_lane_that_changed_the_wrong_file_is_not_landed(repo: Path, tmp_path: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "lane/t1")
    (repo / "src" / "other.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "claims to fix target")
    _git(repo, "checkout", "-q", "main")
    _, state = _make_state(repo, tmp_path)
    result = lm.verify_lane(state, "lane-01-t1")
    assert result.landed is False
    assert result.changed_files == ["src/other.py"]
    assert result.missing_expected_files == ["src/target.py"]
    assert result.test_ran is False, "tests must not be credited when the artifact is wrong"


def test_lane_whose_tests_fail_is_not_landed(repo: Path, tmp_path: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "lane/t1")
    (repo / "src" / "target.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "wrong value")
    _git(repo, "checkout", "-q", "main")
    _, state = _make_state(repo, tmp_path, worktree="nonexistent-worktree")
    # No worktree: tests run in the repo checkout, which is main (VALUE = 1) → exit 1.
    result = lm.verify_lane(state, "lane-01-t1")
    assert result.branch_exists and result.commits_ahead and not result.missing_expected_files
    assert result.test_ran is True
    assert result.test_exit_code == 1
    assert result.landed is False


def test_honest_lane_lands_only_from_evidence(repo: Path, tmp_path: Path) -> None:
    worktree = repo / ".claude" / "worktrees" / "lane-t1"
    _git(repo, "worktree", "add", "-q", "-b", "lane/t1", str(worktree), "main")
    (worktree / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(worktree, "commit", "-q", "-am", "fix target")
    state_path, state = _make_state(repo, tmp_path)
    lm.record_dispatch(state, "lane-01-t1", "lane-01-t1", dry_run=False)
    result = lm.verify_lane(state, "lane-01-t1")
    assert result.landed is True
    assert result.changed_files == ["src/target.py"]
    assert result.test_exit_code == 0
    assert state.lanes[0].status == lm.STATUS_LANDED
    assert state.lanes[0].attempts[-1].outcome == "landed"
    state.save(state_path)
    assert lm.RunState.load(state_path).lanes[0].landed_at is not None


# ----------------------------------------------------------------- idle lane


def test_idle_but_listed_agent_is_alive(repo: Path, tmp_path: Path) -> None:
    _, state = _make_state(repo, tmp_path, worktree="nonexistent-worktree")
    verdict = lm.idle_verdict(state, "lane-01-t1", listed=True)
    assert verdict.verdict == lm.VERDICT_ALIVE_LISTED
    assert verdict.worktree_exists is False
    assert state.lanes[0].idle_checks[-1]["listed_by_list_agents"] is True


def test_idle_unlisted_agent_with_moving_worktree_is_alive(repo: Path, tmp_path: Path) -> None:
    worktree = repo / ".claude" / "worktrees" / "lane-t1"
    _git(repo, "worktree", "add", "-q", "-b", "lane/t1", str(worktree), "main")
    (worktree / "src" / "target.py").write_text("VALUE = 2  # in progress\n", encoding="utf-8")
    _, state = _make_state(repo, tmp_path)
    verdict = lm.idle_verdict(state, "lane-01-t1", listed=False, activity_window=3600)
    assert verdict.verdict == lm.VERDICT_ALIVE_WORKTREE
    assert verdict.uncommitted_changes == 1


def test_idle_is_dead_only_when_both_checks_fail(repo: Path, tmp_path: Path) -> None:
    worktree = repo / ".claude" / "worktrees" / "lane-t1"
    _git(repo, "worktree", "add", "-q", "-b", "lane/t1", str(worktree), "main")
    stale = time.time() - 7200
    for path in worktree.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (stale, stale))
    _, state = _make_state(repo, tmp_path)
    verdict = lm.idle_verdict(state, "lane-01-t1", listed=False, activity_window=60)
    assert verdict.verdict == lm.VERDICT_DEAD
    assert verdict.uncommitted_changes == 0
    assert verdict.seconds_since_activity is not None and verdict.seconds_since_activity > 60


# ----------------------------------------------------------------- ladder


def test_escalation_ladder_nudge_redispatch_block_then_continues(repo: Path, tmp_path: Path) -> None:
    state_path, state = _make_state(repo, tmp_path)
    tickets_extra = state.lanes[0]
    # second lane so "continue with remaining lanes" is observable
    state.lanes.append(
        lm.Lane(
            lane_id="lane-02-t2",
            ticket="t2",
            title="other",
            expected=lm.Expected(branch="lane/t2", files=["src/x.py"], test_command="true"),
            worktree="wt2",
        )
    )
    lm.record_dispatch(state, tickets_extra.lane_id, "lane-01-t1", dry_run=False)
    lm.verify_lane(state, "lane-01-t1")  # no branch → evidence recorded

    assert lm.escalate(state, "lane-01-t1", reason="claimed done; no branch") == lm.ACTION_NUDGE
    assert state.lanes[0].status == lm.STATUS_NUDGED
    assert lm.escalate(state, "lane-01-t1", reason="still no branch") == lm.ACTION_REDISPATCH
    assert state.lanes[0].status == lm.STATUS_REDISPATCHED
    lm.record_dispatch(state, "lane-01-t1", "lane-01-t1-retry", dry_run=False)
    assert len(state.lanes[0].attempts) == 2
    assert lm.escalate(state, "lane-01-t1", reason="redispatch produced nothing") == lm.ACTION_BLOCK
    blocked = state.lanes[0]
    assert blocked.status == lm.STATUS_BLOCKED
    assert blocked.blocked is not None
    assert blocked.blocked["reason"] == "redispatch produced nothing"
    assert blocked.blocked["last_verification"]["landed"] is False
    # further escalation is idempotent — no fourth rung, no reset
    assert lm.escalate(state, "lane-01-t1", reason="again") == lm.ACTION_BLOCK
    # the other lane is untouched and still workable
    assert state.lanes[1].status == lm.STATUS_PENDING
    state.save(state_path)
    reloaded = lm.RunState.load(state_path)
    assert reloaded.lanes[0].escalation == {"nudged": True, "redispatched": True}


def test_real_dispatch_after_dry_run_only_resets_the_ladder(repo: Path, tmp_path: Path) -> None:
    _, state = _make_state(repo, tmp_path)
    lm.record_dispatch(state, "lane-01-t1", None, dry_run=True)
    for _ in range(3):
        lm.escalate(state, "lane-01-t1", reason="rehearsal")
    assert state.lanes[0].status == lm.STATUS_BLOCKED
    lm.record_dispatch(state, "lane-01-t1", "lane-01-t1", dry_run=False)
    lane = state.lanes[0]
    assert lane.status == lm.STATUS_DISPATCHED
    assert lane.blocked is None
    assert lane.escalation == {"nudged": False, "redispatched": False}
    assert len(lane.attempts) == 2, "dry-run attempts stay as history"
    assert lm.escalate(state, "lane-01-t1", reason="real") == lm.ACTION_NUDGE
    # a second real dispatch (the re-dispatch rung) must NOT reset again
    lm.escalate(state, "lane-01-t1", reason="real")
    lm.record_dispatch(state, "lane-01-t1", "lane-01-t1-retry", dry_run=False)
    assert lane.escalation == {"nudged": True, "redispatched": True}


def test_landed_lane_cannot_be_escalated(repo: Path, tmp_path: Path) -> None:
    _, state = _make_state(repo, tmp_path)
    state.lanes[0].status = lm.STATUS_LANDED
    with pytest.raises(ValueError, match="landed"):
        lm.escalate(state, "lane-01-t1", reason="x")


# ----------------------------------------------------------------- report


def test_report_lists_landed_blocked_and_merge_order(repo: Path, tmp_path: Path) -> None:
    worktree = repo / ".claude" / "worktrees" / "lane-t1"
    _git(repo, "worktree", "add", "-q", "-b", "lane/t1", str(worktree), "main")
    (worktree / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(worktree, "commit", "-q", "-am", "fix target")
    _, state = _make_state(repo, tmp_path)
    state.lanes.append(
        lm.Lane(
            lane_id="lane-02-t2",
            ticket="t2",
            title="liar",
            expected=lm.Expected(branch="lane/t2", files=["src/target.py"], test_command="true"),
            worktree="wt2",
        )
    )
    lm.record_dispatch(state, "lane-01-t1", "a", dry_run=False)
    lm.record_dispatch(state, "lane-02-t2", "b", dry_run=False)
    assert lm.verify_lane(state, "lane-01-t1").landed
    assert not lm.verify_lane(state, "lane-02-t2").landed
    lm.idle_verdict(state, "lane-02-t2", listed=False, activity_window=1)
    for _ in range(3):
        lm.escalate(state, "lane-02-t2", reason="no branch after redispatch")

    report = lm.render_report(state)
    assert "## Landed (1)" in report
    assert "## Blocked (1)" in report
    assert "lane-02-t2" in report and "branch lane/t2 does not exist" in report
    assert "→ dead" in report
    assert "1. `lane/t1` (t1)" in report
    assert "lane/t2" not in report.split("## Merge order proposal")[1]
    assert "DRY RUN" not in report


def test_merge_order_puts_disjoint_lanes_first() -> None:
    def landed(lane_id: str, files: list[str], when: str) -> object:
        return lm.Lane(
            lane_id=lane_id,
            ticket=lane_id,
            title="",
            expected=lm.Expected(branch=f"lane/{lane_id}", files=files, test_command="true"),
            worktree="",
            status=lm.STATUS_LANDED,
            landed_at=when,
        )

    lanes = [
        landed("a", ["x.py", "shared.py"], "2026-01-01T00:00:01"),
        landed("b", ["y.py"], "2026-01-01T00:00:02"),
        landed("c", ["shared.py"], "2026-01-01T00:00:00"),
    ]
    assert [lane.lane_id for lane in lm.merge_order(lanes)] == ["b", "c", "a"]


# ----------------------------------------------------------------- CLI + skill doc


def test_cli_round_trip_dry_run(repo: Path, tmp_path: Path) -> None:
    tickets = tmp_path / "tickets.json"
    tickets.write_text(
        json.dumps([{"ticket": "t1", "title": "x", "files": ["src/target.py"], "test_command": "true"}]),
        encoding="utf-8",
    )
    state = tmp_path / "lanes" / "cli.json"
    base = [sys.executable, str(SCRIPT)]
    run = lambda *a: subprocess.run([*base, *a], capture_output=True, text=True, check=False)  # noqa: E731
    assert (
        run(
            "init", "--state", str(state), "--run-id", "cli", "--repo", str(repo), "--base-ref", "main", "--tickets", str(tickets)
        ).returncode
        == 0
    )
    assert run("dispatch", "--state", str(state), "--lane", "lane-01-t1", "--dry-run").returncode == 0
    verify = run("verify", "--state", str(state), "--lane", "lane-01-t1")
    assert verify.returncode == 1
    assert json.loads(verify.stdout)["landed"] is False
    assert run("idle", "--state", str(state), "--lane", "lane-01-t1", "--not-listed").returncode == 1
    actions = [
        json.loads(run("escalate", "--state", str(state), "--lane", "lane-01-t1", "--reason", "dry").stdout)["action"] for _ in range(3)
    ]
    assert actions == [lm.ACTION_NUDGE, lm.ACTION_REDISPATCH, lm.ACTION_BLOCK]
    report = run("report", "--state", str(state))
    assert report.returncode == 0
    assert "**DRY RUN**" in report.stdout
    assert "## Blocked (1)" in report.stdout


def test_skill_doc_names_every_mechanism_and_the_ladder() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\nname: lane-manager\n")
    for token in (
        "ListAgents",
        "git diff",
        "git log",
        ".claude/lanes/",
        "verify",
        "idle",
        "escalate",
        "report",
        "nudge",
        "redispatch",
        "block",
        "merge-order",
    ):
        assert token in text, token
    for phrase in ("ListAgents", "worktree", "dead"):
        assert phrase in text.split("## Phase 4")[1].split("## Phase 5")[0]
