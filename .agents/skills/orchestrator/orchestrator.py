#!/usr/bin/env python3
"""Quota-aware multi-lane orchestrator: single-writer lanes, heartbeats, resume.

This is the concurrency and quota CONTROL PLANE. It is deliberately not a
second copy of ``lane-manager``: that skill owns a lane's verification and
merge evidence under ``.lanes/``, and this owns who is allowed to write to a
lane at all, on which model, and how far it got. The two join on ``lane_id``.

Three failure modes it exists to prevent:

1. a subagent spawned against a quota-exhausted model dying silently --
   ``preflight`` refuses to spawn on a model observed exhausted, and reports
   which models are viable;
2. a usage-limit interruption leaving no record of what each lane was doing --
   every lane carries its worktree, branch, model, task, phase and a monotonic
   step log, and ``status`` flags a quiet lane as SUSPECTED DEAD with elapsed;
3. a forked duplicate instance becoming a phantom second writer -- ``attach``
   mints a random fencing token and EVERY mutation is fenced on it, so a
   second instance fails loudly and changes nothing.

The fencing token is minted from ``secrets`` at attach time and is not a
function of the environment. That is the whole defence against a fork: a fork
reproduces the session, the agent name, the lane, the cwd and even the PID, so
any identity derived from those reads as a legitimate re-attach by the true
holder. Only fresh randomness distinguishes them.

Liveness is tri-state and DEFAULT DENY. A lock is stolen automatically only
when the holder is provably gone (its process has exited, or the machine has
rebooted since). "Cannot tell" is never treated as dead -- that is precisely
how a phantom second writer is manufactured. Use ``evict --force`` naming the
fingerprint to break a lock the tool will not break on its own.

Run ``python orchestrator.py --help`` for the CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TypedDict

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOCK_HELD = 3
EXIT_MODEL_UNAVAILABLE = 4
EXIT_DRIFT = 5

STATE_AVAILABLE = "available"
STATE_EXHAUSTED = "exhausted"
STATE_UNKNOWN = "unknown"

LIVE_UNLOCKED = "unlocked"
LIVE_ALIVE = "alive"
LIVE_DEAD_PROCESS = "dead-process"
LIVE_DEAD_BOOT = "dead-boot"
LIVE_UNKNOWN = "unknown"

# Verdicts that prove the holder is gone. Anything else -- including "unknown"
# -- keeps the lock, because an unverifiable lock silently treated as stale is
# how two writers end up on one worktree.
PROVABLY_DEAD = frozenset({LIVE_DEAD_PROCESS, LIVE_DEAD_BOOT})

DEFAULT_STALE_AFTER = 900.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS lanes (
    lane_id          TEXT PRIMARY KEY,
    worktree         TEXT NOT NULL,
    branch           TEXT NOT NULL,
    model            TEXT NOT NULL,
    task             TEXT NOT NULL,
    phase            TEXT NOT NULL DEFAULT 'pending',
    heartbeat_at     REAL,
    fingerprint      TEXT,
    holder_agent     TEXT,
    holder_pid       INTEGER,
    holder_starttime INTEGER,
    holder_boot_id   TEXT,
    attached_at      REAL,
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    lane_id      TEXT NOT NULL REFERENCES lanes(lane_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    verify_cmd   TEXT NOT NULL,
    completed_at REAL,
    completed_by TEXT,
    PRIMARY KEY (lane_id, seq)
);

CREATE TABLE IF NOT EXISTS model_state (
    model       TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    observed_at REAL NOT NULL,
    resets_at   REAL,
    source      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_id             TEXT NOT NULL,
    evicted_fingerprint TEXT NOT NULL,
    evicted_by          TEXT NOT NULL,
    reason              TEXT NOT NULL,
    liveness            TEXT NOT NULL,
    at                  REAL NOT NULL
);
"""


class Drift(TypedDict):
    """A disagreement between the step log and the tree, found by re-verification."""

    kind: str
    seq: int
    name: str
    detail: str


class ModelVerdict(TypedDict, total=False):
    """What is known about one model's quota. ``verified`` false means unknown."""

    model: str
    state: str
    verified: bool
    viable: bool
    resets_at: float | None
    observed_at: float | None
    source: str | None
    note: str
    viable_alternatives: list[str]
    refused: bool


class LaneReport(TypedDict, total=False):
    """One lane as ``status`` and ``resume`` report it."""

    lane_id: str
    worktree: str
    branch: str
    model: str
    task: str
    phase: str
    fingerprint: str | None
    holder_pid: int | None
    liveness: str
    heartbeat_age_seconds: float | None
    suspected_dead: bool
    safe_to_resume: bool
    steps_total: int
    steps_done: int
    action: str
    reason: str
    drift: list[Drift]
    restart_at_seq: int | None
    restart_at_name: str | None
    model_state: ModelVerdict
    took_unknown_lock: bool


class OrchestratorError(Exception):
    """A refusal that carries the exit code the operator should see."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ process


def boot_id() -> str:
    """An identifier for the current boot, so a pre-reboot lock is provably stale."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def process_starttime(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``: the tick the process started at.

    Recorded alongside the PID so a recycled PID cannot impersonate the holder.
    ``None`` means the process is gone or unreadable.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The comm field is parenthesised and may contain spaces, so split after it.
    tail = raw.rpartition(")")[2].split()
    if len(tail) < 20:
        return None
    try:
        return int(tail[19])
    except ValueError:
        return None


def mint_fingerprint(agent: str) -> str:
    """A fresh fencing token for one attach.

    The ``secrets`` component is the entire defence against a forked duplicate
    instance. A fork reproduces the agent name, the lane, the cwd, the session
    and possibly the PID, so every other term here is decoration for humans
    reading ``status``; only the nonce makes two instances distinguishable.
    Anything that makes this a pure function of the environment reopens the
    phantom-second-writer hole.
    """
    return f"{agent}-{os.getpid()}-{secrets.token_hex(16)}"


def liveness_of(row: sqlite3.Row) -> str:
    """Tri-state verdict on a lane's lock. Never guesses toward dead."""
    if row["fingerprint"] is None:
        return LIVE_UNLOCKED
    recorded_boot = row["holder_boot_id"]
    current_boot = boot_id()
    if recorded_boot and current_boot and recorded_boot != current_boot:
        return LIVE_DEAD_BOOT
    pid = row["holder_pid"]
    if pid is None:
        # No process was ever pinned to this lock (an agent lane, or a holder on
        # another host). Its death cannot be proven, so the lock stands.
        return LIVE_UNKNOWN
    if not Path("/proc").is_dir():
        return LIVE_UNKNOWN
    current = process_starttime(int(pid))
    if current is None:
        return LIVE_DEAD_PROCESS
    recorded = row["holder_starttime"]
    if recorded is not None and int(recorded) != current:
        # The PID was recycled onto an unrelated process.
        return LIVE_DEAD_PROCESS
    return LIVE_ALIVE


def humanise(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60}s"
    return f"{total // 3600}h{(total % 3600) // 60}m"


# ----------------------------------------------------------------- database


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction: commit on success, roll back on ANY exception.

    Hand-rolled BEGIN/ROLLBACK pairs leaked an open transaction whenever a
    refusal came from a helper (``lane_or_die``, ``fence``) rather than from the
    command body, because those paths re-raised without rolling back. Routing
    every writer through here makes that unrepresentable.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def lane_or_die(conn: sqlite3.Connection, lane_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute("SELECT * FROM lanes WHERE lane_id = ?", (lane_id,)).fetchone()
    if row is None:
        raise OrchestratorError(f"no such lane: {lane_id!r}")
    return row


def fence(conn: sqlite3.Connection, lane_id: str, fingerprint: str, now: float) -> sqlite3.Row:
    """Assert ``fingerprint`` holds ``lane_id``, refusing loudly when it does not.

    Every mutation goes through here. A write from an instance that no longer
    holds the lane is the phantom second writer, and it must not land.
    """
    row = lane_or_die(conn, lane_id)
    held = row["fingerprint"]
    if held is None:
        raise OrchestratorError(
            f"LOCK NOT HELD: lane {lane_id!r} has no holder; attach before writing (refused fingerprint={fingerprint})",
            EXIT_LOCK_HELD,
        )
    if held != fingerprint:
        raise OrchestratorError(
            f"LOCK HELD by another instance: lane {lane_id!r} holder={held} "
            f"refused={fingerprint}. This write was rejected and nothing changed.",
            EXIT_LOCK_HELD,
        )
    conn.execute("UPDATE lanes SET heartbeat_at = ? WHERE lane_id = ? AND fingerprint = ?", (now, lane_id, fingerprint))
    return row


# ----------------------------------------------------------------- commands


def cmd_init(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    return EXIT_OK


def cmd_register(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    steps: list[dict[str, str]] = []
    if args.steps_json:
        raw = json.loads(Path(args.steps_json).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise OrchestratorError("--steps-json must contain a list of steps")
        for entry in raw:
            if not isinstance(entry, dict) or "name" not in entry or "verify_cmd" not in entry:
                raise OrchestratorError(f"each step needs a name and a verify_cmd: {entry!r}")
            steps.append({"name": str(entry["name"]), "verify_cmd": str(entry["verify_cmd"])})

    with transaction(conn):
        conn.execute(
            "INSERT INTO lanes (lane_id, worktree, branch, model, task, phase, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?) "
            "ON CONFLICT(lane_id) DO UPDATE SET worktree=excluded.worktree, "
            "branch=excluded.branch, model=excluded.model, task=excluded.task",
            (args.lane, str(Path(args.worktree)), args.branch, args.model, args.task, now),
        )
        conn.execute("DELETE FROM steps WHERE lane_id = ? AND completed_at IS NULL", (args.lane,))
        for index, step in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO steps (lane_id, seq, name, verify_cmd) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(lane_id, seq) DO UPDATE SET name=excluded.name, "
                "verify_cmd=excluded.verify_cmd",
                (args.lane, index, step["name"], step["verify_cmd"]),
            )
    print(json.dumps({"lane_id": args.lane, "steps": len(steps)}))
    return EXIT_OK


def cmd_attach(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    candidate = args.fingerprint or mint_fingerprint(args.agent)

    with transaction(conn):
        row = lane_or_die(conn, args.lane)
        held = row["fingerprint"]
        verdict = liveness_of(row)

        if held is not None and held != candidate:
            if verdict not in PROVABLY_DEAD:
                detail = (
                    "its holder process is still running"
                    if verdict == LIVE_ALIVE
                    else "its holder cannot be proven dead (no pinned process)"
                )
                raise OrchestratorError(
                    f"LOCK HELD: lane {args.lane!r} is held and {detail}.\n"
                    f"  holder={held}\n"
                    f"  refused={candidate}\n"
                    f"  liveness={verdict}\n"
                    "Nothing was changed. If you are certain the holder is gone, run:\n"
                    f"  orchestrator evict --lane {args.lane} --fingerprint {held} "
                    '--force --reason "<why>"',
                    EXIT_LOCK_HELD,
                )
            conn.execute(
                "INSERT INTO evictions (lane_id, evicted_fingerprint, evicted_by, reason, liveness, at) VALUES (?, ?, ?, ?, ?, ?)",
                (args.lane, held, candidate, f"holder provably gone ({verdict})", verdict, now),
            )

        pid = args.pid
        conn.execute(
            "UPDATE lanes SET fingerprint = ?, holder_agent = ?, holder_pid = ?, "
            "holder_starttime = ?, holder_boot_id = ?, attached_at = ?, heartbeat_at = ? "
            "WHERE lane_id = ?",
            (
                candidate,
                args.agent,
                pid,
                process_starttime(pid) if pid is not None else None,
                boot_id(),
                now,
                now,
                args.lane,
            ),
        )

    print(
        json.dumps(
            {
                "lane_id": args.lane,
                "fingerprint": candidate,
                "agent": args.agent,
                "pid": args.pid,
                "attached_at": now,
            }
        )
    )
    return EXIT_OK


def cmd_release(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    with transaction(conn):
        fence(conn, args.lane, args.fingerprint, time.time())
        conn.execute(
            "UPDATE lanes SET fingerprint = NULL, holder_agent = NULL, holder_pid = NULL, "
            "holder_starttime = NULL, holder_boot_id = NULL WHERE lane_id = ? AND fingerprint = ?",
            (args.lane, args.fingerprint),
        )
    return EXIT_OK


def cmd_heartbeat(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    with transaction(conn):
        fence(conn, args.lane, args.fingerprint, time.time())
    return EXIT_OK


def cmd_step_done(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    with transaction(conn):
        # One transaction stamps the heartbeat AND the step, so "a heartbeat on
        # every step" is a property of the schema rather than a convention.
        fence(conn, args.lane, args.fingerprint, now)
        changed = conn.execute(
            "UPDATE steps SET completed_at = ?, completed_by = ? WHERE lane_id = ? AND seq = ?",
            (now, args.fingerprint, args.lane, args.seq),
        ).rowcount
        if changed != 1:
            raise OrchestratorError(f"lane {args.lane!r} has no step {args.seq}")
    return EXIT_OK


def cmd_set_phase(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    with transaction(conn):
        fence(conn, args.lane, args.fingerprint, now)
        conn.execute(
            "UPDATE lanes SET phase = ? WHERE lane_id = ? AND fingerprint = ?",
            (args.phase, args.lane, args.fingerprint),
        )
    return EXIT_OK


def cmd_evict(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    with transaction(conn):
        row = lane_or_die(conn, args.lane)
        held = row["fingerprint"]
        verdict = liveness_of(row)
        if held is None:
            raise OrchestratorError(f"lane {args.lane!r} is not locked; nothing to evict")
        if held != args.fingerprint:
            raise OrchestratorError(
                f"EVICTION REFUSED: lane {args.lane!r} is held by {held}, not {args.fingerprint}. Name the fingerprint you are evicting.",
                EXIT_LOCK_HELD,
            )
        if verdict == LIVE_ALIVE and not args.force:
            raise OrchestratorError(
                f"EVICTION REFUSED: the holder of {args.lane!r} is still running. Pass --force only when you know the instance is gone.",
                EXIT_LOCK_HELD,
            )
        conn.execute(
            "INSERT INTO evictions (lane_id, evicted_fingerprint, evicted_by, reason, liveness, at) VALUES (?, ?, ?, ?, ?, ?)",
            (args.lane, held, "operator", args.reason, verdict, now),
        )
        conn.execute(
            "UPDATE lanes SET fingerprint = NULL, holder_agent = NULL, holder_pid = NULL, "
            "holder_starttime = NULL, holder_boot_id = NULL WHERE lane_id = ?",
            (args.lane,),
        )
    return EXIT_OK


def cmd_liveness(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    row = lane_or_die(conn, args.lane)
    verdict = liveness_of(row)
    payload = {
        "lane_id": args.lane,
        "verdict": verdict,
        "fingerprint": row["fingerprint"],
        "holder_pid": row["holder_pid"],
        "safe_to_resume": verdict in PROVABLY_DEAD or verdict == LIVE_UNLOCKED,
    }
    if args.json:
        print(json.dumps(payload))
    else:
        print(f"{args.lane}: {verdict} (holder={row['fingerprint']})")
    return EXIT_OK


def lane_report(row: sqlite3.Row, stale_after: float, now: float) -> LaneReport:
    verdict = liveness_of(row)
    heartbeat = row["heartbeat_at"]
    elapsed = None if heartbeat is None else now - float(heartbeat)
    suspected = row["fingerprint"] is not None and elapsed is not None and elapsed > stale_after
    report: LaneReport = {
        "lane_id": row["lane_id"],
        "worktree": row["worktree"],
        "branch": row["branch"],
        "model": row["model"],
        "task": row["task"],
        "phase": row["phase"],
        "fingerprint": row["fingerprint"],
        "holder_pid": row["holder_pid"],
        "liveness": verdict,
        "heartbeat_age_seconds": None if elapsed is None else round(elapsed, 1),
        "suspected_dead": suspected,
        # A quiet lane whose process is still running is NOT resumable: taking
        # it over is exactly the phantom-second-writer move.
        "safe_to_resume": verdict in PROVABLY_DEAD or verdict == LIVE_UNLOCKED,
    }
    return report


def cmd_status(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    rows = conn.execute("SELECT * FROM lanes ORDER BY lane_id").fetchall()
    reports = [lane_report(row, args.stale_after, now) for row in rows]
    for report in reports:
        steps = conn.execute(
            "SELECT seq, name, completed_at FROM steps WHERE lane_id = ? ORDER BY seq",
            (report["lane_id"],),
        ).fetchall()
        report["steps_total"] = len(steps)
        report["steps_done"] = sum(1 for s in steps if s["completed_at"] is not None)

    if args.json:
        print(json.dumps(reports, indent=2))
        return EXIT_OK

    if not reports:
        print("no lanes registered")
        return EXIT_OK
    for report in reports:
        age = report["heartbeat_age_seconds"]
        if report["suspected_dead"] and age is not None:
            elapsed = f"{int(age)}s / {humanise(float(age))}"
            holder = "holder process ALIVE - DO NOT resume" if report["liveness"] == LIVE_ALIVE else f"holder {report['liveness']}"
            flag = f"SUSPECTED DEAD (quiet {elapsed}; {holder})"
        elif report["fingerprint"] is None:
            flag = "unlocked"
        else:
            flag = f"running ({report['liveness']})"
        print(
            f"{report['lane_id']:<24} {flag}\n"
            f"    model={report['model']}  phase={report['phase']}  "
            f"steps={report['steps_done']}/{report['steps_total']}\n"
            f"    worktree={report['worktree']}  branch={report['branch']}\n"
            f"    task={report['task']}"
        )
    return EXIT_OK


# ------------------------------------------------------------------- models


def model_verdict(conn: sqlite3.Connection, model: str, now: float) -> ModelVerdict:
    """What is known about ``model``. Absence of knowledge is never viability."""
    row = conn.execute("SELECT * FROM model_state WHERE model = ?", (model,)).fetchone()
    if row is None:
        unknown: ModelVerdict = {
            "model": model,
            "state": STATE_UNKNOWN,
            "verified": False,
            "viable": True,
            "resets_at": None,
            "observed_at": None,
            "source": None,
            "note": "no observation on record: unknown, not confirmed available",
        }
        return unknown
    state: str = row["state"]
    resets_at: float | None = row["resets_at"]
    exhausted_now = state == STATE_EXHAUSTED and (resets_at is None or float(resets_at) > now)
    known: ModelVerdict = {
        "model": model,
        "state": state,
        "verified": state in (STATE_AVAILABLE, STATE_EXHAUSTED),
        "viable": not exhausted_now,
        "resets_at": resets_at,
        "observed_at": row["observed_at"],
        "source": row["source"],
        "note": "",
    }
    return known


def cmd_observe_model(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    resets_at = None if args.resets_in is None else now + args.resets_in
    conn.execute(
        "INSERT INTO model_state (model, state, observed_at, resets_at, source) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(model) DO UPDATE SET state=excluded.state, "
        "observed_at=excluded.observed_at, resets_at=excluded.resets_at, source=excluded.source",
        (args.model, args.state, now, resets_at, args.source),
    )
    return EXIT_OK


def viable_models(conn: sqlite3.Connection, now: float, exclude: str) -> list[str]:
    rows = conn.execute("SELECT model FROM model_state ORDER BY model").fetchall()
    viable = []
    for row in rows:
        if row["model"] == exclude:
            continue
        if model_verdict(conn, row["model"], now)["viable"]:
            viable.append(row["model"])
    return viable


def cmd_preflight(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    verdict = model_verdict(conn, args.model, now)
    alternatives = viable_models(conn, now, exclude=args.model)
    verdict["viable_alternatives"] = alternatives

    refused = not verdict["viable"] or (args.require_verified and not verdict["verified"])
    verdict["refused"] = refused

    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        if refused and not verdict["viable"]:
            resets = verdict["resets_at"]
            when = "" if resets is None else f" until {time.strftime('%H:%M:%S', time.localtime(float(resets)))}"
            print(f"REFUSED: {args.model} is EXHAUSTED{when}")
        elif refused:
            print(f"REFUSED: {args.model} is {verdict['state'].upper()} and --require-verified was set")
        else:
            print(
                f"{args.model}: {verdict['state'].upper()}"
                + ("" if verdict["verified"] else " (unverified - not a guarantee of availability)")
            )
        print("viable models: " + (", ".join(alternatives) if alternatives else "(none observed viable)"))
    return EXIT_MODEL_UNAVAILABLE if refused else EXIT_OK


# ------------------------------------------------------------------- resume


def run_verify(worktree: str, command: str) -> bool:
    """Re-measure a step. The log is a claim; this is the measurement."""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def resume_lane(conn: sqlite3.Connection, row: sqlite3.Row, args: argparse.Namespace, now: float) -> LaneReport:
    report = lane_report(row, args.stale_after, now)
    verdict = report["liveness"]

    taking_unknown = verdict == LIVE_UNKNOWN and args.take_unknown_locks
    if verdict == LIVE_ALIVE or (verdict == LIVE_UNKNOWN and not taking_unknown):
        report["action"] = "skip"
        report["reason"] = (
            "holder process is running"
            if verdict == LIVE_ALIVE
            else "holder cannot be proven dead; --take-unknown-locks or evict --force to take it over"
        )
        report["drift"] = []
        report["restart_at_seq"] = None
        report["restart_at_name"] = None
        return report

    model = model_verdict(conn, row["model"], now)
    if not model["viable"]:
        report["action"] = "blocked-on-model"
        report["reason"] = f"model {row['model']} is exhausted"
        report["model_state"] = model
        report["drift"] = []
        report["restart_at_seq"] = None
        report["restart_at_name"] = None
        return report

    if not Path(row["worktree"]).is_dir():
        # Without the tree there is nothing to measure. Saying "restart at step 1"
        # here would be a guess dressed as a finding: every verify_cmd fails for
        # the same reason, and none of them failed because the work is undone.
        report["action"] = "blocked-worktree-missing"
        report["reason"] = f"worktree {row['worktree']} does not exist; re-create it before resuming"
        report["drift"] = []
        report["restart_at_seq"] = None
        report["restart_at_name"] = None
        return report

    steps = conn.execute("SELECT * FROM steps WHERE lane_id = ? ORDER BY seq", (row["lane_id"],)).fetchall()

    drift: list[Drift] = []
    restart_seq: int | None = None
    restart_name: str | None = None

    for step in steps:
        logged = step["completed_at"] is not None
        actually_done = run_verify(row["worktree"], step["verify_cmd"])

        if logged and not actually_done:
            # The log says done, the tree disagrees. The tree wins.
            drift.append(
                {
                    "kind": "log-wrong",
                    "seq": step["seq"],
                    "name": step["name"],
                    "detail": "logged complete but re-verification failed; the log was repaired",
                }
            )
        elif actually_done and not logged:
            # Work nobody claimed. This is the signature of a second writer.
            drift.append(
                {
                    "kind": "unlogged-work",
                    "seq": step["seq"],
                    "name": step["name"],
                    "detail": "the tree satisfies a step no instance logged - possible second writer",
                }
            )

        if not actually_done and restart_seq is None:
            restart_seq = int(step["seq"])
            restart_name = str(step["name"])

    report["action"] = "resume"
    report["drift"] = drift
    report["restart_at_seq"] = restart_seq
    report["restart_at_name"] = restart_name
    report["reason"] = "all steps re-verified" if restart_seq is None else f"first unsatisfied step is {restart_seq} ({restart_name})"

    if args.execute and taking_unknown and row["fingerprint"] is not None:
        conn.execute(
            "INSERT INTO evictions (lane_id, evicted_fingerprint, evicted_by, reason, liveness, at) VALUES (?, ?, ?, ?, ?, ?)",
            (row["lane_id"], row["fingerprint"], "resume --take-unknown-locks", "operator declared the instance gone", verdict, now),
        )
        conn.execute(
            "UPDATE lanes SET fingerprint = NULL, holder_agent = NULL, holder_pid = NULL, "
            "holder_starttime = NULL, holder_boot_id = NULL WHERE lane_id = ?",
            (row["lane_id"],),
        )
    if taking_unknown:
        report["took_unknown_lock"] = True

    if args.execute:
        for item in drift:
            if item["kind"] == "log-wrong":
                conn.execute(
                    "UPDATE steps SET completed_at = NULL, completed_by = NULL WHERE lane_id = ? AND seq = ?",
                    (row["lane_id"], item["seq"]),
                )
    return report


def cmd_resume(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = time.time()
    if args.lane:
        rows = [lane_or_die(conn, args.lane)]
    else:
        rows = conn.execute("SELECT * FROM lanes ORDER BY lane_id").fetchall()

    reports = [resume_lane(conn, row, args, now) for row in rows]

    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        if not args.execute:
            print("DRY RUN - nothing was written. Re-run with --execute to repair the step log.\n")
        for report in reports:
            print(f"{report['lane_id']}: {report['action']} ({report.get('reason', '')})")
            if report["action"] == "resume":
                print(f"    worktree={report['worktree']}  branch={report['branch']}  model={report['model']}")
                if report["restart_at_seq"] is None:
                    print("    every step re-verified; nothing to restart")
                else:
                    print(f"    restart at step {report['restart_at_seq']} ({report['restart_at_name']})")
            for item in report.get("drift", []):
                print(f"    DRIFT [{item['kind']}] step {item['seq']} ({item['name']}): {item['detail']}")

    if any(r["action"] == "blocked-on-model" for r in reports):
        return EXIT_MODEL_UNAVAILABLE
    if any(r["action"] == "blocked-worktree-missing" for r in reports):
        return EXIT_ERROR
    if any(r.get("drift") for r in reports):
        return EXIT_DRIFT
    return EXIT_OK


# ---------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Quota-aware multi-lane orchestrator: single-writer lanes, heartbeats, model preflight and evidence-based resume.",
    )
    parser.add_argument("--db", required=True, help="path to .orchestrator/lanes.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and schema")

    p = sub.add_parser("register", help="register or update a lane")
    p.add_argument("--lane", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--steps-json", help="JSON list of {name, verify_cmd} steps")

    p = sub.add_parser("attach", help="take the single-writer lock; prints the fencing token")
    p.add_argument("--lane", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--pid", type=int, help="a process whose death proves the lane's; omit when there is none")
    p.add_argument("--fingerprint", help="re-present an existing token to re-attach as its holder")

    p = sub.add_parser("release", help="give up the lock")
    p.add_argument("--lane", required=True)
    p.add_argument("--fingerprint", required=True)

    p = sub.add_parser("heartbeat", help="stamp the lane's heartbeat (fenced)")
    p.add_argument("--lane", required=True)
    p.add_argument("--fingerprint", required=True)

    p = sub.add_parser("step-done", help="record a completed step and its heartbeat (fenced)")
    p.add_argument("--lane", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--seq", type=int, required=True)

    p = sub.add_parser("set-phase", help="record the lane's current phase (fenced)")
    p.add_argument("--lane", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--phase", required=True)

    p = sub.add_parser("evict", help="break a lock, naming the fingerprint being evicted")
    p.add_argument("--lane", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("liveness", help="tri-state verdict on one lane's lock")
    p.add_argument("--lane", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="every lane, flagging stale heartbeats")
    p.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("preflight", help="refuse to spawn on an exhausted model")
    p.add_argument("--model", required=True)
    p.add_argument("--require-verified", action="store_true", help="also refuse a model with no observation on record")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("observe-model", help="record what was seen of a model's quota")
    p.add_argument("--model", required=True)
    p.add_argument("--state", required=True, choices=(STATE_AVAILABLE, STATE_EXHAUSTED, STATE_UNKNOWN))
    p.add_argument("--resets-in", type=float, help="seconds until the limit resets")
    p.add_argument("--source", default="operator")

    p = sub.add_parser("resume", help="re-verify each lane's step log and report restart points")
    p.add_argument("--lane")
    p.add_argument("--execute", action="store_true", help="repair a log the tree disproved")
    p.add_argument(
        "--take-unknown-locks",
        action="store_true",
        help="also take over lanes whose holder cannot be proven dead (never a running one); "
        "the standard recovery after a usage limit killed every agent",
    )
    p.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER)
    p.add_argument("--json", action="store_true")

    return parser


HANDLERS = {
    "init": cmd_init,
    "register": cmd_register,
    "attach": cmd_attach,
    "release": cmd_release,
    "heartbeat": cmd_heartbeat,
    "step-done": cmd_step_done,
    "set-phase": cmd_set_phase,
    "evict": cmd_evict,
    "liveness": cmd_liveness,
    "status": cmd_status,
    "preflight": cmd_preflight,
    "observe-model": cmd_observe_model,
    "resume": cmd_resume,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(Path(args.db))
    try:
        return HANDLERS[args.command](conn, args)
    except OrchestratorError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
