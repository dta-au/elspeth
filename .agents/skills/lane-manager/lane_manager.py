#!/usr/bin/env python3
"""lane-manager mechanism: crash-durable lane state, red→green→suite verification, gated serial merge.

The orchestrating agent (see SKILL.md) does the spawning, the list-live call
and the nudges; this module owns everything that must survive the
orchestrator dying and must never depend on a worker's word:

* the per-lane directory ``.lanes/<lane-id>/`` holding ``plan.md`` (what the
  lane must do), ``status.json`` (state: pending|running|verified|failed|merged,
  heartbeat, attempts, evidence) and ``report.md`` (written by the worker);
  every write is an atomic replace, so a kill leaves the old or the new file;
* ``resume``: reads every ``status.json``, decides liveness from the heartbeat
  and worktree activity, and re-dispatches ONLY lanes killed mid-flight;
* ``verify``: a lane advances to ``verified`` only when its test is RED on the
  base without the fix, GREEN on the merged tree, and the full suite passes on
  that same merged tree — all measured in throwaway worktrees, never in a
  worker's working directory;
* ``merge``: serial and gated — a verification is bound to the base and branch
  SHAs it measured; if either moved, the merge is refused until re-verified.

Run ``python lane_manager.py --help`` for the CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_VERIFIED = "verified"
STATE_FAILED = "failed"
STATE_MERGED = "merged"
STATES = (STATE_PENDING, STATE_RUNNING, STATE_VERIFIED, STATE_FAILED, STATE_MERGED)

ACTION_NUDGE = "nudge"
ACTION_REDISPATCH = "redispatch"
ACTION_BLOCK = "block"

LIVE_LISTED = "alive-listed"
LIVE_HEARTBEAT = "alive-heartbeat"
LIVE_WORKTREE = "alive-worktree-activity"
DEAD = "dead"

DEFAULT_WINDOW_SECONDS = 900
# RED is a FAILED ASSERTION, visible in the output. Exit 1 alone proves nothing: pytest, unittest and a plain script all
# report an uncaught exception inside a test as exit 1 too. Same rule and regex as prove-it's prove_it.py.
RED_RE = re.compile(r"AssertionError|^E\s+assert\b|- assert\b|\bFailed: |^FAIL: ", re.MULTILINE)
SCRIPT_PATH = Path(__file__).resolve()


def stamp(epoch: float | None = None) -> str:
    """ISO-8601 UTC timestamp (seconds) for ``epoch`` or now."""
    when = datetime.now(UTC) if epoch is None else datetime.fromtimestamp(epoch, UTC)
    return when.isoformat(timespec="seconds")


def age_of(iso: str) -> float:
    """Seconds elapsed since an ISO-8601 stamp produced by :func:`stamp`."""
    return time.time() - datetime.fromisoformat(iso).timestamp()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- durable state


@dataclass
class Expected:
    files: list[str]
    test_files: list[str]
    test_command: str


@dataclass
class Attempt:
    agent_name: str | None
    dispatched_at: str
    dry_run: bool = False
    resumed: bool = False
    outcome: str = "open"


@dataclass
class LaneStatus:
    """One lane's ``status.json``. ``lanes_dir`` is where it lives and is not serialised."""

    lanes_dir: Path
    lane_id: str
    run_id: str
    repo: str
    ticket: str
    title: str
    description: str
    branch: str
    worktree: str
    expected: Expected
    state: str = STATE_PENDING
    dispatched_at: str | None = None
    heartbeat_at: str | None = None
    agent_name: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    verifications: list[dict[str, object]] = field(default_factory=list)
    liveness_checks: list[dict[str, object]] = field(default_factory=list)
    escalation: dict[str, bool] = field(default_factory=lambda: {"nudged": False, "redispatched": False})
    blocked: dict[str, object] | None = None
    failure_reasons: list[str] = field(default_factory=list)
    verified_at: str | None = None
    merged_at: str | None = None
    merge_sha: str | None = None

    @property
    def dir(self) -> Path:
        return self.lanes_dir / self.lane_id

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    @property
    def plan_path(self) -> Path:
        return self.dir / "plan.md"

    @property
    def report_path(self) -> Path:
        return self.dir / "report.md"

    @property
    def worktree_path(self) -> Path:
        return Path(self.repo) / self.worktree

    def to_dict(self) -> dict[str, object]:
        raw = asdict(self)
        del raw["lanes_dir"]
        return raw

    def save(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"{self.lane_id}: unknown state {self.state!r}")
        _atomic_write(self.status_path, json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, lanes_dir: Path, lane_id: str) -> LaneStatus:
        path = Path(lanes_dir) / lane_id / "status.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError(f"{path}: status root must be a mapping")
        expected_raw = raw["expected"]
        if type(expected_raw) is not dict:
            raise ValueError(f"{path}: expected must be a mapping")
        attempts_raw = raw.get("attempts", [])
        if type(attempts_raw) is not list:
            raise ValueError(f"{path}: attempts must be a list")
        return cls(
            lanes_dir=Path(lanes_dir),
            lane_id=str(raw["lane_id"]),
            run_id=str(raw["run_id"]),
            repo=str(raw["repo"]),
            ticket=str(raw["ticket"]),
            title=str(raw["title"]),
            description=str(raw.get("description", "")),
            branch=str(raw["branch"]),
            worktree=str(raw["worktree"]),
            expected=Expected(
                files=[str(f) for f in expected_raw["files"]],
                test_files=[str(f) for f in expected_raw["test_files"]],
                test_command=str(expected_raw["test_command"]),
            ),
            state=str(raw.get("state", STATE_PENDING)),
            dispatched_at=_opt_str(raw.get("dispatched_at")),
            heartbeat_at=_opt_str(raw.get("heartbeat_at")),
            agent_name=_opt_str(raw.get("agent_name")),
            attempts=[
                Attempt(
                    agent_name=_opt_str(a.get("agent_name")),
                    dispatched_at=str(a["dispatched_at"]),
                    dry_run=bool(a.get("dry_run", False)),
                    resumed=bool(a.get("resumed", False)),
                    outcome=str(a.get("outcome", "open")),
                )
                for a in attempts_raw
            ],
            verifications=list(raw.get("verifications", [])),
            liveness_checks=list(raw.get("liveness_checks", [])),
            escalation=dict(raw.get("escalation", {"nudged": False, "redispatched": False})),
            blocked=raw.get("blocked"),
            failure_reasons=[str(r) for r in raw.get("failure_reasons", [])],
            verified_at=_opt_str(raw.get("verified_at")),
            merged_at=_opt_str(raw.get("merged_at")),
            merge_sha=_opt_str(raw.get("merge_sha")),
        )


@dataclass
class Run:
    """The run manifest ``.lanes/<run-id>.run.json``: base ref, suite command, lane ids, merges."""

    lanes_dir: Path
    run_id: str
    repo: str
    base_ref: str
    suite_command: str
    created_at: str
    lane_ids: list[str]
    merges: list[dict[str, object]] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return self.lanes_dir / f"{self.run_id}.run.json"

    def save(self) -> None:
        raw = asdict(self)
        del raw["lanes_dir"]
        _atomic_write(self.manifest_path, json.dumps(raw, indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, lanes_dir: Path, run_id: str) -> Run:
        path = Path(lanes_dir) / f"{run_id}.run.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError(f"{path}: manifest root must be a mapping")
        return cls(
            lanes_dir=Path(lanes_dir),
            run_id=str(raw["run_id"]),
            repo=str(raw["repo"]),
            base_ref=str(raw["base_ref"]),
            suite_command=str(raw["suite_command"]),
            created_at=str(raw["created_at"]),
            lane_ids=[str(x) for x in raw["lane_ids"]],
            merges=list(raw.get("merges", [])),
        )

    def lane(self, lane_id: str) -> LaneStatus:
        if lane_id not in self.lane_ids:
            raise KeyError(f"no lane {lane_id!r} in run {self.run_id}")
        return LaneStatus.load(self.lanes_dir, lane_id)

    def lanes(self) -> list[LaneStatus]:
        return [self.lane(lane_id) for lane_id in self.lane_ids]

    def base_sha(self) -> str:
        proc = _git(Path(self.repo), "rev-parse", "--verify", f"{self.base_ref}^{{commit}}")
        if proc.returncode != 0:
            raise RuntimeError(f"base ref {self.base_ref!r} does not resolve in {self.repo}")
        return proc.stdout.strip()


def run_for_lane(lanes_dir: Path, lane_id: str) -> tuple[Run, LaneStatus]:
    lane = LaneStatus.load(lanes_dir, lane_id)
    return Run.load(lanes_dir, lane.run_id), lane


# ---------------------------------------------------------------- init / plan / brief


def render_plan(run: Run, lane: LaneStatus) -> str:
    files = "\n".join(f"- `{f}`" for f in lane.expected.files) or "- _none named_"
    tests = "\n".join(f"- `{f}`" for f in lane.expected.test_files) or "- _none named_"
    return (
        f"# {lane.lane_id} — {lane.title}\n\n"
        f"Ticket `{lane.ticket}` · run `{run.run_id}` · branch `{lane.branch}` · base `{run.base_ref}`\n\n"
        f"## Goal\n\n{lane.description or lane.title}\n\n"
        f"## Files a correct fix must change\n\n{files}\n\n"
        f"## Test files the lane must add or change\n\n{tests}\n\n"
        f"## Test command (must be RED before the fix, GREEN after)\n\n```\n{lane.expected.test_command}\n```\n\n"
        f"## Full suite (must pass on the merged tree)\n\n```\n{run.suite_command}\n```\n\n"
        "## Definition of done\n\n"
        "1. The failing test is committed FIRST and `verify` proves it fails on the base without the fix.\n"
        "2. The fix is committed; the test command exits 0 on the merged tree.\n"
        "3. The full suite exits 0 on the merged tree.\n"
        f"4. Findings are written to `{lane.report_path}`; the worker returns one line.\n"
    )


def render_brief(run: Run, lane: LaneStatus) -> str:
    heartbeat_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT_PATH))} heartbeat --lanes-dir {shlex.quote(str(lane.lanes_dir))} --lane {lane.lane_id}"
    files = ", ".join(f"`{f}`" for f in lane.expected.files) or "_(none named)_"
    tests = ", ".join(f"`{f}`" for f in lane.expected.test_files) or "_(none named)_"
    return (
        f"Working directory: {lane.worktree_path} (a git worktree on branch `{lane.branch}`, based on `{run.base_ref}`).\n"
        f"Lane: {lane.lane_id} — {lane.title} (ticket {lane.ticket}). Plan: {lane.plan_path}\n\n"
        f"Goal: {lane.description or lane.title}\n\n"
        "Rules:\n"
        f"1. Write the FAILING TEST FIRST in {tests}. Run `{lane.expected.test_command}` BEFORE touching the fix and confirm it "
        "FAILS AT AN ASSERTION (AssertionError / FAIL in the output, exit 1) — not a crash: an uncaught exception also exits 1 "
        "and proves nothing. If the fix adds a module or attribute, assert it exists first "
        "(`assert importlib.util.find_spec('x') is not None`, `assert hasattr(obj, 'name')`) so the base run fails at the "
        "assertion rather than at an import. Commit the test on its own.\n"
        f"2. Then fix the code in {files}; run the same test command until it exits 0; commit the fix.\n"
        f"3. Commit only to `{lane.branch}` inside {lane.worktree_path}. Never edit or commit in the main checkout.\n"
        f"4. Before each step, run the heartbeat so a crash can be told from slow work:\n   {heartbeat_cmd}\n"
        f"5. WRITE your findings, evidence and any caveats to {lane.report_path} — first paragraph is a one-line "
        "summary, then the detail. That file is the deliverable; a chat message can be truncated, the file cannot.\n"
        "6. Your final message must be exactly ONE LINE: the summary line from report.md. Nothing else.\n"
    )


def init_run(
    *,
    lanes_dir: Path,
    run_id: str,
    repo: Path,
    base_ref: str,
    suite_command: str,
    tickets: list[dict[str, object]],
) -> Run:
    """Create ``.lanes/<run-id>.run.json`` and ``.lanes/<lane-id>/{plan.md,status.json}`` per ticket.

    Each ticket is ``{ticket, title, files, test_files, test_command[, description, branch, worktree]}``.
    Lane id, branch and worktree derive from the ticket id so a re-dispatch targets the same place.
    """
    lanes_dir = Path(lanes_dir)
    repo = Path(repo).resolve()
    run = Run(
        lanes_dir=lanes_dir,
        run_id=run_id,
        repo=str(repo),
        base_ref=base_ref,
        suite_command=suite_command,
        created_at=stamp(),
        lane_ids=[],
    )
    lanes: list[LaneStatus] = []
    for index, ticket in enumerate(tickets, start=1):
        ticket_id = str(ticket["ticket"])
        branch = str(ticket.get("branch") or f"lane/{ticket_id}")
        for key in ("files", "test_files"):
            if type(ticket.get(key, [])) is not list:
                raise ValueError(f"ticket {ticket_id}: {key} must be a list")
        lane = LaneStatus(
            lanes_dir=lanes_dir,
            lane_id=f"lane-{index:02d}-{ticket_id}",
            run_id=run_id,
            repo=str(repo),
            ticket=ticket_id,
            title=str(ticket["title"]),
            description=str(ticket.get("description", "")),
            branch=branch,
            worktree=str(ticket.get("worktree") or f".claude/worktrees/{branch.replace('/', '-')}"),
            expected=Expected(
                files=[str(f) for f in ticket.get("files", [])],
                test_files=[str(f) for f in ticket.get("test_files", [])],
                test_command=str(ticket["test_command"]),
            ),
        )
        lanes.append(lane)
        run.lane_ids.append(lane.lane_id)
    run.save()
    for lane in lanes:
        _atomic_write(lane.plan_path, render_plan(run, lane))
        lane.save()
    return run


# ---------------------------------------------------------------- dispatch / heartbeat


def _ensure_worktree(run: Run, lane: LaneStatus) -> None:
    repo = Path(run.repo)
    path = lane.worktree_path
    listed = _git(repo, "worktree", "list", "--porcelain").stdout
    if f"worktree {path.resolve()}" in listed or (path / ".git").exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{lane.branch}").returncode == 0
    args = ["worktree", "add", str(path), lane.branch] if branch_exists else ["worktree", "add", "-b", lane.branch, str(path), run.base_ref]
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise RuntimeError(f"could not create worktree for {lane.lane_id}: {proc.stderr.strip()}")
    venv = repo / ".venv"
    if venv.exists() and not (path / ".venv").exists():
        os.symlink(venv, path / ".venv")


def _record_attempt(lane: LaneStatus, agent_name: str | None, *, dry_run: bool, resumed: bool) -> None:
    now = stamp()
    if not dry_run and lane.attempts and all(a.dry_run for a in lane.attempts):
        lane.escalation = {"nudged": False, "redispatched": False}  # rehearsal rungs are not real rungs
        lane.blocked = None
    lane.attempts.append(Attempt(agent_name=agent_name, dispatched_at=now, dry_run=dry_run, resumed=resumed))
    lane.dispatched_at = now
    lane.heartbeat_at = now
    lane.agent_name = agent_name
    lane.state = STATE_RUNNING


def dispatch(run: Run, lane_id: str, agent_name: str | None, *, dry_run: bool = False) -> LaneStatus:
    """Create the lane's worktree (idempotent), record the attempt, mark it running."""
    lane = run.lane(lane_id)
    if lane.state == STATE_MERGED:
        raise ValueError(f"{lane_id} is merged; nothing to dispatch")
    if not dry_run:
        _ensure_worktree(run, lane)
    _record_attempt(lane, agent_name, dry_run=dry_run, resumed=False)
    lane.save()
    return lane


def heartbeat(lanes_dir: Path, lane_id: str) -> LaneStatus:
    lane = LaneStatus.load(lanes_dir, lane_id)
    lane.heartbeat_at = stamp()
    lane.save()
    return lane


# ---------------------------------------------------------------- liveness / resume


@dataclass
class Liveness:
    checked_at: str
    listed: bool
    heartbeat_age: float | None
    worktree_exists: bool
    worktree_activity_age: float | None
    uncommitted_changes: int
    window: int
    verdict: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _latest_mtime(root: Path) -> float | None:
    latest: float | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", ".venv", "__pycache__", "node_modules"}]
        for name in filenames:
            if name == ".git":  # linked-worktree gitfile: harness-written, not lane activity
                continue
            try:
                mtime = (Path(dirpath) / name).stat().st_mtime
            except FileNotFoundError:
                continue
            if latest is None or mtime > latest:
                latest = mtime
    return latest


def liveness(run: Run, lane_id: str, *, listed: bool, window: int = DEFAULT_WINDOW_SECONDS) -> Liveness:
    """Alive if the harness lists it, its heartbeat is fresh, or its worktree moved inside ``window`` seconds.

    Uncommitted changes alone are NOT life: a lane killed mid-edit leaves them behind forever.
    """
    lane = run.lane(lane_id)
    hb_age = age_of(lane.heartbeat_at) if lane.heartbeat_at else None
    worktree = lane.worktree_path
    exists = worktree.is_dir()
    uncommitted = 0
    activity_age: float | None = None
    if exists:
        status = _git(worktree, "status", "--porcelain")
        uncommitted = len([line for line in status.stdout.splitlines() if line.strip()])
        latest = _latest_mtime(worktree)
        if latest is not None:
            activity_age = time.time() - latest
    if listed:
        verdict = LIVE_LISTED
    elif hb_age is not None and hb_age <= window:
        verdict = LIVE_HEARTBEAT
    elif activity_age is not None and activity_age <= window:
        verdict = LIVE_WORKTREE
    else:
        verdict = DEAD
    result = Liveness(
        checked_at=stamp(),
        listed=listed,
        heartbeat_age=hb_age,
        worktree_exists=exists,
        worktree_activity_age=activity_age,
        uncommitted_changes=uncommitted,
        window=window,
        verdict=verdict,
    )
    lane.liveness_checks.append(result.as_dict())
    lane.save()
    return result


@dataclass
class ResumePlan:
    killed: list[LaneStatus]
    alive: list[LaneStatus]
    pending: list[LaneStatus]
    terminal: list[LaneStatus]
    briefs: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "killed": [lane.lane_id for lane in self.killed],
            "alive": [lane.lane_id for lane in self.alive],
            "pending": [lane.lane_id for lane in self.pending],
            "terminal": {lane.lane_id: lane.state for lane in self.terminal},
            "briefs": self.briefs,
        }


def resume(run: Run, *, window: int = DEFAULT_WINDOW_SECONDS, listed: set[str], redispatch: bool = False) -> ResumePlan:
    """Read every status.json; lanes that are ``running`` with no life signal inside ``window`` were killed.

    ``listed`` is the set of worker names the harness still reports as running (may be empty after a
    crash: the previous orchestrator's workers are gone). With ``redispatch`` the killed lanes get a
    new attempt marked ``resumed`` and their briefs are returned for the harness to spawn — nothing else
    is touched: alive lanes keep running, pending lanes are dispatched normally, terminal lanes are left.
    """
    plan = ResumePlan(killed=[], alive=[], pending=[], terminal=[], briefs={})
    for lane in run.lanes():
        if lane.state == STATE_PENDING:
            plan.pending.append(lane)
        elif lane.state != STATE_RUNNING:
            plan.terminal.append(lane)
        else:
            is_listed = lane.lane_id in listed or (lane.agent_name is not None and lane.agent_name in listed)
            verdict = liveness(run, lane.lane_id, listed=is_listed, window=window)
            (plan.killed if verdict.verdict == DEAD else plan.alive).append(run.lane(lane.lane_id))
    if redispatch:
        for index, lane in enumerate(plan.killed):
            if lane.attempts:
                lane.attempts[-1].outcome = "killed"
            _record_attempt(lane, f"{lane.lane_id}-resume-{len(lane.attempts) + 1}", dry_run=False, resumed=True)
            lane.save()
            plan.killed[index] = lane
            plan.briefs[lane.lane_id] = render_brief(run, lane)
    return plan


# ---------------------------------------------------------------- verify: red → green → suite


@dataclass
class Verification:
    """Evidence gathered from git and throwaway worktrees — never from the lane."""

    checked_at: str
    base_ref: str
    base_sha: str
    branch: str
    branch_sha: str | None
    branch_exists: bool
    commits_ahead: list[str]
    changed_files: list[str]
    missing_expected_files: list[str]
    missing_test_files: list[str]
    test_command: str
    red_exit_code: int | None
    red_output_tail: str
    green_exit_code: int | None
    green_output_tail: str
    suite_command: str
    suite_exit_code: int | None
    suite_output_tail: str
    verified: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@contextlib.contextmanager
def _temp_worktree(repo: Path, sha: str, prefix: str) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix=prefix)) / "tree"
    proc = _git(repo, "worktree", "add", "--detach", str(path), sha)
    if proc.returncode != 0:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise RuntimeError(f"could not create verification worktree: {proc.stderr.strip()}")
    try:
        venv = repo / ".venv"
        if venv.exists():
            os.symlink(venv, path / ".venv")
        yield path
    finally:
        _git(repo, "worktree", "remove", "--force", str(path))
        _git(repo, "worktree", "prune")
        shutil.rmtree(path.parent, ignore_errors=True)


def _run_command(command: str, cwd: Path, timeout: int) -> tuple[int | None, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{cwd / 'src'}:{cwd / 'elspeth-lints' / 'src'}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # a stale .pyc (same source size, same second) would run the wrong tree's code
    try:
        proc = subprocess.run(shlex.split(command), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return None, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    return proc.returncode, (proc.stdout + proc.stderr)[-3000:]


def verify(run: Run, lane_id: str, *, test_timeout: int = 1800, suite_timeout: int = 7200) -> Verification:
    """RED on base (test files only) → GREEN on merged tree → full suite on merged tree. All three or nothing."""
    lane = run.lane(lane_id)
    if lane.state == STATE_MERGED:
        raise ValueError(f"{lane_id} is merged; nothing to verify")
    repo = Path(run.repo)
    base_sha = run.base_sha()
    reasons: list[str] = []

    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{lane.branch}").returncode == 0
    branch_sha: str | None = None
    commits: list[str] = []
    changed: list[str] = []
    if exists:
        branch_sha = _git(repo, "rev-parse", lane.branch).stdout.strip()
        commits = [line for line in _git(repo, "log", "--format=%h %s", f"{base_sha}..{branch_sha}").stdout.splitlines() if line.strip()]
        changed = sorted(
            line for line in _git(repo, "diff", "--name-only", f"{base_sha}...{branch_sha}").stdout.splitlines() if line.strip()
        )
        if not commits:
            reasons.append(f"branch {lane.branch} has no commits ahead of {run.base_ref}")
    else:
        reasons.append(f"branch {lane.branch} does not exist")
    missing_expected = sorted(f for f in lane.expected.files if f not in changed)
    missing_tests = sorted(f for f in lane.expected.test_files if f not in changed)
    if exists and commits:
        if missing_expected:
            reasons.append(f"expected files not changed on {lane.branch}: {', '.join(missing_expected)}")
        if missing_tests:
            reasons.append(f"expected test files not changed on {lane.branch}: {', '.join(missing_tests)} (no failing test was produced)")

    red_rc: int | None = None
    red_tail = ""
    green_rc: int | None = None
    green_tail = ""
    suite_rc: int | None = None
    suite_tail = ""
    if not reasons and branch_sha is not None:
        with _temp_worktree(repo, base_sha, "lane-red-") as red_tree:
            checkout = _git(red_tree, "checkout", branch_sha, "--", *lane.expected.test_files)
            if checkout.returncode != 0:
                reasons.append(f"could not place test files on base: {checkout.stderr.strip()}")
            else:
                red_rc, red_tail = _run_command(lane.expected.test_command, red_tree, test_timeout)
                if red_rc == 0:
                    reasons.append("test does not fail on the base without the fix: not a failing-first test")
                elif red_rc is None:
                    reasons.append(f"red run could not complete: {red_tail}")
                elif red_rc != 1:
                    reasons.append(f"test crashed on the base rather than failing (exit {red_rc}): RED means a failed assertion (exit 1)")
                elif not RED_RE.search(red_tail):
                    reasons.append(
                        "test crashed on the base rather than failing: exit 1 but no failed assertion in the output (an uncaught "
                        "exception, e.g. importing a module the fix adds); assert what the fix adds exists "
                        "(importlib.util.find_spec / hasattr) so the assertion is what fails"
                    )
    if not reasons and branch_sha is not None:
        with _temp_worktree(repo, base_sha, "lane-green-") as tree:
            merge = _git(tree, "merge", "--no-ff", "--no-commit", branch_sha)
            if merge.returncode != 0:
                _git(tree, "merge", "--abort")
                reasons.append(f"merge conflict with {run.base_ref}: {(merge.stdout + merge.stderr).strip()[-500:]}")
            else:
                green_rc, green_tail = _run_command(lane.expected.test_command, tree, test_timeout)
                if green_rc != 0:
                    reasons.append(f"test command exited {green_rc} on the merged tree (expected 0)")
                else:
                    suite_rc, suite_tail = _run_command(run.suite_command, tree, suite_timeout)
                    if suite_rc != 0:
                        reasons.append(f"full suite exited {suite_rc} on the merged tree (expected 0)")

    verification = Verification(
        checked_at=stamp(),
        base_ref=run.base_ref,
        base_sha=base_sha,
        branch=lane.branch,
        branch_sha=branch_sha,
        branch_exists=exists,
        commits_ahead=commits,
        changed_files=changed,
        missing_expected_files=missing_expected,
        missing_test_files=missing_tests,
        test_command=lane.expected.test_command,
        red_exit_code=red_rc,
        red_output_tail=red_tail,
        green_exit_code=green_rc,
        green_output_tail=green_tail,
        suite_command=run.suite_command,
        suite_exit_code=suite_rc,
        suite_output_tail=suite_tail,
        verified=not reasons,
        reasons=reasons,
    )
    lane.verifications.append(verification.as_dict())
    if verification.verified:
        lane.state = STATE_VERIFIED
        lane.verified_at = verification.checked_at
        lane.failure_reasons = []
        if lane.attempts:
            lane.attempts[-1].outcome = "verified"
    else:
        lane.state = STATE_FAILED
        lane.failure_reasons = list(reasons)
    lane.save()
    return verification


# ---------------------------------------------------------------- merge: serial + gated


@dataclass
class MergeOutcome:
    lane_id: str
    merged: bool
    merge_sha: str | None
    reasons: list[str]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def merge(run: Run, lane_id: str) -> MergeOutcome:
    """Gate, then ``git merge --no-ff`` into the checkout that has ``base_ref`` checked out.

    The gate binds the merge to the evidence: the lane must be ``verified``, and the base and branch
    SHAs must equal the ones that verification measured. After any merge the base has moved, so the
    next lane's verification is stale by construction and must be re-run — that is the serial gate.
    """
    lane = run.lane(lane_id)
    repo = Path(run.repo)
    reasons: list[str] = []
    if lane.state != STATE_VERIFIED:
        reasons.append(f"lane is not verified (state={lane.state})")
    last = lane.verifications[-1] if lane.verifications else None
    if last is None or not last.get("verified"):
        reasons.append("no successful verification recorded")
    else:
        base_now = run.base_sha()
        branch_now = _git(repo, "rev-parse", lane.branch).stdout.strip()
        if last["base_sha"] != base_now:
            reasons.append(f"base moved since verification ({str(last['base_sha'])[:12]} → {base_now[:12]}): re-verify")
        if last["branch_sha"] != branch_now:
            reasons.append(f"branch moved since verification ({str(last['branch_sha'])[:12]} → {branch_now[:12]}): re-verify")
    head = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if head != run.base_ref:
        reasons.append(f"checkout {repo} is on {head or 'a detached HEAD'!s}, not {run.base_ref}")
    if reasons:
        return MergeOutcome(lane_id=lane_id, merged=False, merge_sha=None, reasons=reasons)

    proc = _git(repo, "merge", "--no-ff", "--no-edit", "-m", f"merge {lane.branch}: {lane.title} ({lane.ticket})", lane.branch)
    if proc.returncode != 0:
        _git(repo, "merge", "--abort")
        return MergeOutcome(
            lane_id=lane_id, merged=False, merge_sha=None, reasons=[f"git merge failed: {(proc.stdout + proc.stderr).strip()[-500:]}"]
        )
    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    lane.state = STATE_MERGED
    lane.merged_at = stamp()
    lane.merge_sha = merge_sha
    lane.save()
    run.merges.append({"lane_id": lane_id, "branch": lane.branch, "merge_sha": merge_sha, "at": lane.merged_at})
    run.save()
    return MergeOutcome(lane_id=lane_id, merged=True, merge_sha=merge_sha, reasons=[])


# ---------------------------------------------------------------- escalation ladder


def escalate(run: Run, lane_id: str, *, reason: str) -> str:
    """Advance one rung: nudge → redispatch → block. Each rung fires once; block marks the lane failed."""
    lane = run.lane(lane_id)
    if lane.state in {STATE_VERIFIED, STATE_MERGED}:
        raise ValueError(f"{lane_id} is {lane.state}; nothing to escalate")
    if lane.blocked is not None:
        return ACTION_BLOCK
    if lane.attempts:
        lane.attempts[-1].outcome = reason
    if not lane.escalation["nudged"]:
        lane.escalation["nudged"] = True
        lane.state = STATE_RUNNING
        action = ACTION_NUDGE
    elif not lane.escalation["redispatched"]:
        lane.escalation["redispatched"] = True
        lane.state = STATE_RUNNING
        action = ACTION_REDISPATCH
    else:
        lane.state = STATE_FAILED
        lane.blocked = {
            "blocked_at": stamp(),
            "reason": reason,
            "attempts": [asdict(a) for a in lane.attempts],
            "last_verification": lane.verifications[-1] if lane.verifications else None,
            "last_liveness_check": lane.liveness_checks[-1] if lane.liveness_checks else None,
        }
        lane.failure_reasons = [f"blocked: {reason}"]
        action = ACTION_BLOCK
    lane.save()
    return action


# ---------------------------------------------------------------- report


def summary_line(lanes_dir: Path, lane_id: str) -> str | None:
    """First non-empty, non-heading line of the lane's report.md — the worker's one-line summary."""
    path = Path(lanes_dir) / lane_id / "report.md"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    return None


def merge_order(lanes: list[LaneStatus]) -> list[LaneStatus]:
    """Verified lanes, disjoint expected-file sets first, then by overlap count, then by verification time."""
    verified = [lane for lane in lanes if lane.state == STATE_VERIFIED]

    def overlap(lane: LaneStatus) -> int:
        mine = set(lane.expected.files)
        return sum(1 for other in verified if other is not lane and mine & set(other.expected.files))

    return sorted(verified, key=lambda lane: (overlap(lane), lane.verified_at or "", lane.lane_id))


def render_report(run: Run) -> str:
    lanes = run.lanes()
    by_state = {state: [lane for lane in lanes if lane.state == state] for state in STATES}
    dry = any(a.dry_run for lane in lanes for a in lane.attempts)
    lines = [f"# lane-manager report — run `{run.run_id}`", ""]
    lines.append(f"Base ref: `{run.base_ref}` · repo: `{run.repo}` · created {run.created_at} · lanes dir `{run.lanes_dir}`")
    if dry:
        lines += ["", "**DRY RUN** — at least one attempt was a rehearsal; verification ran against the real tree."]
    for state, title in (
        (STATE_MERGED, "Merged"),
        (STATE_VERIFIED, "Verified"),
        (STATE_FAILED, "Failed"),
        (STATE_RUNNING, "Running"),
        (STATE_PENDING, "Pending"),
    ):
        group = by_state[state]
        lines += ["", f"## {title} ({len(group)})", ""]
        if not group:
            lines.append("_none_")
        for lane in group:
            summary = summary_line(run.lanes_dir, lane.lane_id)
            lines.append(f"- **{lane.lane_id}** `{lane.ticket}` — {lane.title}" + (f" — _{summary}_" if summary else ""))
            if lane.verifications:
                v = lane.verifications[-1]
                lines.append(
                    f"  - verification {v['checked_at']}: base `{str(v['base_sha'])[:12]}` branch `{str(v['branch_sha'] or '')[:12]}` "
                    f"red={v['red_exit_code']} green={v['green_exit_code']} suite={v['suite_exit_code']} → "
                    + ("verified" if v["verified"] else "; ".join(v["reasons"]))
                )
            if lane.merge_sha:
                lines.append(f"  - merged as `{lane.merge_sha[:12]}` at {lane.merged_at}")
            if lane.blocked:
                lines.append(f"  - blocked: {lane.blocked.get('reason')}")
            if state in {STATE_FAILED, STATE_RUNNING}:
                lines.append(
                    f"  - ladder: nudged={lane.escalation['nudged']} redispatched={lane.escalation['redispatched']} attempts={len(lane.attempts)}"
                )
                for c in lane.liveness_checks[-3:]:
                    lines.append(
                        f"  - liveness {c['checked_at']}: listed={c['listed']} heartbeat_age={c['heartbeat_age']} uncommitted={c['uncommitted_changes']} → {c['verdict']}"
                    )
    lines += ["", "## Merge order proposal", ""]
    ordered = merge_order(lanes)
    if not ordered:
        lines.append("_nothing verified to merge_")
    for index, lane in enumerate(ordered, start=1):
        lines.append(f"{index}. `{lane.branch}` ({lane.ticket}) — `lane_manager.py merge --lane {lane.lane_id}`")
    lines += ["", "Merge serially: after each merge the base moves, so re-verify the next lane before merging it."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def _cmd_init(args: argparse.Namespace) -> int:
    tickets = json.loads(Path(args.tickets).read_text(encoding="utf-8"))
    if type(tickets) is not list:
        print("tickets file must contain a JSON list", file=sys.stderr)
        return 2
    run = init_run(
        lanes_dir=Path(args.lanes_dir),
        run_id=args.run_id,
        repo=Path(args.repo).resolve(),
        base_ref=args.base_ref,
        suite_command=args.suite_command,
        tickets=tickets,
    )
    print(json.dumps({"run_id": run.run_id, "lanes": run.lane_ids, "manifest": str(run.manifest_path)}, indent=2))
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    run, lane = run_for_lane(Path(args.lanes_dir), args.lane)
    print(render_brief(run, lane))
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    run, _ = run_for_lane(Path(args.lanes_dir), args.lane)
    lane = dispatch(run, args.lane, args.agent_name, dry_run=args.dry_run)
    print(json.dumps({"lane_id": lane.lane_id, "state": lane.state, "attempt": len(lane.attempts), "worktree": str(lane.worktree_path)}))
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    lane = heartbeat(Path(args.lanes_dir), args.lane)
    print(json.dumps({"lane_id": lane.lane_id, "heartbeat_at": lane.heartbeat_at}))
    return 0


def _cmd_liveness(args: argparse.Namespace) -> int:
    run, _ = run_for_lane(Path(args.lanes_dir), args.lane)
    verdict = liveness(run, args.lane, listed=args.listed, window=args.window)
    print(json.dumps(verdict.as_dict(), indent=2))
    return 0 if verdict.verdict != DEAD else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    run = Run.load(Path(args.lanes_dir), args.run_id)
    plan = resume(run, window=args.window, listed=set(args.listed or []), redispatch=args.redispatch)
    print(json.dumps(plan.as_dict(), indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    run, _ = run_for_lane(Path(args.lanes_dir), args.lane)
    result = verify(run, args.lane, test_timeout=args.test_timeout, suite_timeout=args.suite_timeout)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.verified else 1


def _cmd_merge(args: argparse.Namespace) -> int:
    run, _ = run_for_lane(Path(args.lanes_dir), args.lane)
    outcome = merge(run, args.lane)
    print(json.dumps(outcome.as_dict(), indent=2))
    return 0 if outcome.merged else 1


def _cmd_escalate(args: argparse.Namespace) -> int:
    run, _ = run_for_lane(Path(args.lanes_dir), args.lane)
    action = escalate(run, args.lane, reason=args.reason)
    print(json.dumps({"lane_id": args.lane, "action": action, "state": run.lane(args.lane).state}))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    run = Run.load(Path(args.lanes_dir), args.run_id)
    print(json.dumps({lane.lane_id: lane.to_dict() for lane in run.lanes()}, indent=2, sort_keys=True))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    run = Run.load(Path(args.lanes_dir), args.run_id)
    text = render_report(run)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def lanes_dir(p: argparse.ArgumentParser) -> None:
        p.add_argument("--lanes-dir", default=".lanes", help="directory holding <lane-id>/ and <run-id>.run.json")

    p = sub.add_parser("init", help="create the run manifest and one .lanes/<lane-id>/ per ticket")
    lanes_dir(p)
    p.add_argument("--run-id", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--base-ref", required=True)
    p.add_argument("--suite-command", required=True, help="full-suite command that must pass on every merged tree")
    p.add_argument(
        "--tickets", required=True, help="JSON list of {ticket,title,files,test_files,test_command[,description,branch,worktree]}"
    )
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("brief", help="print the worker brief for a lane (pass it verbatim to spawn)")
    lanes_dir(p)
    p.add_argument("--lane", required=True)
    p.set_defaults(func=_cmd_brief)

    p = sub.add_parser("dispatch", help="create the worktree, record the attempt, mark the lane running")
    lanes_dir(p)
    p.add_argument("--lane", required=True)
    p.add_argument("--agent-name", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_dispatch)

    p = sub.add_parser("heartbeat", help="stamp the lane's heartbeat (workers run this before each step)")
    lanes_dir(p)
    p.add_argument("--lane", required=True)
    p.set_defaults(func=_cmd_heartbeat)

    p = sub.add_parser("liveness", help="decide whether a running lane is alive; exit 1 if dead")
    lanes_dir(p)
    p.add_argument("--lane", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--listed", dest="listed", action="store_true", help="list-live showed the lane's worker")
    group.add_argument("--not-listed", dest="listed", action="store_false", help="list-live did not show it")
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW_SECONDS)
    p.set_defaults(func=_cmd_liveness)

    p = sub.add_parser("resume", help="find lanes killed mid-flight; with --redispatch mark them and print their briefs")
    lanes_dir(p)
    p.add_argument("--run-id", required=True)
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW_SECONDS)
    p.add_argument("--listed", nargs="*", default=[], help="worker names list-live still reports")
    p.add_argument("--redispatch", action="store_true")
    p.set_defaults(func=_cmd_resume)

    p = sub.add_parser("verify", help="red on base → green on merged tree → full suite; exit 1 unless verified")
    lanes_dir(p)
    p.add_argument("--lane", required=True)
    p.add_argument("--test-timeout", type=int, default=1800)
    p.add_argument("--suite-timeout", type=int, default=7200)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("merge", help="gated --no-ff merge of a verified lane into base; exit 1 if refused")
    lanes_dir(p)
    p.add_argument("--lane", required=True)
    p.set_defaults(func=_cmd_merge)

    p = sub.add_parser("escalate", help="advance one ladder rung; prints nudge|redispatch|block")
    lanes_dir(p)
    p.add_argument("--lane", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_escalate)

    p = sub.add_parser("status", help="dump every lane's status.json for a run")
    lanes_dir(p)
    p.add_argument("--run-id", required=True)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("report", help="render the markdown report with a merge-order proposal")
    lanes_dir(p)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
