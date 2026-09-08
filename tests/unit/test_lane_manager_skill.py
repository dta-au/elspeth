"""Contract tests for the crash-durable lane-manager skill mechanism.

Every scenario uses a real temporary git repository and real subprocesses —
never mocks. The mechanism under test is ``.agents/skills/lane-manager/lane_manager.py``:

* per-lane ``.lanes/<lane-id>/{plan.md,status.json,report.md}`` as the durable
  source of truth (state: pending|running|verified|failed|merged);
* liveness from a heartbeat + worktree activity, and ``resume`` that re-dispatches
  only lanes killed mid-flight (a simulated SIGKILL proves it);
* ``verify`` that advances to ``verified`` only when the lane's test is RED on
  the base, GREEN on the merged tree, and the full suite passes there;
* ``merge`` that is serial and gated on a verification against the CURRENT base.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "lane-manager"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "lane_manager.py"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lane_manager_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lm = _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=GIT_ENV).stdout


# A tiny "project": src/target.py is the thing lanes fix; tests/run_all.py is the
# full suite (runs every tests/test_*.py); tests/test_existing.py guards src/other.py.
TEST_CMD = f"{sys.executable} tests/test_target.py"
SUITE_CMD = f"{sys.executable} tests/run_all.py"
GOOD_TEST = "assert open('src/target.py').read().strip() == 'VALUE = 2'\n"  # RED is a FAILED ASSERTION, visible in the output
FAKE_TEST = "import sys\nsys.exit(0)\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    (root / "tests" / "test_existing.py").write_text(
        "import sys\nsys.exit(0 if open('src/other.py').read().strip() == 'OTHER = 1' else 1)\n", encoding="utf-8"
    )
    (root / "tests" / "run_all.py").write_text(
        textwrap.dedent(
            f"""\
            import glob, subprocess, sys
            rc = 0
            for path in sorted(glob.glob('tests/test_*.py')):
                rc |= subprocess.run([{sys.executable!r}, path]).returncode
            sys.exit(rc)
            """
        ),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".lanes/\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _ticket(ticket: str = "t1", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ticket": ticket,
        "title": "make target VALUE = 2",
        "description": "src/target.py must read VALUE = 2",
        "files": ["src/target.py"],
        "test_files": ["tests/test_target.py"],
        "test_command": TEST_CMD,
    }
    base.update(overrides)
    return base


def _init(repo: Path, *tickets: dict[str, object], run_id: str = "run", suite_command: str = SUITE_CMD) -> object:
    return lm.init_run(
        lanes_dir=repo / ".lanes",
        run_id=run_id,
        repo=repo,
        base_ref="main",
        suite_command=suite_command,
        tickets=list(tickets) or [_ticket()],
    )


def _worker_commits(worktree: Path, *, test_body: str = GOOD_TEST, value: str = "VALUE = 2\n", break_other: bool = False) -> None:
    """What an honest lane does: commit the failing test, then commit the fix."""
    (worktree / "tests" / "test_target.py").write_text(test_body, encoding="utf-8")
    _git(worktree, "add", "tests/test_target.py")
    _git(worktree, "commit", "-q", "-m", "test: target must be 2 (red)")
    (worktree / "src" / "target.py").write_text(value, encoding="utf-8")
    if break_other:
        (worktree / "src" / "other.py").write_text("OTHER = 9\n", encoding="utf-8")
    _git(worktree, "add", "src")
    _git(worktree, "commit", "-q", "-m", "fix: target = 2 (green)")


def _worktrees(repo: Path) -> list[str]:
    return [line.split(" ", 1)[1] for line in _git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


# ----------------------------------------------------------------- durable layout


def test_init_writes_plan_status_and_run_manifest(repo: Path) -> None:
    run = _init(repo)
    lane_dir = repo / ".lanes" / "lane-01-t1"
    assert (lane_dir / "plan.md").is_file()
    assert (lane_dir / "status.json").is_file()
    status = json.loads((lane_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == lm.STATE_PENDING
    assert status["lane_id"] == "lane-01-t1"
    assert status["run_id"] == "run"
    assert status["branch"] == "lane/t1"
    assert status["expected"]["files"] == ["src/target.py"]
    assert status["expected"]["test_files"] == ["tests/test_target.py"]
    assert status["heartbeat_at"] is None
    plan = (lane_dir / "plan.md").read_text(encoding="utf-8")
    for token in ("src/target.py", "tests/test_target.py", TEST_CMD, "make target VALUE = 2", "report.md"):
        assert token in plan, token
    manifest = json.loads((repo / ".lanes" / "run.run.json").read_text(encoding="utf-8"))
    assert manifest["base_ref"] == "main"
    assert manifest["suite_command"] == SUITE_CMD
    assert manifest["lane_ids"] == ["lane-01-t1"]
    assert run.lane("lane-01-t1").state == lm.STATE_PENDING


def test_status_save_is_atomic_and_round_trips(repo: Path) -> None:
    run = _init(repo)
    lane = run.lane("lane-01-t1")
    lane.state = lm.STATE_RUNNING
    lane.save()
    assert not list((repo / ".lanes" / "lane-01-t1").glob("*.tmp")), "no torn temp file may survive a save"
    reloaded = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1")
    assert reloaded.state == lm.STATE_RUNNING
    assert reloaded.expected.test_command == TEST_CMD


def test_brief_tells_the_worker_where_to_write_and_to_return_one_line(repo: Path) -> None:
    run = _init(repo)
    brief = lm.render_brief(run, run.lane("lane-01-t1"))
    worktree = run.lane("lane-01-t1").worktree_path
    assert str(worktree) in brief
    assert "lane/t1" in brief
    assert str(repo / ".lanes" / "lane-01-t1" / "report.md") in brief
    assert "heartbeat" in brief and "lane-01-t1" in brief
    assert "one line" in brief.lower()
    assert "failing test" in brief.lower() and "before" in brief.lower()
    assert "assertion" in brief.lower() and "find_spec" in brief, "RED is a failed assertion; a module the fix adds is asserted present"
    assert TEST_CMD in brief


# ----------------------------------------------------------------- dispatch / heartbeat / liveness


def test_dispatch_creates_the_worktree_and_marks_running(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="lane-01-t1")
    assert lane.state == lm.STATE_RUNNING
    assert lane.heartbeat_at is not None
    assert lane.worktree_path.is_dir()
    assert _git(lane.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "lane/t1"
    assert str(lane.worktree_path) in _worktrees(repo)
    on_disk = json.loads((repo / ".lanes" / "lane-01-t1" / "status.json").read_text(encoding="utf-8"))
    assert on_disk["state"] == lm.STATE_RUNNING
    assert on_disk["attempts"][0]["agent_name"] == "lane-01-t1"
    # idempotent: a re-dispatch reuses the same worktree and branch
    again = lm.dispatch(run, "lane-01-t1", agent_name="lane-01-t1-retry")
    assert len(again.attempts) == 2
    assert _worktrees(repo).count(str(lane.worktree_path)) == 1


def test_heartbeat_moves_the_stamp(repo: Path) -> None:
    run = _init(repo)
    lm.dispatch(run, "lane-01-t1", agent_name="a")
    first = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1").heartbeat_at
    time.sleep(1.1)
    lm.heartbeat(repo / ".lanes", "lane-01-t1")
    second = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1").heartbeat_at
    assert second is not None and first is not None and second > first


def _age_everything(lane: object, seconds: float) -> None:
    """Backdate the heartbeat and every worktree file so the lane looks abandoned."""
    stale = time.time() - seconds
    lane.heartbeat_at = lm.stamp(stale)
    lane.save()
    for path in lane.worktree_path.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (stale, stale))


def test_liveness_listed_or_fresh_heartbeat_or_moving_worktree_is_alive(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    assert lm.liveness(run, "lane-01-t1", listed=True, window=1).verdict == lm.LIVE_LISTED
    assert lm.liveness(run, "lane-01-t1", listed=False, window=3600).verdict == lm.LIVE_HEARTBEAT
    _age_everything(lane, 7200)
    (lane.worktree_path / "src" / "target.py").write_text("VALUE = 2  # editing\n", encoding="utf-8")
    verdict = lm.liveness(run, "lane-01-t1", listed=False, window=3600)
    assert verdict.verdict == lm.LIVE_WORKTREE
    assert verdict.uncommitted_changes == 1


def test_liveness_is_dead_only_when_every_signal_is_stale(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    (lane.worktree_path / "src" / "target.py").write_text("VALUE = 2  # half done\n", encoding="utf-8")
    _age_everything(lane, 7200)
    verdict = lm.liveness(run, "lane-01-t1", listed=False, window=60)
    assert verdict.verdict == lm.DEAD
    assert verdict.uncommitted_changes == 1, "uncommitted edits are evidence of a kill, not of life"
    assert verdict.heartbeat_age is not None and verdict.heartbeat_age > 60


# ----------------------------------------------------------------- resume


def test_resume_redispatches_only_lanes_killed_mid_flight(repo: Path) -> None:
    run = _init(repo, _ticket("t1"), _ticket("t2"), _ticket("t3"), _ticket("t4"))
    killed = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _age_everything(killed, 7200)
    lm.dispatch(run, "lane-02-t2", agent_name="b")  # fresh heartbeat: alive
    verified = run.lane("lane-03-t3")
    verified.state = lm.STATE_VERIFIED
    verified.save()
    # lane-04-t4 stays pending: never dispatched, not resume's business

    plan = lm.resume(run, window=60, listed=set())
    assert [lane.lane_id for lane in plan.killed] == ["lane-01-t1"]
    assert [lane.lane_id for lane in plan.alive] == ["lane-02-t2"]
    assert [lane.lane_id for lane in plan.pending] == ["lane-04-t4"]
    assert [lane.lane_id for lane in plan.terminal] == ["lane-03-t3"]

    redispatched = lm.resume(run, window=60, listed=set(), redispatch=True)
    lane = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1")
    assert lane.state == lm.STATE_RUNNING
    assert lane.attempts[-1].resumed is True
    assert len(lane.attempts) == 2
    assert lane.heartbeat_at is not None and lm.age_of(lane.heartbeat_at) < 60
    assert "lane-01-t1" in redispatched.briefs and str(lane.worktree_path) in redispatched.briefs["lane-01-t1"]
    assert "lane-02-t2" not in redispatched.briefs
    # a lane the harness still lists is alive even with a stale heartbeat; a re-dispatched one is only alive while fresh
    _age_everything(lm.LaneStatus.load(repo / ".lanes", "lane-01-t1"), 7200)
    _age_everything(lm.LaneStatus.load(repo / ".lanes", "lane-02-t2"), 7200)
    later = lm.resume(run, window=60, listed={"lane-02-t2"})
    assert [lane.lane_id for lane in later.killed] == ["lane-01-t1"]
    assert [lane.lane_id for lane in later.alive] == ["lane-02-t2"]


WORKER = textwrap.dedent(
    """\
    import subprocess, sys, time
    script, lanes_dir, lane_id, worktree, mode = sys.argv[1:6]
    def beat():
        subprocess.run([sys.executable, script, "heartbeat", "--lanes-dir", lanes_dir, "--lane", lane_id], check=True)
    beat()
    open(f"{worktree}/tests/test_target.py", "w").write(
        "assert open('src/target.py').read().strip() == 'VALUE = 2'\\n")
    open(f"{worktree}/progress", "w").write("test written\\n")
    if mode == "crash":
        beat()
        while True:          # the lane is 'working' when it gets SIGKILLed
            time.sleep(0.2)
    git = lambda *a: subprocess.run(["git", "-C", worktree, *a], check=True)
    git("add", "tests/test_target.py"); git("commit", "-q", "-m", "test: red")
    open(f"{worktree}/src/target.py", "w").write("VALUE = 2\\n")
    git("add", "src/target.py"); git("commit", "-q", "-m", "fix: green")
    beat()
    open(f"{lanes_dir}/{lane_id}/report.md", "w").write("# lane-01-t1\\n\\nResumed after crash; red test then fix committed.\\n")
    """
)


def test_simulated_crash_then_resume_recovers_the_lane(repo: Path, tmp_path: Path) -> None:
    """Kill a real worker process mid-flight; resume must find exactly that lane and a fresh worker must land it."""
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="worker-1")
    argv = [sys.executable, str(worker), str(SCRIPT), str(repo / ".lanes"), "lane-01-t1", str(lane.worktree_path)]

    proc = subprocess.Popen([*argv, "crash"], env=GIT_ENV)  # the interpreter itself, so the kill hits the worker
    deadline = time.time() + 20
    while not (lane.worktree_path / "progress").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert (lane.worktree_path / "progress").exists(), "worker never started working"
    os.kill(proc.pid, signal.SIGKILL)
    assert proc.wait(timeout=10) == -signal.SIGKILL

    crashed = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1")
    assert crashed.state == lm.STATE_RUNNING, "a killed lane still says running on disk — that is the crash signature"
    assert crashed.heartbeat_at is not None
    _age_everything(crashed, 7200)  # the kill happened 'two hours ago'

    plan = lm.resume(lm.Run.load(repo / ".lanes", "run"), window=60, listed=set(), redispatch=True)
    assert [lane.lane_id for lane in plan.killed] == ["lane-01-t1"]
    assert lm.LaneStatus.load(repo / ".lanes", "lane-01-t1").attempts[-1].resumed is True

    subprocess.run([*argv, "finish"], env=GIT_ENV, check=True, timeout=60)  # the re-dispatched worker
    verification = lm.verify(lm.Run.load(repo / ".lanes", "run"), "lane-01-t1")
    assert verification.reasons == []
    assert verification.verified is True
    final = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1")
    assert final.state == lm.STATE_VERIFIED
    assert (repo / ".lanes" / "lane-01-t1" / "report.md").read_text(encoding="utf-8").startswith("# lane-01-t1")
    assert lm.summary_line(repo / ".lanes", "lane-01-t1") == "Resumed after crash; red test then fix committed."


# ----------------------------------------------------------------- verify: red → green → suite


def test_honest_lane_is_verified_with_red_green_and_suite_evidence(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path)
    result = lm.verify(run, "lane-01-t1")
    assert result.verified is True, result.reasons
    assert result.red_exit_code not in (0, None)
    assert result.green_exit_code == 0
    assert result.suite_exit_code == 0
    assert result.base_sha == _git(repo, "rev-parse", "main").strip()
    assert result.branch_sha == _git(repo, "rev-parse", "lane/t1").strip()
    assert sorted(result.changed_files) == ["src/target.py", "tests/test_target.py"]
    status = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1")
    assert status.state == lm.STATE_VERIFIED
    assert status.verified_at is not None
    assert status.verifications[-1]["verified"] is True
    assert set(_worktrees(repo)) == {str(repo), str(lane.worktree_path)}, "temporary verification worktrees must be removed"


def test_lane_whose_test_passes_without_the_fix_is_not_verified(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path, test_body=FAKE_TEST)
    result = lm.verify(run, "lane-01-t1")
    assert result.verified is False
    assert result.red_exit_code == 0
    assert any("does not fail" in r for r in result.reasons), result.reasons
    assert result.green_exit_code is None, "green and suite are not credited once red is disproved"
    assert lm.LaneStatus.load(repo / ".lanes", "lane-01-t1").state == lm.STATE_FAILED


def test_lane_whose_fix_breaks_the_suite_is_not_verified(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path, break_other=True)
    result = lm.verify(run, "lane-01-t1")
    assert result.red_exit_code not in (0, None) and result.green_exit_code == 0
    assert result.suite_exit_code not in (0, None)
    assert result.verified is False
    assert any("suite" in r for r in result.reasons)


def test_lane_whose_test_fails_on_the_merged_tree_is_not_verified_even_if_the_suite_passes(repo: Path) -> None:
    """The GREEN gate stands on its own: a suite that does not select the lane's test must not cover for it (reviewer M11)."""
    run = _init(repo, suite_command=f"{sys.executable} tests/test_existing.py")
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path, test_body="assert open('src/target.py').read().strip() == 'VALUE = 3'\n")
    result = lm.verify(run, "lane-01-t1")
    assert result.red_exit_code == 1
    assert result.green_exit_code == 1
    assert result.suite_exit_code is None, "the suite is not credited once green is disproved"
    assert result.verified is False
    assert any("merged tree" in r for r in result.reasons), result.reasons


def test_lane_whose_test_crashes_on_the_base_is_not_verified(repo: Path) -> None:
    """RED means the test FAILED (exit 1). A test that cannot fail but crashes without the fix must not pass the gate (reviewer R1)."""
    run = _init(
        repo,
        _ticket(
            "t1",
            files=["src/target.py", "src/helper.py"],
            test_command=f"{sys.executable} -m pytest tests/test_target.py -q -p no:cacheprovider",
        ),
    )
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    (lane.worktree_path / "tests" / "test_target.py").write_text(
        "import sys\n\nsys.path.insert(0, 'src')\nimport helper  # noqa: E402,F401\n\n\ndef test_nothing():\n    assert True\n",
        encoding="utf-8",
    )
    _git(lane.worktree_path, "add", "tests/test_target.py")
    _git(lane.worktree_path, "commit", "-q", "-m", "test: asserts nothing, imports the new helper")
    (lane.worktree_path / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (lane.worktree_path / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(lane.worktree_path, "add", "src")
    _git(lane.worktree_path, "commit", "-q", "-m", "fix: adds helper")
    result = lm.verify(run, "lane-01-t1")
    assert result.red_exit_code not in (0, 1, None), result.red_exit_code
    assert result.verified is False
    assert any("crash" in r for r in result.reasons), result.reasons
    assert result.green_exit_code is None


EXIT_1_CRASH_SHAPES = {
    "plain-script": ("import helper  # noqa: F401\n", f"{sys.executable} tests/test_target.py"),
    "pytest-in-body-import": (
        "def test_nothing():\n    import helper  # noqa: F401\n",
        f"{sys.executable} -m pytest tests/test_target.py -q -p no:cacheprovider",
    ),
    "unittest": (
        "import unittest\n\nimport helper  # noqa: F401\n\n\nclass T(unittest.TestCase):\n    def test_nothing(self):\n        pass\n",
        f"{sys.executable} -m unittest tests/test_target.py",
    ),
}


def _lane_with_new_module(repo: Path, test_body: str, command: str) -> tuple[object, object]:
    run = _init(repo, _ticket("t1", files=["src/target.py", "src/helper.py"], test_command=command))
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    (lane.worktree_path / "tests" / "test_target.py").write_text(test_body, encoding="utf-8")
    _git(lane.worktree_path, "add", "tests/test_target.py")
    _git(lane.worktree_path, "commit", "-q", "-m", "test: imports the new helper")
    (lane.worktree_path / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (lane.worktree_path / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(lane.worktree_path, "add", "src")
    _git(lane.worktree_path, "commit", "-q", "-m", "fix: adds helper")
    return run, lane


@pytest.mark.parametrize("shape", sorted(EXIT_1_CRASH_SHAPES))
def test_lane_whose_test_dies_with_exit_1_but_no_failed_assertion_is_not_verified(repo: Path, shape: str) -> None:
    """Every common runner reports an uncaught exception inside a test as exit 1 (round two): the exit code alone cannot
    tell a failed assertion from a crash. RED is a failed assertion VISIBLE in the output."""
    body, command = EXIT_1_CRASH_SHAPES[shape]
    run, _ = _lane_with_new_module(repo, body, command)
    result = lm.verify(run, "lane-01-t1")
    assert result.red_exit_code == 1, (result.red_exit_code, result.red_output_tail)
    assert result.verified is False
    assert any("crash" in r and "assertion" in r for r in result.reasons), result.reasons
    assert result.green_exit_code is None


def test_lane_whose_test_asserts_the_new_module_exists_is_verified(repo: Path) -> None:
    """The honest shape for a fix that adds a module: assert its presence, so the base run fails at an assertion."""
    body = (
        "import importlib.util\n\n\ndef test_helper_exists():\n    assert importlib.util.find_spec('helper') is not None\n"
        "    import helper\n\n    assert helper.HELPED\n"
    )
    run, _ = _lane_with_new_module(repo, body, f"{sys.executable} -m pytest tests/test_target.py -q -p no:cacheprovider")
    result = lm.verify(run, "lane-01-t1")
    assert result.verified is True, result.reasons
    assert result.red_exit_code == 1 and result.green_exit_code == 0 and result.suite_exit_code == 0


def test_lane_that_never_wrote_a_test_is_not_verified(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    (lane.worktree_path / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(lane.worktree_path, "commit", "-q", "-am", "fix without a test")
    result = lm.verify(run, "lane-01-t1")
    assert result.verified is False
    assert result.missing_test_files == ["tests/test_target.py"]
    assert result.red_exit_code is None, "no red run without a test to run"


def test_lane_that_lies_about_the_artifact_is_not_verified(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    (lane.worktree_path / "src" / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
    _git(lane.worktree_path, "add", ".")
    _git(lane.worktree_path, "commit", "-q", "-m", "claims to fix target")
    result = lm.verify(run, "lane-01-t1")
    assert result.verified is False
    assert result.missing_expected_files == ["src/target.py"]
    assert result.red_exit_code is None


def test_lane_that_conflicts_with_base_is_not_verified_and_leaves_no_worktree(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path)
    (repo / "src" / "target.py").write_text("VALUE = 7\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "base moved on the same line")
    result = lm.verify(run, "lane-01-t1")
    assert result.verified is False
    assert any("conflict" in r for r in result.reasons), result.reasons
    assert set(_worktrees(repo)) == {str(repo), str(lane.worktree_path)}


# ----------------------------------------------------------------- merge: serial + gated


def test_merge_refuses_an_unverified_lane(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path)
    outcome = lm.merge(run, "lane-01-t1")
    assert outcome.merged is False
    assert any("verified" in r for r in outcome.reasons)
    assert _git(repo, "rev-parse", "main").strip() == run.base_sha()


def test_merge_lands_a_verified_lane_with_no_ff_and_records_the_sha(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path)
    assert lm.verify(run, "lane-01-t1").verified
    outcome = lm.merge(run, "lane-01-t1")
    assert outcome.merged is True, outcome.reasons
    tip = _git(repo, "rev-parse", "main").strip()
    assert outcome.merge_sha == tip
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", "main").split()) == 3, "--no-ff merge commit"
    assert (repo / "src" / "target.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    status = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1")
    assert status.state == lm.STATE_MERGED and status.merge_sha == tip
    manifest = lm.Run.load(repo / ".lanes", "run")
    assert manifest.merges[-1]["lane_id"] == "lane-01-t1"


def test_merge_is_serial_the_second_lane_needs_a_verification_against_the_new_base(repo: Path) -> None:
    run = _init(
        repo,
        _ticket("t1"),
        _ticket("t2", files=["src/other.py"], test_files=["tests/test_other.py"], test_command=f"{sys.executable} tests/test_other.py"),
    )
    a = lm.dispatch(run, "lane-01-t1", agent_name="a")
    b = lm.dispatch(run, "lane-02-t2", agent_name="b")
    _worker_commits(a.worktree_path)
    (b.worktree_path / "tests" / "test_other.py").write_text(
        "assert open('src/other.py').read().strip() == 'OTHER = 2'\n", encoding="utf-8"
    )
    _git(b.worktree_path, "add", "tests")
    _git(b.worktree_path, "commit", "-q", "-m", "test: other (red)")
    (b.worktree_path / "src" / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    (b.worktree_path / "tests" / "test_existing.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    _git(b.worktree_path, "commit", "-q", "-am", "fix: other = 2 (green)")

    assert lm.verify(run, "lane-01-t1").verified
    assert lm.verify(run, "lane-02-t2").verified
    assert lm.merge(run, "lane-01-t1").merged
    stale = lm.merge(run, "lane-02-t2")
    assert stale.merged is False
    assert any("base moved" in r for r in stale.reasons), stale.reasons
    assert lm.LaneStatus.load(repo / ".lanes", "lane-02-t2").state == lm.STATE_VERIFIED, "a stale verification is not a failure"
    run = lm.Run.load(repo / ".lanes", "run")
    again = lm.verify(run, "lane-02-t2")
    assert again.verified and again.base_sha == _git(repo, "rev-parse", "main").strip()
    assert lm.merge(run, "lane-02-t2").merged
    assert (repo / "src" / "target.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "src" / "other.py").read_text(encoding="utf-8") == "OTHER = 2\n"
    assert [m["lane_id"] for m in lm.Run.load(repo / ".lanes", "run").merges] == ["lane-01-t1", "lane-02-t2"]


def test_merge_refuses_when_the_branch_moved_after_verification(repo: Path) -> None:
    run = _init(repo)
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path)
    assert lm.verify(run, "lane-01-t1").verified
    (lane.worktree_path / "src" / "target.py").write_text("VALUE = 2  # sneaky\n", encoding="utf-8")
    _git(lane.worktree_path, "commit", "-q", "-am", "after verification")
    outcome = lm.merge(run, "lane-01-t1")
    assert outcome.merged is False
    assert any("branch moved" in r for r in outcome.reasons), outcome.reasons


# ----------------------------------------------------------------- escalation ladder


def test_escalation_ladder_ends_in_failed_with_evidence(repo: Path) -> None:
    run = _init(repo, _ticket("t1"), _ticket("t2"))
    lm.dispatch(run, "lane-01-t1", agent_name="a")
    lm.verify(run, "lane-01-t1")  # nothing committed → failed with reasons
    assert lm.escalate(run, "lane-01-t1", reason="claimed done; nothing on branch") == lm.ACTION_NUDGE
    assert lm.escalate(run, "lane-01-t1", reason="still nothing") == lm.ACTION_REDISPATCH
    lm.dispatch(run, "lane-01-t1", agent_name="a-retry")
    assert lm.escalate(run, "lane-01-t1", reason="redispatch produced nothing") == lm.ACTION_BLOCK
    lane = lm.LaneStatus.load(repo / ".lanes", "lane-01-t1")
    assert lane.state == lm.STATE_FAILED
    assert lane.blocked is not None and lane.blocked["reason"] == "redispatch produced nothing"
    assert lane.blocked["last_verification"]["verified"] is False
    assert lm.escalate(run, "lane-01-t1", reason="again") == lm.ACTION_BLOCK
    assert lm.LaneStatus.load(repo / ".lanes", "lane-02-t2").state == lm.STATE_PENDING
    with pytest.raises(ValueError, match="verified"):
        other = run.lane("lane-02-t2")
        other.state = lm.STATE_VERIFIED
        other.save()
        lm.escalate(run, "lane-02-t2", reason="x")


# ----------------------------------------------------------------- report + CLI


def test_report_and_merge_order(repo: Path) -> None:
    run = _init(repo, _ticket("t1"), _ticket("t2", files=["src/target.py"]), _ticket("t3", files=["src/other.py"]))
    lane = lm.dispatch(run, "lane-01-t1", agent_name="a")
    _worker_commits(lane.worktree_path)
    assert lm.verify(run, "lane-01-t1").verified
    lm.dispatch(run, "lane-02-t2", agent_name="b")
    for _ in range(3):
        lm.escalate(run, "lane-02-t2", reason="never committed")
    report = lm.render_report(run)
    assert "## Verified (1)" in report and "## Failed (1)" in report and "## Pending (1)" in report
    assert "lane-02-t2" in report and "never committed" in report
    assert "1. `lane/t1`" in report
    assert "lane/t2" not in report.split("## Merge order")[1]


def test_cli_round_trip_including_resume_and_status(repo: Path, tmp_path: Path) -> None:
    tickets = tmp_path / "tickets.json"
    tickets.write_text(json.dumps([_ticket()]), encoding="utf-8")
    lanes = repo / ".lanes"
    base = [sys.executable, str(SCRIPT)]

    def run(*a: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*base, *a], capture_output=True, text=True, check=False, env=GIT_ENV)

    init = run(
        "init",
        "--lanes-dir",
        str(lanes),
        "--run-id",
        "cli",
        "--repo",
        str(repo),
        "--base-ref",
        "main",
        "--suite-command",
        SUITE_CMD,
        "--tickets",
        str(tickets),
    )
    assert init.returncode == 0, init.stderr
    assert json.loads(init.stdout)["lanes"] == ["lane-01-t1"]
    brief = run("brief", "--lanes-dir", str(lanes), "--lane", "lane-01-t1")
    assert brief.returncode == 0 and "report.md" in brief.stdout
    assert run("dispatch", "--lanes-dir", str(lanes), "--lane", "lane-01-t1", "--agent-name", "x").returncode == 0
    assert run("heartbeat", "--lanes-dir", str(lanes), "--lane", "lane-01-t1").returncode == 0
    assert run("liveness", "--lanes-dir", str(lanes), "--lane", "lane-01-t1", "--not-listed").returncode == 0
    verify = run("verify", "--lanes-dir", str(lanes), "--lane", "lane-01-t1")
    assert verify.returncode == 1 and json.loads(verify.stdout)["verified"] is False
    merge = run("merge", "--lanes-dir", str(lanes), "--lane", "lane-01-t1")
    assert merge.returncode == 1 and json.loads(merge.stdout)["merged"] is False
    resume = run("resume", "--lanes-dir", str(lanes), "--run-id", "cli", "--window", "3600")
    assert resume.returncode == 0
    assert json.loads(resume.stdout)["killed"] == []
    status = run("status", "--lanes-dir", str(lanes), "--run-id", "cli")
    assert status.returncode == 0 and json.loads(status.stdout)["lane-01-t1"]["state"] == lm.STATE_FAILED
    actions = [
        json.loads(run("escalate", "--lanes-dir", str(lanes), "--lane", "lane-01-t1", "--reason", "cli").stdout)["action"] for _ in range(3)
    ]
    assert actions == [lm.ACTION_NUDGE, lm.ACTION_REDISPATCH, lm.ACTION_BLOCK]
    report = run("report", "--lanes-dir", str(lanes), "--run-id", "cli", "--out", str(tmp_path / "r.md"))
    assert report.returncode == 0 and "## Failed (1)" in report.stdout and (tmp_path / "r.md").is_file()


# ----------------------------------------------------------------- skill documents


def test_skill_doc_is_harness_neutral_and_names_the_durable_layout() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\nname: lane-manager\n")
    for token in (
        ".lanes/",
        "plan.md",
        "status.json",
        "report.md",
        "pending",
        "running",
        "verified",
        "failed",
        "merged",
        "resume",
        "heartbeat",
        "one line",
        "red",
        "green",
        "full suite",
        "verify",
        "merge",
        "escalate",
        "report",
        "nudge",
        "redispatch",
        "block",
        "claude-code.md",
    ):
        assert token in text, token
    for primitive in ("**spawn**", "**list-live**", "**message**"):
        assert primitive in text, primitive
    body = text.split("---", 2)[2]
    for claude_tool in ("ListAgents", "SendMessage", "`Agent`", "Workflow"):
        assert claude_tool not in body, f"{claude_tool} belongs in claude-code.md, not the neutral procedure"


def test_claude_code_enhancement_binds_every_primitive() -> None:
    text = (SKILL_DIR / "claude-code.md").read_text(encoding="utf-8")
    for primitive, tool in (("spawn", "`Agent`"), ("list-live", "`ListAgents`"), ("message", "`SendMessage`")):
        assert primitive in text and tool in text, (primitive, tool)
    assert "resume" in text and "report.md" in text
