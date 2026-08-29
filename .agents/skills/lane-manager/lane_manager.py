#!/usr/bin/env python3
"""lane-manager mechanism: state file, evidence-based verification, escalation, report.

The orchestrating agent (see SKILL.md) does the dispatching, the ListAgents
call and the SendMessage nudges; this module owns everything that must not
depend on a subagent's word:

* the JSON state file under ``.claude/lanes/<run-id>.json``;
* verification against the expected branch (``git log``/``git diff``), the
  lane's test command, and the claimed files — a lane is *landed* only when all
  three agree with the filesystem, never because the lane said so;
* the idle verdict (ListAgents result + worktree inspection; dead only when
  both fail);
* the escalation ladder (nudge once → re-dispatch once → BLOCKED, continue);
* the final markdown report with a merge-order proposal.

Run ``python lane_manager.py --help`` for the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATUS_PENDING = "pending"
STATUS_DISPATCHED = "dispatched"
STATUS_LANDED = "landed"
STATUS_NUDGED = "nudged"
STATUS_REDISPATCHED = "redispatched"
STATUS_BLOCKED = "blocked"

ACTION_NUDGE = "nudge"
ACTION_REDISPATCH = "redispatch"
ACTION_BLOCK = "block"

VERDICT_ALIVE_LISTED = "alive-listed"
VERDICT_ALIVE_WORKTREE = "alive-worktree-activity"
VERDICT_DEAD = "dead"

DEFAULT_ACTIVITY_WINDOW_SECONDS = 900


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass
class Expected:
    branch: str
    files: list[str]
    test_command: str


@dataclass
class Attempt:
    agent_name: str | None
    dispatched_at: str
    dry_run: bool
    outcome: str = "open"


@dataclass
class Lane:
    lane_id: str
    ticket: str
    title: str
    expected: Expected
    worktree: str
    status: str = STATUS_PENDING
    dispatched_at: str | None = None
    agent_name: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    verifications: list[dict[str, object]] = field(default_factory=list)
    idle_checks: list[dict[str, object]] = field(default_factory=list)
    escalation: dict[str, bool] = field(default_factory=lambda: {"nudged": False, "redispatched": False})
    blocked: dict[str, object] | None = None
    landed_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> Lane:
        expected_raw = raw["expected"]
        if type(expected_raw) is not dict:
            raise ValueError(f"lane {raw.get('lane_id')!r}: expected must be a mapping")
        attempts_raw = raw.get("attempts", [])
        if type(attempts_raw) is not list:
            raise ValueError(f"lane {raw.get('lane_id')!r}: attempts must be a list")
        return cls(
            lane_id=str(raw["lane_id"]),
            ticket=str(raw["ticket"]),
            title=str(raw["title"]),
            expected=Expected(
                branch=str(expected_raw["branch"]),
                files=[str(f) for f in expected_raw["files"]],
                test_command=str(expected_raw["test_command"]),
            ),
            worktree=str(raw["worktree"]),
            status=str(raw.get("status", STATUS_PENDING)),
            dispatched_at=_opt_str(raw.get("dispatched_at")),
            agent_name=_opt_str(raw.get("agent_name")),
            attempts=[
                Attempt(
                    agent_name=_opt_str(a["agent_name"]),
                    dispatched_at=str(a["dispatched_at"]),
                    dry_run=bool(a["dry_run"]),
                    outcome=str(a.get("outcome", "open")),
                )
                for a in attempts_raw
            ],
            verifications=list(raw.get("verifications", [])),
            idle_checks=list(raw.get("idle_checks", [])),
            escalation=dict(raw.get("escalation", {"nudged": False, "redispatched": False})),
            blocked=raw.get("blocked"),
            landed_at=_opt_str(raw.get("landed_at")),
        )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


@dataclass
class RunState:
    run_id: str
    repo: str
    base_ref: str
    created_at: str
    lanes: list[Lane]

    @classmethod
    def load(cls, path: Path) -> RunState:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError(f"{path}: state root must be a mapping")
        return cls(
            run_id=str(raw["run_id"]),
            repo=str(raw["repo"]),
            base_ref=str(raw["base_ref"]),
            created_at=str(raw["created_at"]),
            lanes=[Lane.from_dict(lane) for lane in raw["lanes"]],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def lane(self, lane_id: str) -> Lane:
        for lane in self.lanes:
            if lane.lane_id == lane_id:
                return lane
        raise KeyError(f"no lane {lane_id!r} in run {self.run_id}")


# ---------------------------------------------------------------- init / dispatch


def init_run(
    *,
    state_path: Path,
    run_id: str,
    repo: Path,
    base_ref: str,
    tickets: list[dict[str, object]],
) -> RunState:
    """Create a run from a ticket list.

    Each ticket is ``{ticket, title, files, test_command, branch?}``. The lane id,
    branch and worktree are derived deterministically from the ticket id so a
    re-dispatched lane targets the same branch and the same worktree.
    """
    lanes: list[Lane] = []
    for index, ticket in enumerate(tickets, start=1):
        ticket_id = str(ticket["ticket"])
        branch = str(ticket.get("branch") or f"lane/{ticket_id}")
        files_raw = ticket["files"]
        if type(files_raw) is not list:
            raise ValueError(f"ticket {ticket_id}: files must be a list")
        lanes.append(
            Lane(
                lane_id=f"lane-{index:02d}-{ticket_id}",
                ticket=ticket_id,
                title=str(ticket["title"]),
                expected=Expected(
                    branch=branch,
                    files=[str(f) for f in files_raw],
                    test_command=str(ticket["test_command"]),
                ),
                worktree=str(ticket.get("worktree") or f".claude/worktrees/{branch.replace('/', '-')}"),
            )
        )
    state = RunState(run_id=run_id, repo=str(repo), base_ref=base_ref, created_at=_now(), lanes=lanes)
    state.save(state_path)
    return state


def record_dispatch(state: RunState, lane_id: str, agent_name: str | None, *, dry_run: bool) -> Lane:
    lane = state.lane(lane_id)
    stamp = _now()
    if not dry_run and lane.attempts and all(a.dry_run for a in lane.attempts):
        # A rehearsal must not spend the real ladder: the first real dispatch
        # after dry-run-only attempts starts the lane fresh (attempts stay as history).
        lane.escalation = {"nudged": False, "redispatched": False}
        lane.blocked = None
    lane.attempts.append(Attempt(agent_name=agent_name, dispatched_at=stamp, dry_run=dry_run))
    lane.dispatched_at = stamp
    lane.agent_name = agent_name
    lane.status = STATUS_REDISPATCHED if lane.escalation["redispatched"] else STATUS_DISPATCHED
    return lane


# ---------------------------------------------------------------- verification


@dataclass
class Verification:
    """Evidence gathered from git and the test runner — never from the lane."""

    checked_at: str
    branch_exists: bool
    commits_ahead: list[str]
    changed_files: list[str]
    missing_expected_files: list[str]
    test_command: str
    test_ran: bool
    test_exit_code: int | None
    test_output_tail: str
    landed: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def verify_lane(
    state: RunState,
    lane_id: str,
    *,
    run_tests: bool = True,
    test_timeout: int = 1800,
) -> Verification:
    lane = state.lane(lane_id)
    repo = Path(state.repo)
    branch = lane.expected.branch
    reasons: list[str] = []

    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    commits: list[str] = []
    changed: list[str] = []
    if exists:
        log = _git(repo, "log", "--format=%h %s", f"{state.base_ref}..{branch}")
        commits = [line for line in log.stdout.splitlines() if line.strip()]
        diff = _git(repo, "diff", "--name-only", f"{state.base_ref}...{branch}")
        changed = sorted(line for line in diff.stdout.splitlines() if line.strip())
        if not commits:
            reasons.append(f"branch {branch} has no commits ahead of {state.base_ref}")
    else:
        reasons.append(f"branch {branch} does not exist")

    missing = sorted(f for f in lane.expected.files if f not in changed)
    if exists and missing:
        reasons.append(f"expected files not changed on {branch}: {', '.join(missing)}")

    test_ran = False
    exit_code: int | None = None
    tail = ""
    if run_tests and exists and commits and not missing:
        cwd = repo / lane.worktree
        if not cwd.is_dir():
            cwd = repo
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{cwd / 'src'}:{cwd / 'elspeth-lints' / 'src'}"
        try:
            proc = subprocess.run(
                shlex.split(lane.expected.test_command),
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=test_timeout,
                check=False,
            )
            test_ran = True
            exit_code = proc.returncode
            tail = (proc.stdout + proc.stderr)[-2000:]
            if exit_code != 0:
                reasons.append(f"test command exited {exit_code}")
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            reasons.append(f"test command could not complete: {exc.__class__.__name__}")
    elif run_tests:
        reasons.append("tests not run: prerequisite git evidence failed")

    landed = exists and bool(commits) and not missing and test_ran and exit_code == 0
    verification = Verification(
        checked_at=_now(),
        branch_exists=exists,
        commits_ahead=commits,
        changed_files=changed,
        missing_expected_files=missing,
        test_command=lane.expected.test_command,
        test_ran=test_ran,
        test_exit_code=exit_code,
        test_output_tail=tail,
        landed=landed,
        reasons=reasons,
    )
    lane.verifications.append(verification.as_dict())
    if landed:
        lane.status = STATUS_LANDED
        lane.landed_at = verification.checked_at
        if lane.attempts:
            lane.attempts[-1].outcome = "landed"
    return verification


# ---------------------------------------------------------------- idle verdict


@dataclass
class IdleVerdict:
    checked_at: str
    listed_by_list_agents: bool
    worktree_exists: bool
    uncommitted_changes: int
    seconds_since_activity: float | None
    verdict: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _latest_mtime(root: Path) -> float | None:
    latest: float | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", ".venv", "__pycache__", "node_modules"}]
        for name in filenames:
            if name == ".git":  # linked worktree gitfile: harness-written, not lane activity
                continue
            try:
                mtime = (Path(dirpath) / name).stat().st_mtime
            except FileNotFoundError:
                continue
            if latest is None or mtime > latest:
                latest = mtime
    return latest


def idle_verdict(
    state: RunState,
    lane_id: str,
    *,
    listed: bool,
    activity_window: int = DEFAULT_ACTIVITY_WINDOW_SECONDS,
) -> IdleVerdict:
    """Decide whether an idle lane is alive.

    ``listed`` is the result of the orchestrator's ListAgents call (check 1).
    Check 2 inspects the worktree: uncommitted changes or a file touched inside
    ``activity_window`` seconds counts as life. Dead only when BOTH fail.
    """
    lane = state.lane(lane_id)
    worktree = Path(state.repo) / lane.worktree
    exists = worktree.is_dir()
    uncommitted = 0
    since: float | None = None
    if exists:
        status = _git(worktree, "status", "--porcelain")
        uncommitted = len([line for line in status.stdout.splitlines() if line.strip()])
        latest = _latest_mtime(worktree)
        if latest is not None:
            since = time.time() - latest

    if listed:
        verdict = VERDICT_ALIVE_LISTED
    elif exists and (uncommitted > 0 or (since is not None and since <= activity_window)):
        verdict = VERDICT_ALIVE_WORKTREE
    else:
        verdict = VERDICT_DEAD

    result = IdleVerdict(
        checked_at=_now(),
        listed_by_list_agents=listed,
        worktree_exists=exists,
        uncommitted_changes=uncommitted,
        seconds_since_activity=since,
        verdict=verdict,
    )
    lane.idle_checks.append(result.as_dict())
    return result


# ---------------------------------------------------------------- escalation


def escalate(state: RunState, lane_id: str, *, reason: str) -> str:
    """Advance one rung on the ladder and return the action the orchestrator must take."""
    lane = state.lane(lane_id)
    if lane.status == STATUS_LANDED:
        raise ValueError(f"{lane_id} is landed; nothing to escalate")
    if lane.status == STATUS_BLOCKED:
        return ACTION_BLOCK
    if lane.attempts:
        lane.attempts[-1].outcome = reason
    if not lane.escalation["nudged"]:
        lane.escalation["nudged"] = True
        lane.status = STATUS_NUDGED
        return ACTION_NUDGE
    if not lane.escalation["redispatched"]:
        lane.escalation["redispatched"] = True
        lane.status = STATUS_REDISPATCHED
        return ACTION_REDISPATCH
    lane.status = STATUS_BLOCKED
    lane.blocked = {
        "blocked_at": _now(),
        "reason": reason,
        "attempts": [asdict(a) for a in lane.attempts],
        "last_verification": lane.verifications[-1] if lane.verifications else None,
        "last_idle_check": lane.idle_checks[-1] if lane.idle_checks else None,
    }
    return ACTION_BLOCK


# ---------------------------------------------------------------- report


def merge_order(lanes: list[Lane]) -> list[Lane]:
    """Propose an order for landed lanes: disjoint file sets first, then by overlap count, then by landing time."""
    landed = [lane for lane in lanes if lane.status == STATUS_LANDED]

    def overlap(lane: Lane) -> int:
        mine = set(lane.expected.files)
        return sum(1 for other in landed if other is not lane and mine & set(other.expected.files))

    return sorted(landed, key=lambda lane: (overlap(lane), lane.landed_at or "", lane.lane_id))


def render_report(state: RunState) -> str:
    landed = [lane for lane in state.lanes if lane.status == STATUS_LANDED]
    blocked = [lane for lane in state.lanes if lane.status == STATUS_BLOCKED]
    other = [lane for lane in state.lanes if lane.status not in {STATUS_LANDED, STATUS_BLOCKED}]
    dry = any(a.dry_run for lane in state.lanes for a in lane.attempts)

    lines = [f"# lane-manager report — run `{state.run_id}`", ""]
    lines.append(f"Base ref: `{state.base_ref}` · repo: `{state.repo}` · created {state.created_at}")
    if dry:
        lines.append("")
        lines.append("**DRY RUN** — no subagents were dispatched; verification ran against the real tree.")
    lines += ["", f"## Landed ({len(landed)})", ""]
    if not landed:
        lines.append("_none_")
    for lane in landed:
        v = lane.verifications[-1]
        lines.append(f"- **{lane.lane_id}** `{lane.ticket}` — {lane.title}")
        lines.append(f"  - branch `{lane.expected.branch}`: {len(v['commits_ahead'])} commit(s) ahead of `{state.base_ref}`")
        lines.append(f"  - files changed: {', '.join(f'`{f}`' for f in v['changed_files']) or '_none_'}")
        lines.append(f"  - tests: `{lane.expected.test_command}` → exit {v['test_exit_code']}")
    lines += ["", f"## Blocked ({len(blocked)})", ""]
    if not blocked:
        lines.append("_none_")
    for lane in blocked:
        b = lane.blocked or {}
        lines.append(f"- **{lane.lane_id}** `{lane.ticket}` — {lane.title}")
        lines.append(f"  - reason: {b.get('reason')}")
        lines.append(
            f"  - ladder: nudged={lane.escalation['nudged']} redispatched={lane.escalation['redispatched']} attempts={len(lane.attempts)}"
        )
        for v in lane.verifications:
            lines.append(f"  - verification {v['checked_at']}: " + ("; ".join(v["reasons"]) or "landed"))
        for c in lane.idle_checks:
            lines.append(
                f"  - idle check {c['checked_at']}: listed={c['listed_by_list_agents']} "
                f"worktree={c['worktree_exists']} uncommitted={c['uncommitted_changes']} → {c['verdict']}"
            )
    if other:
        lines += ["", f"## Still open ({len(other)})", ""]
        for lane in other:
            lines.append(f"- **{lane.lane_id}** `{lane.ticket}` — status `{lane.status}`")
    lines += ["", "## Merge order proposal", ""]
    ordered = merge_order(state.lanes)
    if not ordered:
        lines.append("_nothing to merge_")
    for index, lane in enumerate(ordered, start=1):
        lines.append(f"{index}. `{lane.expected.branch}` ({lane.ticket}) — `git merge --no-ff {lane.expected.branch}`")
    lines.append("")
    lines.append(
        "Ordering heuristic: lanes whose expected files overlap no other landed lane go first; "
        "overlapping lanes follow by ascending overlap count, then landing time. Re-run the full suite after each merge."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def _cmd_init(args: argparse.Namespace) -> int:
    tickets = json.loads(Path(args.tickets).read_text(encoding="utf-8"))
    if type(tickets) is not list:
        print("tickets file must contain a JSON list", file=sys.stderr)
        return 2
    state = init_run(
        state_path=Path(args.state),
        run_id=args.run_id,
        repo=Path(args.repo).resolve(),
        base_ref=args.base_ref,
        tickets=tickets,
    )
    print(json.dumps({"run_id": state.run_id, "lanes": [lane.lane_id for lane in state.lanes]}, indent=2))
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    path = Path(args.state)
    state = RunState.load(path)
    lane = record_dispatch(state, args.lane, args.agent_name, dry_run=args.dry_run)
    state.save(path)
    print(json.dumps({"lane_id": lane.lane_id, "status": lane.status, "attempt": len(lane.attempts)}))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.state)
    state = RunState.load(path)
    verification = verify_lane(state, args.lane, run_tests=not args.skip_tests, test_timeout=args.test_timeout)
    state.save(path)
    print(json.dumps(verification.as_dict(), indent=2))
    return 0 if verification.landed else 1


def _cmd_idle(args: argparse.Namespace) -> int:
    path = Path(args.state)
    state = RunState.load(path)
    verdict = idle_verdict(state, args.lane, listed=args.listed, activity_window=args.activity_window)
    state.save(path)
    print(json.dumps(verdict.as_dict(), indent=2))
    return 0 if verdict.verdict != VERDICT_DEAD else 1


def _cmd_escalate(args: argparse.Namespace) -> int:
    path = Path(args.state)
    state = RunState.load(path)
    action = escalate(state, args.lane, reason=args.reason)
    state.save(path)
    print(json.dumps({"lane_id": args.lane, "action": action, "status": state.lane(args.lane).status}))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    state = RunState.load(Path(args.state))
    text = render_report(state)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a run state file from a JSON ticket list")
    p.add_argument("--state", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--base-ref", required=True)
    p.add_argument("--tickets", required=True, help="JSON list of {ticket,title,files,test_command[,branch]}")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("dispatch", help="record a dispatch attempt for a lane")
    p.add_argument("--state", required=True)
    p.add_argument("--lane", required=True)
    p.add_argument("--agent-name", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_dispatch)

    p = sub.add_parser("verify", help="verify a lane from git + tests; exit 1 unless landed")
    p.add_argument("--state", required=True)
    p.add_argument("--lane", required=True)
    p.add_argument("--skip-tests", action="store_true")
    p.add_argument("--test-timeout", type=int, default=1800)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("idle", help="decide whether an idle lane is alive; exit 1 if dead")
    p.add_argument("--state", required=True)
    p.add_argument("--lane", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--listed", dest="listed", action="store_true", help="ListAgents showed the lane's agent")
    group.add_argument("--not-listed", dest="listed", action="store_false", help="ListAgents did not show it")
    p.add_argument("--activity-window", type=int, default=DEFAULT_ACTIVITY_WINDOW_SECONDS)
    p.set_defaults(func=_cmd_idle)

    p = sub.add_parser("escalate", help="advance one ladder rung; prints nudge|redispatch|block")
    p.add_argument("--state", required=True)
    p.add_argument("--lane", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_escalate)

    p = sub.add_parser("report", help="render the markdown report")
    p.add_argument("--state", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
