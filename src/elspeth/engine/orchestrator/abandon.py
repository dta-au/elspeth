"""Operator finalization of a leaderless RUNNING run (elspeth-5dd23f4df9).

ADR-030 §C.3 makes ``elspeth resume`` the recovery verb for a leaderless run:
the run-status gate admits RUNNING + expired seat as the dead-leader takeover
arm. Resume's source-lifecycle gate (ADR-038, elspeth-1f5b83cd28) then
refuses whenever a source is outside ``SOURCE_COMPLETE_LIFECYCLE_STATES``,
because resume replays only persisted row payloads. The ingest loop is
pull-then-process, so ``EXHAUSTED`` is recorded only after the LAST row's
traversal returns — and a leader that dies mid-run is, by construction,
mid-loop. The two designs therefore leave a run that is RUNNING, refused by
resume, and reachable by no other verb: its followers' finished work strands
and the run never reaches a terminal state.

The refusal is correct (unread source rows may exist; the lifecycle state
cannot say). What was missing is the honest way out. This module is the
engine behind ``elspeth abandon``: take the dead seat through the SAME
takeover CAS resume uses, finalize INTERRUPTED under that token — the fenced
arm on which ``complete_run`` runs the ADR-038 sweep, recording
``(NULL, ABANDONED)`` for every undecided token exactly when a resume would be
refused and departing the followers — then vacate the seat. It is the
mechanism the web orphan reaper already applies to its own runs
(``web/app.py::_finalize_orphaned_landscape_runs``), exposed to the operator.

Nothing here decides fate per token or bypasses a fence: the preflight is
advisory and read-only; ``acquire_run_leadership`` is the arbiter; every
durable write rides the existing leader-fenced verbs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from elspeth.contracts import RunStatus
from elspeth.contracts.checkpoint import ResumeCheck, ResumeRefusalCause
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken, mint_worker_id
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.errors import AbandonRefusedError
from elspeth.contracts.freeze import freeze_fields
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.checkpoint.recovery import check_run_status_resumable, check_source_lifecycle_resumable
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import token_outcomes_table, token_work_items_table, tokens_table


@dataclass(frozen=True, slots=True)
class LeaderlessRunPreflight:
    """Read-only facts the operator sees before ``--execute``.

    ``refusal`` is ``None`` exactly when abandon may proceed: the run exists,
    is RUNNING, and its leader seat is vacant or expired. ``resume_check`` is
    the verdict of the SAME shared gates ``elspeth resume`` consults (run
    status, source lifecycle, resume baseline), so the operator learns whether
    resume would have worked instead — a resumable run finalized here keeps
    its tokens honestly pending under INTERRUPTED (ADR-038 sweeps only when
    resume would refuse) and stays resumable.
    """

    run_id: str
    run_status: RunStatus | None
    leader_worker_id: str | None
    seat_expires_at: datetime | None
    seat_live: bool
    source_lifecycle: Mapping[str, str]
    resume_check: ResumeCheck
    work_item_counts: Mapping[str, int]
    undecided_tokens: int
    refusal: str | None
    refusal_cause: ResumeRefusalCause | None

    def __post_init__(self) -> None:
        freeze_fields(self, "source_lifecycle", "work_item_counts")
        if (self.refusal is None) != (self.refusal_cause is None):
            raise ValueError("Abandon refusal must carry both reason and cause")

    @property
    def admissible(self) -> bool:
        return self.refusal is None


@dataclass(frozen=True, slots=True)
class AbandonOutcome:
    """What ``abandon_leaderless_run`` durably did."""

    run_id: str
    run_status: RunStatus
    worker_id: str
    leader_epoch: int
    abandoned_tokens: int


def _resume_verdict(db: LandscapeDB, run_id: str) -> ResumeCheck:
    """The graph-independent slice of ``RecoveryManager.can_resume``.

    Status gate, then the shared source-lifecycle gate, then the resume
    baseline. Graph-dependent checks (topology, contract integrity) stay with
    the resume pre-flight — evaluating them here without the rebuilt graph
    could refuse falsely, and this verdict is advisory.
    """
    _run_status, status_check = check_run_status_resumable(db, run_id)
    if not status_check.can_resume:
        return status_check
    lifecycle_gate = check_source_lifecycle_resumable(db, run_id)
    if not lifecycle_gate.check.can_resume:
        return lifecycle_gate.check
    if CheckpointManager(db).get_latest_checkpoint(run_id) is None:
        return ResumeCheck(
            can_resume=False, reason="no resume baseline exists (checkpointing was disabled)", cause=ResumeRefusalCause.CHECKPOINT_MISSING
        )
    return ResumeCheck(can_resume=True)


@dataclass(frozen=True, slots=True)
class _RunWorkSnapshot:
    """One-connection read of the run's scheduler work and token decisions."""

    work_item_counts: Mapping[str, int]
    undecided_tokens: int
    abandoned_tokens: int

    def __post_init__(self) -> None:
        freeze_fields(self, "work_item_counts")


def _read_run_work(db: LandscapeDB, run_id: str) -> _RunWorkSnapshot:
    """Read-only facts about the run's work, taken on ONE connection.

    ``undecided_tokens`` counts tokens with no ``completed=1`` outcome row —
    the ADR-038 sweep's candidates; ``abandoned_tokens`` counts the
    ``(NULL, ABANDONED)`` rows that sweep has written. Read together so the
    operator sees one consistent snapshot, not three.
    """
    decided = (
        select(token_outcomes_table.c.outcome_id)
        .where(token_outcomes_table.c.run_id == run_id)
        .where(token_outcomes_table.c.token_id == tokens_table.c.token_id)
        .where(token_outcomes_table.c.completed == 1)
        .exists()
    )
    with db.engine.connect() as conn:
        status_rows = conn.execute(
            select(token_work_items_table.c.status, func.count())
            .where(token_work_items_table.c.run_id == run_id)
            .group_by(token_work_items_table.c.status)
            .order_by(token_work_items_table.c.status)
        ).fetchall()
        undecided = conn.execute(
            select(func.count()).select_from(tokens_table).where(tokens_table.c.run_id == run_id).where(~decided)
        ).scalar_one()
        abandoned = conn.execute(
            select(func.count())
            .select_from(token_outcomes_table)
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.path == TerminalPath.ABANDONED.value)
        ).scalar_one()
    return _RunWorkSnapshot(
        work_item_counts={str(row[0]): int(row[1]) for row in status_rows},
        undecided_tokens=int(undecided),
        abandoned_tokens=int(abandoned),
    )


def inspect_leaderless_run(db: LandscapeDB, run_id: str) -> LeaderlessRunPreflight:
    """Advisory, read-only preflight for ``elspeth abandon``.

    Check-then-act is acceptable here for the same reason it is in the resume
    entry guard: the takeover CAS in :func:`abandon_leaderless_run` is the
    true arbiter, and a seat revived between this read and that write is
    refused there with zero mutation.
    """
    factory = RecorderFactory(db)
    run = factory.run_lifecycle.get_run(run_id)
    if run is None:
        return LeaderlessRunPreflight(
            run_id=run_id,
            run_status=None,
            leader_worker_id=None,
            seat_expires_at=None,
            seat_live=False,
            source_lifecycle={},
            resume_check=ResumeCheck(can_resume=False, reason=f"Run {run_id} not found", cause=ResumeRefusalCause.RUN_NOT_FOUND),
            work_item_counts={},
            undecided_tokens=0,
            refusal=f"Run {run_id} not found",
            refusal_cause=ResumeRefusalCause.RUN_NOT_FOUND,
        )

    leader = RunCoordinationRepository(db.engine).live_leader(run_id=run_id)
    source_lifecycle = check_source_lifecycle_resumable(db, run_id).lifecycle_by_source
    resume_check = _resume_verdict(db, run_id)

    refusal: str | None
    refusal_cause: ResumeRefusalCause | None
    if run.status is not RunStatus.RUNNING:
        refusal_cause = ResumeRefusalCause.RUN_NOT_RUNNING
        refusal = f"run status is {run.status.value!r}, already terminal; nothing to abandon" + (
            " (it is resumable: use `elspeth resume`)" if resume_check.can_resume else ""
        )
    elif leader is not None and leader.seat_live:
        refusal_cause = ResumeRefusalCause.LEADER_LIVE
        refusal = (
            f"run is led by live leader {leader.leader_worker_id!r} "
            f"(seat expires {leader.leader_heartbeat_expires_at.isoformat()}) — "
            "use `elspeth join` to attach as a follower, or wait for the seat to lapse"
        )
    else:
        refusal = None
        refusal_cause = None

    work = _read_run_work(db, run_id)
    return LeaderlessRunPreflight(
        run_id=run_id,
        run_status=run.status,
        leader_worker_id=None if leader is None else leader.leader_worker_id,
        seat_expires_at=None if leader is None else leader.leader_heartbeat_expires_at,
        seat_live=leader is not None and leader.seat_live,
        source_lifecycle=dict(source_lifecycle),
        resume_check=resume_check,
        work_item_counts=work.work_item_counts,
        undecided_tokens=work.undecided_tokens,
        refusal=refusal,
        refusal_cause=refusal_cause,
    )


def _acquire_leaderless_run_seat(factory: RecorderFactory, *, run_id: str) -> CoordinationToken:
    """Take the dead leader's seat through the takeover CAS (epoch+1) for one abandon.

    The mutation-fencing gate admits exactly this helper for the ``abandon``
    entry point, the way it admits ``web/app.py``'s orphan finaliser for
    ``orphan-finalize``: one run-bound ``mint_worker_id``, the nominal
    liveness window, and a literal entry-point label.
    """
    return factory.run_coordination.acquire_run_leadership(
        run_id=run_id,
        worker_id=mint_worker_id(run_id),
        window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
        entry_point="abandon",
    )


def abandon_leaderless_run(db: LandscapeDB, run_id: str) -> AbandonOutcome:
    """Take the dead leader's seat and finalize the run as INTERRUPTED.

    Three durable acts, each an existing leader-fenced verb:

    1. ``acquire_run_leadership`` — the §B.4 takeover CAS (epoch+1), which
       identity-evicts the deposed leader and refuses a live seat with
       ``NonResumableRunError`` and zero mutation;
    2. ``complete_run(INTERRUPTED)`` under that token — the fenced finalize
       arm: ADR-038 abandons every undecided token when the run is
       non-resumable, fails open effect operations, departs the followers;
    3. ``release_seat`` — seat hygiene (ADR-030 §D), after the stamp.

    Raises:
        AbandonRefusedError: The preflight refused (missing, terminal, or
            live-led run). Nothing was written.
        NonResumableRunError: The seat came back to life between the
            preflight and the CAS. Nothing was written.
    """
    preflight = inspect_leaderless_run(db, run_id)
    if preflight.refusal is not None:
        assert preflight.refusal_cause is not None
        raise AbandonRefusedError(run_id, preflight.refusal, cause=preflight.refusal_cause)

    factory = RecorderFactory(db)
    coordination_token = _acquire_leaderless_run_seat(factory, run_id=run_id)
    factory.run_lifecycle.complete_run(RunStatus.INTERRUPTED, coordination_token=coordination_token)
    abandoned_tokens = _read_run_work(db, run_id).abandoned_tokens
    factory.run_coordination.release_seat(token=coordination_token)
    return AbandonOutcome(
        run_id=run_id,
        run_status=RunStatus.INTERRUPTED,
        worker_id=coordination_token.worker_id,
        leader_epoch=coordination_token.leader_epoch,
        abandoned_tokens=abandoned_tokens,
    )
