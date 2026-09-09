"""Run-coordination repository: the epoch-21 leader/follower substrate (ADR-030).

Owns the ``run_coordination`` seat row, the ``run_workers`` registry, and the
``run_coordination_events`` ledger (design
docs/architecture/design-notes/option-c-multi-worker-coordination-design-2026-06-11.md §A.2/§B.4/§C/§G).
Three shared fence constructs live here and in the schema module — one
definition, one dedicated unit test each (design §G; ADR-030 D4's three fences
restored by the ADR-048 amendment of 2026-09-07):

- :func:`verify_and_extend_leader_fence` (this module) — the leader epoch
  fence, emitted as the FIRST statement of every leader-fenced transaction
  (``fenced_leader_transaction``); the authority is a ``CoordinationToken``;
- :func:`verify_membership_fence` (this module) — the membership fence in its
  D7 verify-UPDATE form, emitted as the FIRST statement of every
  membership-fenced transaction (``fenced_member_transaction``); the authority
  is a ``WorkerMembershipToken``;
- ``active_worker_fence_clause`` (schema module) — the membership EXISTS
  predicate slice 4 compiles into the claim/enqueue verbs (the in-statement
  form of the same fence).

Every coordination state transition writes its event row in the SAME
transaction as the state change (the scheduler_events discipline). The one
exception is ``fence_refusal``: the payload transaction that tripped the
fence rolls back, so the refusal event is written on a FRESH connection
immediately after rollback — best-effort attribution, never a durability
guarantee (§A.2).

All write transactions use the slice-1 write-intent discipline
(:func:`~elspeth.core.landscape.database.begin_write` — ``BEGIN IMMEDIATE``,
WAL write lock at BEGIN, ``OperationalError("database is locked")`` after the
5000 ms ``busy_timeout`` poll).

Slice ownership of the §G verb surface (this module is slice 2):

==========================  =======================================================
verb                        consumer
==========================  =======================================================
register_run_leader_on      slice 2/3: ``begin_run`` (uniformity rule, epoch 1)
acquire_run_leadership      slice 2/3: ``resume()``'s first durable act (§B.4)
release_seat                slice 2/3: run/resume teardown + ceremony arms (LEADER-fenced)
live_leader                 implemented now; WIRED in slice 4 (entry-guard precision)
record_fence_refusal        slice 2: every fenced verb's refusal path
verify_and_extend_fence     slice 2: finalize/run-status/checkpoint/barrier/ingest
worker_heartbeat            slice 4: the dedicated heartbeat thread (§A.3) (MEMBER-fenced)
record_heartbeat_degraded   slice 4: heartbeat thread after k busy failures (§A.3)
evict_worker                slice 4: leader housekeeping sweep (§C.2 path 1) (LEADER-fenced)
depart_worker               slice 5: follower clean exit (§B.1 step 5) (MEMBER-fenced)
admit_follower              slice 5: ``elspeth join`` atomic admission (§B.1 step 2);
                            returns the follower's ``WorkerMembershipToken``
==========================  =======================================================
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from elspeth.contracts.coordination import (
    DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
    CoordinationSnapshot,
    CoordinationToken,
    LeaderInfo,
    RegisteredWorker,
    WorkerMembershipLost,
    WorkerMembershipToken,
)
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.errors import (
    AuditIntegrityError,
    JoinRefusedError,
    RunLeadershipLostError,
    RunMembershipLostError,
    WriteLockHeldError,
)
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.canonical import canonical_json
from elspeth.core.landscape.database import Tier1Engine, begin_write, verify_sqlite_tier1_pragmas
from elspeth.core.landscape.database_clock import read_landscape_decision_time
from elspeth.core.landscape.lease_deadlines import (
    DeadlineKey,
    DeadlineKind,
    forget_issued_deadline,
    has_issued_deadline,
    record_issued_deadline,
)
from elspeth.core.landscape.schema import (
    run_coordination_events_table,
    run_coordination_table,
    run_workers_table,
    runs_table,
)

__all__ = [
    "RunCoordinationRepository",
    "fenced_leader_transaction",
    "fenced_member_transaction",
    "record_coordination_event",
    "verify_and_extend_leader_fence",
    "verify_membership_fence",
]

logger = logging.getLogger(__name__)


def _bound_heartbeat_statement_waits(conn: Connection) -> None:
    """Bound heartbeat and forensic writes without limiting pipeline payloads.

    SQLite already applies a five-second busy timeout. PostgreSQL defaults
    to unbounded waits; transaction-local limits let a blocked beat report
    degradation and let its owner join the thread during shutdown.
    """
    if conn.dialect.name == "postgresql":
        # A lock timeout must precede the whole-statement limit, otherwise
        # PostgreSQL reports generic cancellation instead of lock contention.
        conn.exec_driver_sql("SET LOCAL lock_timeout = '4000ms'")
        conn.exec_driver_sql("SET LOCAL statement_timeout = '5000ms'")


@contextmanager
def fenced_heartbeat_transaction(engine: Tier1Engine, *, member_token: WorkerMembershipToken, verb: str) -> Iterator[Connection]:
    """Lock the seat before checking membership, then admit heartbeat writes.

    Heartbeats update both liveness records. Their lock order must match
    takeover and finalization; ordinary member writes need only their own
    membership fence. The caller records a refusal after this transaction
    rolls back, preserving the heartbeat's declared loss outcome.
    """
    if not isinstance(member_token, WorkerMembershipToken):
        raise TypeError("worker heartbeat requires a WorkerMembershipToken")
    with begin_write(engine) as conn:
        _bound_heartbeat_statement_waits(conn)
        locked_seat = conn.execute(
            select(run_coordination_table.c.run_id).where(run_coordination_table.c.run_id == member_token.run_id).with_for_update()
        ).one_or_none()
        verify_membership_fence(conn, member_token=member_token, verb=verb)
        if locked_seat is None:
            raise AuditIntegrityError(f"Run {member_token.run_id!r} has registered membership but no coordination seat")
        yield conn


# Run statuses the takeover CAS flips back to 'running' (§B.4). The
# dead-leader RUNNING takeover arm also clears prior finalization metadata;
# terminal-success statuses are refused by the
# immutable-success backstop below before the seat CAS runs.
_TAKEOVER_FLIPPABLE_RUN_STATUSES = (RunStatus.FAILED.value, RunStatus.INTERRUPTED.value)

# Immutable-success run statuses (§B.4 closing line: "the immutability guards
# retained beneath as the durable backstop"). Historically the resume path's
# first durable write was ``update_run_status(RUNNING)``, whose conditional
# UPDATE refused these durably; the takeover CAS subsumed that write, so the
# durable backstop moves INTO the arbiter transaction: a takeover of a
# terminally-successful run is refused with zero mutation BEFORE the seat CAS.
# Mirrors ``_IMMUTABLE_SUCCESS_RUN_STATUSES`` in run_lifecycle_repository (the
# update_run_status guard remains for every other caller of that verb).
_IMMUTABLE_SUCCESS_RUN_STATUSES = (
    RunStatus.COMPLETED.value,
    RunStatus.COMPLETED_WITH_FAILURES.value,
    RunStatus.EMPTY.value,
)
# Every terminal status: the only runs whose seat an audit-export re-drive may take (ADR-048 §4).
_EXPORT_SEAT_RUN_STATUSES = (*_TAKEOVER_FLIPPABLE_RUN_STATUSES, *_IMMUTABLE_SUCCESS_RUN_STATUSES)


def _utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime read back from SQLite (storage is UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _is_database_locked(exc: Exception) -> bool:
    # Message-based, so it is safe to call on any DB-layer exception, not just
    # OperationalError (lets the best-effort recorder split log severity without
    # an isinstance branch).
    return "database is locked" in str(exc).lower()


def record_coordination_event(
    conn: Connection,
    *,
    run_id: str,
    event_type: str,
    worker_id: str,
    leader_epoch: int | None,
    recorded_at: datetime,
    context: Mapping[str, object] | None = None,
) -> None:
    """Append one ledger row in the caller's transaction (same-txn discipline).

    ``event_id`` is ``sha256(canonical_json(identity))`` — the scheduler-events
    dedup recipe — enforced by the ``uq_run_coordination_events_event_id``
    unique index. ``seq`` (AUTOINCREMENT) is the authoritative replay order;
    ``recorded_at`` is forensic wall-clock only.
    """
    context_json = canonical_json({} if context is None else dict(context))
    conn.execute(
        insert(run_coordination_events_table).values(
            event_id=_coordination_event_id(
                run_id=run_id,
                event_type=event_type,
                worker_id=worker_id,
                leader_epoch=leader_epoch,
                recorded_at=recorded_at,
                context_json=context_json,
            ),
            run_id=run_id,
            event_type=event_type,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            recorded_at=recorded_at,
            context_json=context_json,
        )
    )


def _coordination_event_id(
    *,
    run_id: str,
    event_type: str,
    worker_id: str,
    leader_epoch: int | None,
    recorded_at: datetime,
    context_json: str,
) -> str:
    """The ledger row's ``event_id``: ``sha256(canonical_json(identity))``, one recipe for every writer."""
    identity = canonical_json(
        {
            "context_json": context_json,
            "event_type": event_type,
            "leader_epoch": leader_epoch,
            "recorded_at": recorded_at.isoformat(),
            "run_id": run_id,
            "worker_id": worker_id,
        }
    )
    return hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CoordinationEventRow:
    """One ledger row for :func:`record_coordination_events` (same identity recipe as the single-row writer).

    ``context`` is the small string-valued label set the lifecycle writers
    attach (``{"reason": ...}``, ``{"status": ...}``); it is canonicalised
    exactly like the single-row writer's context.
    """

    event_type: str
    worker_id: str
    leader_epoch: int | None
    recorded_at: datetime
    context: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        freeze_fields(self, "context")


def record_coordination_events(conn: Connection, *, run_id: str, events: Sequence[CoordinationEventRow]) -> None:
    """Append N ledger rows in ONE statement inside the caller's transaction.

    ADR-048: a DML construction executes exactly once inside its fence, never
    per iteration — a fenced owner that must record one event per follower
    builds the rows and calls this once. Identity, ``event_id`` and ordering
    are exactly :func:`record_coordination_event`'s (:func:`_coordination_event_id`).
    """
    if not events:
        return
    rows = [
        {
            "event_id": _coordination_event_id(
                run_id=run_id,
                event_type=event.event_type,
                worker_id=event.worker_id,
                leader_epoch=event.leader_epoch,
                recorded_at=event.recorded_at,
                context_json=context_json,
            ),
            "run_id": run_id,
            "event_type": event.event_type,
            "worker_id": event.worker_id,
            "leader_epoch": event.leader_epoch,
            "recorded_at": event.recorded_at,
            "context_json": context_json,
        }
        for event, context_json in ((event, canonical_json({} if event.context is None else dict(event.context))) for event in events)
    ]
    conn.execute(insert(run_coordination_events_table), rows)


class BestEffortEventOutcome(Enum):
    """Declared result of a best-effort coordination-event write.

    ``LOST_TO_DB_FAULT`` is the writer's recorded failure result: the event
    row could not be written (logged at WARNING for transient lock
    contention, ERROR otherwise). Losing the event is benign by design
    (§A.2) — the refused transaction left no durable state needing
    explanation — so callers may deliberately ignore the outcome, but the
    failure is always recorded and surfaced as this declared result, never
    silently discarded.
    """

    RECORDED = "recorded"
    LOST_TO_DB_FAULT = "lost_to_db_fault"


class SeatReleaseOutcome(Enum):
    """Declared result of :meth:`RunCoordinationRepository.release_seat`.

    ``RELEASED`` is the transition: the seat was vacated, this worker's
    membership row departed, and the ``leader_release`` event written in one
    transaction. ``FENCE_REFUSED`` is the leader fence's refusal — the seat is
    already vacant, or another epoch holds it — surfaced as a value rather
    than swallowed, because the ``fence_refusal`` ledger row the fence writes
    on a fresh connection is best-effort attribution (§A.2) and never a
    durability guarantee. Zero mutation either way: the fence is the first
    statement of the BEGIN IMMEDIATE transaction, so a refusal unwinds before
    any payload write. Teardown callers may ignore the refusal deliberately;
    it is a declared result, never a silent one.
    """

    RELEASED = "released"
    FENCE_REFUSED = "fence_refused"


def _record_best_effort_event(
    engine: Tier1Engine,
    *,
    run_id: str,
    event_type: str,
    worker_id: str,
    leader_epoch: int | None,
    recorded_at: datetime,
    context: Mapping[str, object] | None = None,
) -> BestEffortEventOutcome:
    """Write a ledger row on a FRESH connection; best-effort, never raises.

    For events whose triggering transaction rolled back (``fence_refusal``) or
    whose writer must never crash (``heartbeat_degraded``, §A.3). Best-effort
    attribution by design: losing the event is benign because the refused
    transaction left no durable state needing explanation (§A.2).

    "Never raises" is load-bearing, not a convenience. ``record_fence_refusal``
    runs inside ``fenced_leader_transaction``'s
    ``except RunLeadershipLostError: ... raise`` unwind: a raise here would
    REPLACE the recoverable leadership-lost signal with a crash-class error, and
    on the ``record_heartbeat_degraded`` path it would crash the heartbeat thread
    before it latches coordination-lost. So every DB-layer write fault is recorded
    (logged) and surfaced as the declared ``LOST_TO_DB_FAULT`` result instead of
    raising. Transient write contention ("database is locked") logs at WARNING;
    any other DB fault (FK miss on a vanished run, ``event_id`` dedup collision,
    real corruption) logs at ERROR so it stays visible and alarmable without
    crashing the leader. Non-DB faults (e.g. a ``canonical_json`` ``TypeError``)
    are not ``SQLAlchemyError`` and still surface as the programmer errors they are.
    """
    try:
        with begin_write(engine) as conn:
            _bound_heartbeat_statement_waits(conn)
            record_coordination_event(
                conn,
                run_id=run_id,
                event_type=event_type,
                worker_id=worker_id,
                leader_epoch=leader_epoch,
                recorded_at=recorded_at,
                context=context,
            )
    except SQLAlchemyError as exc:
        level = logging.WARNING if _is_database_locked(exc) else logging.ERROR
        logger.log(
            level,
            "best-effort coordination event %r for run %r (worker %r) could not be recorded: %s",
            event_type,
            run_id,
            worker_id,
            type(exc).__name__,
            exc_info=True,
        )
        return BestEffortEventOutcome.LOST_TO_DB_FAULT
    return BestEffortEventOutcome.RECORDED


def _leader_deadline_key(token: CoordinationToken) -> DeadlineKey:
    return DeadlineKey(DeadlineKind.LEADER, (token.run_id, token.worker_id, str(token.leader_epoch)))


def _worker_deadline_key(*, run_id: str, worker_id: str) -> DeadlineKey:
    return DeadlineKey(DeadlineKind.WORKER, (run_id, worker_id))


def _renew_leader_deadline_on(conn: Connection, *, token: CoordinationToken, window_seconds: float, verb: str) -> None:
    """Renew the exact seat after its authority lock has already been acquired."""
    database_now = read_landscape_decision_time(conn)
    expires = database_now + timedelta(seconds=window_seconds)
    renewed = conn.execute(
        update(run_coordination_table)
        .where(
            run_coordination_table.c.run_id == token.run_id,
            run_coordination_table.c.leader_worker_id == token.worker_id,
            run_coordination_table.c.leader_epoch == token.leader_epoch,
        )
        .values(leader_heartbeat_expires_at=expires, updated_at=database_now)
    )
    if renewed.rowcount != 1:
        raise RunLeadershipLostError(run_id=token.run_id, worker_id=token.worker_id, leader_epoch=token.leader_epoch, verb=verb)
    record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)


def verify_and_extend_leader_fence(
    conn: Connection,
    *,
    token: CoordinationToken,
    window_seconds: float,
    verb: str,
) -> None:
    """The leader epoch fence (§C.4): verify-and-extend UPDATE CAS.

    MUST be the first statement of the caller's ``BEGIN IMMEDIATE``
    transaction: under IMMEDIATE the verify and the payload are one atomic
    unit, and rowcount-0 raising here unwinds the whole transaction before
    any payload write. Every fenced verb thereby doubles as the seat
    heartbeat (the predicate is identity+epoch only — NEVER expiry: an idle
    N=1 leader whose seat lapsed mid-run must still pass its own fence).

    The first CAS preserves identity and takes the seat lock. Only after
    that statement finishes does a separate Landscape clock query sample
    the renewal instant (ADR-047). Raw Connection callers retain an explicit
    renewal obligation; the outer transaction guard refuses an aged lease,
    and the leader context renews again after a successful body.

    On rowcount 0 raises :class:`RunLeadershipLostError`. The caller (or
    :func:`fenced_leader_transaction`) records the ``fence_refusal`` event on
    a fresh connection AFTER its rollback completes — writing it here on a
    second connection would deadlock against the caller's own write lock.
    """
    result = conn.execute(
        update(run_coordination_table)
        .where(
            run_coordination_table.c.run_id == token.run_id,
            run_coordination_table.c.leader_worker_id == token.worker_id,
            run_coordination_table.c.leader_epoch == token.leader_epoch,
        )
        .values(leader_epoch=run_coordination_table.c.leader_epoch)
    )
    if result.rowcount != 1:
        raise RunLeadershipLostError(
            run_id=token.run_id,
            worker_id=token.worker_id,
            leader_epoch=token.leader_epoch,
            verb=verb,
        )
    _renew_leader_deadline_on(conn, token=token, window_seconds=window_seconds, verb=verb)


@contextmanager
def fenced_leader_transaction(
    engine: Tier1Engine,
    *,
    token: CoordinationToken,
    window_seconds: float,
    verb: str,
) -> Iterator[Connection]:
    """One leader-fenced ``BEGIN IMMEDIATE`` transaction, refusal-evented.

    Composes :func:`~elspeth.core.landscape.database.begin_write` with
    :func:`verify_and_extend_leader_fence` as the first statement, and — on a
    fence miss — records the ``fence_refusal`` event on a fresh connection
    AFTER the payload transaction has rolled back, then re-raises
    :class:`RunLeadershipLostError`. Fenced verbs in other repositories
    (finalize / run-status / checkpoint / complete_barrier / ingest / repair
    sweep) wrap their existing transaction bodies in this.
    """
    if not isinstance(token, CoordinationToken):
        raise TypeError("leader fencing requires a CoordinationToken")
    try:
        with begin_write(engine) as conn:
            verify_and_extend_leader_fence(conn, token=token, window_seconds=window_seconds, verb=verb)
            yield conn
            if has_issued_deadline(conn, key=_leader_deadline_key(token)):
                _renew_leader_deadline_on(conn, token=token, window_seconds=window_seconds, verb=verb)
    except RunLeadershipLostError:
        # The begin_write context has exited (rolled back) by the time we get
        # here, so the fresh-connection write cannot deadlock on our own lock.
        # The refusal's timestamp is forensic (when this process saw the miss),
        # never an authority deadline: the process clock is the honest source.
        _record_best_effort_event(
            engine,
            run_id=token.run_id,
            event_type="fence_refusal",
            worker_id=token.worker_id,
            leader_epoch=token.leader_epoch,
            recorded_at=datetime.now(UTC),
            context={"verb": verb},
        )
        raise


def verify_membership_fence(
    conn: Connection,
    *,
    member_token: WorkerMembershipToken,
    verb: str,
) -> None:
    """The membership fence (ADR-030 D4, second fence) in its D7 verify-UPDATE form.

    MUST be the first statement of the caller's ``BEGIN IMMEDIATE``
    transaction: the conditional UPDATE on ``run_workers`` matching
    ``(run_id, worker_id, status='active')`` takes the row lock that an
    EXISTS subquery's snapshot would not hold under PostgreSQL READ COMMITTED
    (ADR-030 D7), and rowcount 0 raising here unwinds the whole transaction
    before any payload write. Single-use identity doctrine: ``departed`` and
    ``evicted`` rows never return to ``active``, so a refusal is permanent
    for this identity.

    The verify-UPDATE writes the row's own status back (``status='active'``
    where ``status='active'``): the fence proves and locks membership, it does
    not extend liveness. Run-level liveness is the heartbeat thread's job
    (``worker_heartbeat``), so a member's ``heartbeat_expires_at`` moves only
    when that verb says so — a departing or claiming member must never look
    fresher than its last beat.

    On rowcount 0, reads registration in this transaction: an absent row is
    :class:`AuditIntegrityError`; an inactive row raises
    :class:`RunMembershipLostError`. No payload write occurs in either case. The caller (or
    :func:`fenced_member_transaction`) records the ``fence_refusal`` event on
    a fresh connection AFTER its rollback completes.
    """
    result = conn.execute(
        update(run_workers_table)
        .where(
            run_workers_table.c.run_id == member_token.run_id,
            run_workers_table.c.worker_id == member_token.worker_id,
            run_workers_table.c.status == "active",
        )
        .values(status="active")
    )
    if result.rowcount != 1:
        registered = conn.execute(
            select(run_workers_table.c.worker_id).where(
                run_workers_table.c.run_id == member_token.run_id,
                run_workers_table.c.worker_id == member_token.worker_id,
            )
        ).scalar_one_or_none()
        if registered is None:
            raise AuditIntegrityError(
                f"{verb} for unregistered worker_id={member_token.worker_id!r} in run {member_token.run_id!r}; "
                "membership authority requires a durable registration."
            )
        raise RunMembershipLostError(
            run_id=member_token.run_id,
            worker_id=member_token.worker_id,
            verb=verb,
        )


@contextmanager
def fenced_member_transaction(
    engine: Tier1Engine,
    *,
    member_token: WorkerMembershipToken,
    verb: str,
) -> Iterator[Connection]:
    """One membership-fenced ``BEGIN IMMEDIATE`` transaction, refusal-evented.

    The member-scoped sibling of :func:`fenced_leader_transaction`: composes
    :func:`~elspeth.core.landscape.database.begin_write` with
    :func:`verify_membership_fence` as the first statement, and — on a fence
    miss — records the ``fence_refusal`` event on a fresh connection AFTER the
    payload transaction has rolled back, then re-raises
    :class:`RunMembershipLostError`. The refusal row carries
    ``leader_epoch=NULL`` (a member holds no epoch) and names the fence in its
    context so the ledger distinguishes it from a leader-fence refusal.

    Member-fenced verbs are the follower's own liveness and departure writes
    and, in wave 2, the claim/enqueue verbs; item-scoped writes additionally
    keep their item-lease CAS (``expected_lease_owner``) as the payload's own
    WHERE — D4's third fence.
    """
    if not isinstance(member_token, WorkerMembershipToken):
        raise TypeError("membership fencing requires a WorkerMembershipToken")
    try:
        with begin_write(engine) as conn:
            verify_membership_fence(conn, member_token=member_token, verb=verb)
            yield conn
    except RunMembershipLostError:
        # The begin_write context has exited (rolled back) by the time we get
        # here, so the fresh-connection write cannot deadlock on our own lock.
        _record_best_effort_event(
            engine,
            run_id=member_token.run_id,
            event_type="fence_refusal",
            worker_id=member_token.worker_id,
            leader_epoch=None,
            recorded_at=datetime.now(UTC),
            context={"verb": verb, "fence": "membership"},
        )
        raise


class RunCoordinationRepository:
    """Persistence boundary for the run-coordination substrate (ADR-030)."""

    def __init__(self, engine: Tier1Engine) -> None:
        # Runtime SQLite PRAGMA probe — defence in depth against a caller that
        # slips a bare SQLite engine past the type checker. Non-SQLite Tier-1
        # engines skip this SQLite-only syntax.
        verify_sqlite_tier1_pragmas(engine, owner="RunCoordinationRepository")
        self._engine = engine

    # ── seat lifecycle ───────────────────────────────────────────────────

    @staticmethod
    def _finalize_leader_registration_on(conn: Connection, *, token: CoordinationToken, window_seconds: float) -> None:
        """Finalize a newly minted seat/member pair while the owner holds both rows.

        Registration events describe initial admission. This final renewal
        extends that admitted pair together after the caller's composition;
        it does not change registration identity or its historical stamps.
        """
        database_now = read_landscape_decision_time(conn)
        expires = database_now + timedelta(seconds=window_seconds)
        seat = conn.execute(
            update(run_coordination_table)
            .where(
                run_coordination_table.c.run_id == token.run_id,
                run_coordination_table.c.leader_worker_id == token.worker_id,
                run_coordination_table.c.leader_epoch == token.leader_epoch,
            )
            .values(leader_heartbeat_expires_at=expires, updated_at=database_now)
        )
        if seat.rowcount != 1:
            raise RunLeadershipLostError(
                run_id=token.run_id, worker_id=token.worker_id, leader_epoch=token.leader_epoch, verb="finalize_leader_registration"
            )
        member = conn.execute(
            update(run_workers_table)
            .where(
                run_workers_table.c.run_id == token.run_id,
                run_workers_table.c.worker_id == token.worker_id,
                run_workers_table.c.role == "leader",
                run_workers_table.c.status == "active",
            )
            .values(heartbeat_expires_at=expires)
        )
        if member.rowcount != 1:
            raise AuditIntegrityError(f"Newly registered leader {token.worker_id!r} has no active same-run leader membership")
        record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)
        record_issued_deadline(
            conn,
            key=_worker_deadline_key(run_id=token.run_id, worker_id=token.worker_id),
            expires_at=expires,
            window_seconds=window_seconds,
        )

    def register_run_leader_on(
        self,
        conn: Connection,
        *,
        run_id: str,
        worker_id: str,
        window_seconds: float,
        entry_point: str = "run",
    ) -> CoordinationToken:
        """Connection-accepting seat mint: composes into the caller's transaction.

        INSERTs the ``run_coordination`` seat row (epoch 1), the leader's
        ``run_workers`` row (with the §A.1 pid/hostname/entry_point
        forensics), and the ``leader_acquire`` + ``worker_register`` events —
        all on ``conn``. The ``runs`` row must already exist in this
        transaction (FK). The seat's deadline and every stamp come from the
        initial Landscape admission sample on ``conn`` (ADR-047). The outer
        owner finalizes the pair after its body; returning this token has not
        committed either the admission or its deadline.
        """
        database_now = read_landscape_decision_time(conn)
        expires = database_now + timedelta(seconds=window_seconds)
        conn.execute(
            insert(run_coordination_table).values(
                run_id=run_id,
                leader_worker_id=worker_id,
                leader_epoch=1,
                leader_heartbeat_expires_at=expires,
                updated_at=database_now,
            )
        )
        token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=1)
        record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)
        self._insert_worker_row(
            conn,
            run_id=run_id,
            worker_id=worker_id,
            role="leader",
            window_seconds=window_seconds,
            entry_point=entry_point,
            database_now=database_now,
        )
        record_coordination_event(
            conn,
            run_id=run_id,
            event_type="worker_register",
            worker_id=worker_id,
            leader_epoch=1,
            recorded_at=database_now,
            context={"role": "leader", "entry_point": entry_point},
        )
        record_coordination_event(
            conn,
            run_id=run_id,
            event_type="leader_acquire",
            worker_id=worker_id,
            leader_epoch=1,
            recorded_at=database_now,
            context={"entry_point": entry_point},
        )
        return token

    def acquire_run_leadership(
        self,
        *,
        run_id: str,
        worker_id: str,
        window_seconds: float,
        entry_point: str = "resume",
    ) -> CoordinationToken:
        """The §B.4 seat-takeover CAS — resume()'s first durable act (TOCTOU closure).

        One ``BEGIN IMMEDIATE`` transaction:

        1. read the incumbent seat (``:prior``, may be vacant) and refuse a
           terminally-successful run with ``AuditIntegrityError`` (the
           immutable-success durable backstop — formerly update_run_status's
           conditional UPDATE, subsumed by this verb);
        2. seat CAS — bump ``leader_epoch``, claim the seat — admissible only
           when the seat is vacant or expired; rowcount 0 ⇒ ROLLBACK with zero
           mutation and ``NonResumableRunError("run leadership is held by
           …")`` (the pinned refusal-before-mutation discipline);
        3. the run-status flip ``failed/interrupted → running`` (subsumes the
           old ``update_run_status(RUNNING)`` first-durable-write), clearing
           prior finalization metadata even on dead-leader RUNNING takeover;
        4. identity-eviction of the deposed leader, unconditional — by
           identity, no heartbeat predicate (the expired seat IS the proof of
           lost custody); NO bulk follower eviction (§C.2 housekeeping is
           slice 4);
        5. new leader ``run_workers`` row + ``worker_register`` /
           ``leader_acquire`` (+ ``worker_evict``) events.

        BUSY-vs-CAS-loss discrimination (§B.4): a busy timeout at BEGIN (or
        anywhere inside) is NOT "leadership held" — it means a live-or-frozen
        process holds the WAL write lock; raised as the operator-actionable
        :class:`WriteLockHeldError` carrying structured registered-worker
        forensics (pids read on a plain read connection — WAL readers don't
        block on the writer).
        """
        try:
            with begin_write(self._engine) as conn:
                token = self._acquire_run_leadership_on(
                    conn,
                    run_id=run_id,
                    worker_id=worker_id,
                    window_seconds=window_seconds,
                    entry_point=entry_point,
                )
                self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)
            return token
        except OperationalError as exc:
            if not _is_database_locked(exc):
                raise
            raise WriteLockHeldError(run_id=run_id, workers=self._read_registered_workers(run_id)) from exc

    def _acquire_run_leadership_on(
        self,
        conn: Connection,
        *,
        run_id: str,
        worker_id: str,
        window_seconds: float,
        entry_point: str,
    ) -> CoordinationToken:
        # Acquire the seat before sampling its expiry decision. A transaction
        # start timestamp would retain the entire lock wait (ADR-047).
        seat = conn.execute(
            select(
                run_coordination_table.c.leader_worker_id,
                run_coordination_table.c.leader_epoch,
                run_coordination_table.c.leader_heartbeat_expires_at,
            )
            .where(run_coordination_table.c.run_id == run_id)
            .with_for_update()
        ).one_or_none()
        if seat is None:
            # Epoch-21 invariant: begin_run mints the seat in the same
            # transaction as the runs row, so a run without a seat row is
            # audit corruption, not a coordination outcome.
            raise AuditIntegrityError(
                f"Run {run_id!r} has no run_coordination seat row; at schema epoch 21 "
                "begin_run creates it atomically with the run. The audit DB is corrupt "
                "or was written by incompatible code."
            )
        database_now = read_landscape_decision_time(conn)
        prior_worker: str | None = seat.leader_worker_id
        expires = database_now + timedelta(seconds=window_seconds)

        # Immutable-success durable backstop (§B.4 closing line). The takeover
        # CAS subsumed the resume path's update_run_status(RUNNING) first
        # durable write, whose conditional UPDATE was the pinned
        # loser-after-winner refusal (test_concurrent_resume.py): a resume
        # racing a winner that already COMPLETED must be refused durably, in
        # the arbiter transaction, with zero mutation — a vacant seat on a
        # terminally-successful run is NOT an admissible takeover target.
        run_status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one_or_none()
        if run_status in _IMMUTABLE_SUCCESS_RUN_STATUSES:
            status_enum = RunStatus(run_status)
            raise AuditIntegrityError(
                f"Cannot acquire run leadership: cannot transition run {run_id} from "
                f"{status_enum.name} ({status_enum.value!r}) to 'running'. "
                f"Successful terminal runs are immutable. "
                f"FAILED/INTERRUPTED runs can be resumed via seat takeover."
            )

        cas = conn.execute(
            update(run_coordination_table)
            .where(
                run_coordination_table.c.run_id == run_id,
                (run_coordination_table.c.leader_worker_id.is_(None))
                | (run_coordination_table.c.leader_heartbeat_expires_at < database_now),
            )
            .values(
                leader_worker_id=worker_id,
                leader_epoch=run_coordination_table.c.leader_epoch + 1,
                leader_heartbeat_expires_at=expires,
                updated_at=database_now,
            )
        )
        if cas.rowcount != 1:
            # Clean CAS loss to a live seat: raising inside the transaction
            # rolls everything back — the loser is side-effect-free (the
            # pinned refusal-before-mutation discipline, crash walk T3).
            #
            # Local import: recovery.py imports the landscape factory at module
            # scope, and the factory imports this module — a module-level
            # import here would close that cycle.
            from elspeth.core.checkpoint.recovery import NonResumableRunError

            held_expiry = seat.leader_heartbeat_expires_at
            expiry_text = "unknown" if held_expiry is None else _utc(held_expiry).isoformat()
            raise NonResumableRunError(
                run_id,
                f"run leadership is held by {prior_worker!r} (seat expires {expiry_text})",
            )
        new_epoch = int(seat.leader_epoch) + 1
        token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=new_epoch)
        record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)

        # The winner's run-status normalization rides the same transaction
        # (§B.4). An inherited grade belongs to the prior attempt, including
        # when that attempt left a stale grade on RUNNING. Terminal SUCCESS
        # statuses were refused above before the seat CAS ran.
        conn.execute(
            update(runs_table)
            .where(
                runs_table.c.run_id == run_id,
                runs_table.c.status.in_((*_TAKEOVER_FLIPPABLE_RUN_STATUSES, RunStatus.RUNNING.value)),
            )
            .values(status=RunStatus.RUNNING.value, completed_at=None, reproducibility_grade=None)
        )

        if prior_worker is not None and prior_worker != worker_id:
            evicted = conn.execute(
                update(run_workers_table)
                .where(
                    run_workers_table.c.worker_id == prior_worker,
                    run_workers_table.c.status == "active",
                )
                .values(status="evicted", evicted_at=database_now, evicted_by_worker_id=worker_id)
            )
            if evicted.rowcount == 1:
                record_coordination_event(
                    conn,
                    run_id=run_id,
                    event_type="worker_evict",
                    worker_id=prior_worker,
                    leader_epoch=new_epoch,
                    recorded_at=database_now,
                    context={"evicted_by_worker_id": worker_id, "reason": "deposed_leader_takeover"},
                )

        self._insert_worker_row(
            conn,
            run_id=run_id,
            worker_id=worker_id,
            role="leader",
            window_seconds=window_seconds,
            entry_point=entry_point,
            database_now=database_now,
        )
        record_coordination_event(
            conn,
            run_id=run_id,
            event_type="worker_register",
            worker_id=worker_id,
            leader_epoch=new_epoch,
            recorded_at=database_now,
            context={"role": "leader", "entry_point": entry_point},
        )
        record_coordination_event(
            conn,
            run_id=run_id,
            event_type="leader_acquire",
            worker_id=worker_id,
            leader_epoch=new_epoch,
            recorded_at=database_now,
            context={"entry_point": entry_point, "deposed_leader_worker_id": prior_worker},
        )
        return token

    def acquire_export_leadership(
        self,
        *,
        run_id: str,
        worker_id: str,
        window_seconds: float,
    ) -> CoordinationToken:
        """Take the seat of a FINALIZED run to re-drive its audit export (ADR-048 §4).

        The resume takeover (:meth:`acquire_run_leadership`) is the wrong
        instrument for an export: it refuses a terminally-successful run and
        flips FAILED/INTERRUPTED back to RUNNING. This verb is the other arm:
        admissible ONLY on a terminal run whose seat is vacant or expired,
        NEVER touches ``runs.status``, and otherwise mints exactly what the
        takeover mints — epoch+1, the worker's ``run_workers`` row, the
        ``worker_register`` / ``leader_acquire`` events (and the deposed
        worker's eviction when a dead leader still sits there). The caller
        vacates the seat with :meth:`release_seat` when the export is done.
        """
        try:
            with begin_write(self._engine) as conn:
                token = self._acquire_export_leadership_on(
                    conn,
                    run_id=run_id,
                    worker_id=worker_id,
                    window_seconds=window_seconds,
                )
                self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)
            return token
        except OperationalError as exc:
            if not _is_database_locked(exc):
                raise
            raise WriteLockHeldError(run_id=run_id, workers=self._read_registered_workers(run_id)) from exc

    def _acquire_export_leadership_on(
        self,
        conn: Connection,
        *,
        run_id: str,
        worker_id: str,
        window_seconds: float,
    ) -> CoordinationToken:
        seat = conn.execute(
            select(
                run_coordination_table.c.leader_worker_id,
                run_coordination_table.c.leader_epoch,
                run_coordination_table.c.leader_heartbeat_expires_at,
            )
            .where(run_coordination_table.c.run_id == run_id)
            .with_for_update()
        ).one_or_none()
        if seat is None:
            raise AuditIntegrityError(
                f"Run {run_id!r} has no run_coordination seat row; at schema epoch 21 "
                "begin_run creates it atomically with the run. The audit DB is corrupt "
                "or was written by incompatible code."
            )
        database_now = read_landscape_decision_time(conn)
        run_status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one_or_none()
        if run_status == RunStatus.RUNNING.value:
            from elspeth.core.checkpoint.recovery import NonResumableRunError

            raise NonResumableRunError(run_id, "run is not terminal; its running leader owns finalization")
        if run_status not in _EXPORT_SEAT_RUN_STATUSES:
            raise AuditIntegrityError(
                f"Cannot acquire export leadership: run {run_id} is {run_status!r}, not terminal. "
                "An audit export is re-driven only for a finalized run; a RUNNING run is its leader's."
            )
        prior_worker: str | None = seat.leader_worker_id
        expires = database_now + timedelta(seconds=window_seconds)
        cas = conn.execute(
            update(run_coordination_table)
            .where(
                run_coordination_table.c.run_id == run_id,
                (run_coordination_table.c.leader_worker_id.is_(None))
                | (run_coordination_table.c.leader_heartbeat_expires_at < database_now),
            )
            .values(
                leader_worker_id=worker_id,
                leader_epoch=run_coordination_table.c.leader_epoch + 1,
                leader_heartbeat_expires_at=expires,
                updated_at=database_now,
            )
        )
        if cas.rowcount != 1:
            from elspeth.core.checkpoint.recovery import NonResumableRunError

            held_expiry = seat.leader_heartbeat_expires_at
            expiry_text = "unknown" if held_expiry is None else _utc(held_expiry).isoformat()
            raise NonResumableRunError(
                run_id,
                f"run leadership is held by {prior_worker!r} (seat expires {expiry_text})",
            )
        new_epoch = int(seat.leader_epoch) + 1
        token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=new_epoch)
        record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)
        if prior_worker is not None and prior_worker != worker_id:
            evicted = conn.execute(
                update(run_workers_table)
                .where(
                    run_workers_table.c.worker_id == prior_worker,
                    run_workers_table.c.status == "active",
                )
                .values(status="evicted", evicted_at=database_now, evicted_by_worker_id=worker_id)
            )
            if evicted.rowcount == 1:
                record_coordination_event(
                    conn,
                    run_id=run_id,
                    event_type="worker_evict",
                    worker_id=prior_worker,
                    leader_epoch=new_epoch,
                    recorded_at=database_now,
                    context={"evicted_by_worker_id": worker_id, "reason": "deposed_leader_export_takeover"},
                )
        self._insert_worker_row(
            conn,
            run_id=run_id,
            worker_id=worker_id,
            role="leader",
            window_seconds=window_seconds,
            entry_point="export",
            database_now=database_now,
        )
        record_coordination_event(
            conn,
            run_id=run_id,
            event_type="worker_register",
            worker_id=worker_id,
            leader_epoch=new_epoch,
            recorded_at=database_now,
            context={"role": "leader", "entry_point": "export"},
        )
        record_coordination_event(
            conn,
            run_id=run_id,
            event_type="leader_acquire",
            worker_id=worker_id,
            leader_epoch=new_epoch,
            recorded_at=database_now,
            context={"entry_point": "export", "deposed_leader_worker_id": prior_worker},
        )
        return token

    def release_seat(self, *, token: CoordinationToken) -> SeatReleaseOutcome:
        """Graceful leader shutdown: vacate the seat + depart own row, leader-fenced. Idempotent.

        The seat is a run-scoped row, so vacating it is a LEADER write
        (ADR-030 D4): the verify-and-extend epoch fence is the first statement
        and proves the caller still holds this epoch before the seat is
        cleared. A fence miss (already released, or deposed) is this verb's
        declared no-op, returned as :attr:`SeatReleaseOutcome.FENCE_REFUSED`
        rather than raised: release runs from ``finally`` arms where a second
        leadership-lost signal would mask the first, and from
        ``abandon_leaderless_run`` on its success path. Zero mutation either
        way — the fence is the transaction's first statement — and the
        ``fence_refusal`` ledger row is best-effort attribution, so the
        returned outcome, not that row, is the caller's guaranteed signal.

        A seat that passes its own epoch fence but has no matching active
        run-scoped membership is a durable-image contradiction, not a race:
        nothing departs or evicts a leader's own row while it holds the seat
        (``evict_worker`` refuses the incumbent, and finalize's §D hygiene
        departs followers only). It therefore raises
        :class:`AuditIntegrityError`, the same fail-closed backstop the vacate
        CAS above and ``depart_worker`` carry, and the whole transaction rolls
        back (fence extension included) with zero mutation.

        Raises:
            AuditIntegrityError: The seat passed its epoch fence but the
                vacate CAS or the run-scoped membership departure matched
                other than exactly one row.
        """
        try:
            with fenced_leader_transaction(
                self._engine, token=token, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, verb="release_seat"
            ) as conn:
                database_now = read_landscape_decision_time(conn)
                released = conn.execute(
                    update(run_coordination_table)
                    .where(
                        run_coordination_table.c.run_id == token.run_id,
                        run_coordination_table.c.leader_worker_id == token.worker_id,
                        run_coordination_table.c.leader_epoch == token.leader_epoch,
                    )
                    .values(leader_worker_id=None, leader_heartbeat_expires_at=None, updated_at=database_now)
                )
                if released.rowcount != 1:
                    # The fence just matched this exact seat inside the same
                    # IMMEDIATE transaction, so this arm is unreachable by
                    # construction; it stays as a fail-closed integrity backstop.
                    raise AuditIntegrityError(
                        f"release_seat: the seat for run {token.run_id!r} passed its epoch fence but the vacate UPDATE matched "
                        f"{released.rowcount} rows; the run_coordination row changed inside one IMMEDIATE transaction."
                    )
                forget_issued_deadline(conn, key=_leader_deadline_key(token))
                departed = conn.execute(
                    update(run_workers_table)
                    .where(
                        run_workers_table.c.run_id == token.run_id,
                        run_workers_table.c.worker_id == token.worker_id,
                        run_workers_table.c.status == "active",
                    )
                    .values(status="departed", departed_at=database_now)
                )
                if departed.rowcount != 1:
                    # An authoritative seat without its matching active member
                    # is a corrupt durable image, not a benign absence: no verb
                    # departs or evicts a leader's own row while it holds the
                    # seat. Fail closed, exactly as the vacate CAS above does.
                    raise AuditIntegrityError(
                        f"release_seat: the seat for run {token.run_id!r} passed its epoch fence but worker "
                        f"{token.worker_id!r} has no active run_workers row in that run; the departure UPDATE matched "
                        f"{departed.rowcount} rows."
                    )
                record_coordination_event(
                    conn,
                    run_id=token.run_id,
                    event_type="leader_release",
                    worker_id=token.worker_id,
                    leader_epoch=token.leader_epoch,
                    recorded_at=database_now,
                    context={"worker_row_departed": True},
                )
        except RunLeadershipLostError:
            # Already released or deposed: the declared idempotent no-op,
            # surfaced as an explicit result. The fence's own refusal row is
            # best-effort, so this return value is the caller's only
            # guaranteed signal that no release happened.
            return SeatReleaseOutcome.FENCE_REFUSED
        return SeatReleaseOutcome.RELEASED

    def live_leader(self, *, run_id: str) -> LeaderInfo | None:
        """Read-only seat read (§B.3). Implemented now; WIRED into the entry guard in slice 4.

        Returns None when the run has no seat row or the seat is vacant;
        otherwise the incumbent with ``seat_live`` evaluated against the
        Landscape database clock read on the same connection (ADR-047).
        Check-then-act at the caller is acceptable because the leadership CAS
        is the arbiter.
        """
        with self._engine.connect() as conn:
            database_now = read_landscape_decision_time(conn)
            # Liveness is decided in SQL against the bound database time — the
            # same comparison the takeover CAS makes. This remains advisory:
            # the arbiter samples again after it acquires the seat lock.
            seat = conn.execute(
                select(
                    run_coordination_table.c.leader_worker_id,
                    run_coordination_table.c.leader_epoch,
                    run_coordination_table.c.leader_heartbeat_expires_at,
                    (run_coordination_table.c.leader_heartbeat_expires_at >= database_now).label("seat_live"),
                ).where(run_coordination_table.c.run_id == run_id)
            ).one_or_none()
        if seat is None or seat.leader_worker_id is None:
            return None
        return LeaderInfo(
            run_id=run_id,
            leader_worker_id=seat.leader_worker_id,
            leader_epoch=int(seat.leader_epoch),
            leader_heartbeat_expires_at=_utc(seat.leader_heartbeat_expires_at),
            seat_live=bool(seat.seat_live),
        )

    # ── eventing ─────────────────────────────────────────────────────────

    def record_fence_refusal(
        self,
        *,
        run_id: str,
        worker_id: str,
        leader_epoch: int,
        verb: str,
        now: datetime,
    ) -> None:
        """Record a ``fence_refusal`` event on a FRESH connection; best-effort, never raises.

        Called AFTER the refused transaction's rollback completes (§A.2);
        :func:`fenced_leader_transaction` does this automatically.
        """
        _record_best_effort_event(
            self._engine,
            run_id=run_id,
            event_type="fence_refusal",
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            recorded_at=now,
            context={"verb": verb},
        )

    def record_heartbeat_degraded(
        self,
        *,
        member_token: WorkerMembershipToken,
        failures: int,
        now: datetime,
    ) -> None:
        """Record a ``heartbeat_degraded`` event; fresh connection, best-effort (§A.3).

        Consumed by the slice-4 heartbeat thread after ``k`` consecutive busy
        failures, so a later eviction is diagnosable post-hoc as "could not
        reach the DB" rather than "process died". Database faults are best-effort;
        invalid carrier types are programmer errors. This forensic event retains
        its originating identity even after membership is lost, so it deliberately
        does not require a live membership fence (ADR-048).
        """
        if not isinstance(member_token, WorkerMembershipToken):
            raise TypeError("heartbeat degradation requires a WorkerMembershipToken")
        _record_best_effort_event(
            self._engine,
            run_id=member_token.run_id,
            event_type="heartbeat_degraded",
            worker_id=member_token.worker_id,
            leader_epoch=None,
            recorded_at=now,
            context={"consecutive_busy_failures": failures},
        )

    # ── registry membership (slice-4/5 consumers) ────────────────────────

    def worker_heartbeat(
        self, *, member_token: WorkerMembershipToken, window_seconds: float
    ) -> CoordinationSnapshot | WorkerMembershipLost:
        """Member-fenced worker-row beat + seat snapshot, one transaction (§A.3).

        SLICE-4 CONSUMER: the dedicated heartbeat thread. Lock the seat before
        fencing membership, matching takeover and release order. No liveness
        write precedes the membership fence; its refusal — this worker is no
        longer ``active`` (departed at finalize, or evicted) — is this verb's
        DECLARED outcome, ``WorkerMembershipLost``, requiring no further read:
        the thread latches its coordination-lost flag on THAT, never on a DB error, and the
        ``fence_refusal`` row the fence recorded is the durable evidence of
        the zombie beat. A leader beats BOTH rows in one transaction (identity
        CAS on the seat — no epoch parameter here; the leader-fenced verbs are
        the epoch arbiters), so the two liveness clocks can never skew in the
        dangerous worker-fresher-than-seat direction.

        A token for a row that does not exist at all is not a coordination
        outcome: registration precedes the heartbeat thread by construction,
        so a vanished row is audit corruption (``AuditIntegrityError``).
        """
        if not isinstance(member_token, WorkerMembershipToken):
            raise TypeError("worker heartbeat requires a WorkerMembershipToken")
        try:
            with fenced_heartbeat_transaction(self._engine, member_token=member_token, verb="worker_heartbeat") as conn:
                role = conn.execute(
                    select(run_workers_table.c.role).where(
                        run_workers_table.c.run_id == member_token.run_id,
                        run_workers_table.c.worker_id == member_token.worker_id,
                    )
                ).scalar_one()
                database_now = read_landscape_decision_time(conn)
                expires = database_now + timedelta(seconds=window_seconds)
                conn.execute(
                    update(run_workers_table)
                    .where(
                        run_workers_table.c.run_id == member_token.run_id,
                        run_workers_table.c.worker_id == member_token.worker_id,
                    )
                    .values(heartbeat_expires_at=expires)
                )
                record_issued_deadline(
                    conn,
                    key=_worker_deadline_key(run_id=member_token.run_id, worker_id=member_token.worker_id),
                    expires_at=expires,
                    window_seconds=window_seconds,
                )
                leader_renewed = False
                if role == "leader":
                    renewed = conn.execute(
                        update(run_coordination_table)
                        .where(
                            run_coordination_table.c.run_id == member_token.run_id,
                            run_coordination_table.c.leader_worker_id == member_token.worker_id,
                        )
                        .values(leader_heartbeat_expires_at=expires, updated_at=database_now)
                    )
                    leader_renewed = renewed.rowcount == 1
                # Snapshot liveness uses the same final heartbeat decision
                # sample as both deadlines; vacant/NULL seats read dead.
                seat = conn.execute(
                    select(
                        run_coordination_table.c.leader_worker_id,
                        run_coordination_table.c.leader_epoch,
                        (run_coordination_table.c.leader_heartbeat_expires_at >= database_now).label("seat_live"),
                    ).where(run_coordination_table.c.run_id == member_token.run_id)
                ).one_or_none()
                if seat is None:
                    raise AuditIntegrityError(
                        f"Run {member_token.run_id!r} has no run_coordination seat row; "
                        "worker registration requires a seat created atomically by begin_run."
                    )
                if leader_renewed:
                    record_issued_deadline(
                        conn,
                        key=DeadlineKey(DeadlineKind.LEADER, (member_token.run_id, member_token.worker_id, str(seat.leader_epoch))),
                        expires_at=expires,
                        window_seconds=window_seconds,
                    )
        except RunMembershipLostError:
            _record_best_effort_event(
                self._engine,
                run_id=member_token.run_id,
                event_type="fence_refusal",
                worker_id=member_token.worker_id,
                leader_epoch=None,
                recorded_at=datetime.now(UTC),
                context={"verb": "worker_heartbeat", "fence": "membership"},
            )
            return WorkerMembershipLost(member_token=member_token)
        return CoordinationSnapshot(
            leader_worker_id=seat.leader_worker_id,
            leader_epoch=int(seat.leader_epoch),
            seat_live=seat.leader_worker_id is not None and bool(seat.seat_live),
            worker_active=True,
            worker_role=role,  # ADR-030 §B: follower sees foreign leader_worker_id normally
        )

    def admit_follower(
        self,
        *,
        run_id: str,
        worker_id: str,
        config_hash: str,
        window_seconds: float,
    ) -> WorkerMembershipToken:
        """§B.1 step 2: atomic IMMEDIATE follower admission; returns the membership token.

        SLICE-5 CONSUMER: ``elspeth join`` (which performs the filesystem
        preflight BEFORE calling this). Refuses with
        :class:`JoinRefusedError` when the run is not RUNNING, the joiner's
        config hash disagrees, or the leader seat is not live (a follower
        must never be the first process on an abandoned run). Seat liveness
        is judged against the Landscape database clock read inside this
        transaction (ADR-047).

        The returned :class:`WorkerMembershipToken` is the ONLY production
        source of a follower's membership authority (ADR-048 amendment): the
        follower threads it by value into its heartbeat and departure verbs
        and, in wave 2, its claim verbs. A caller never constructs one.
        """
        with begin_write(self._engine) as conn:
            # Serialize admission with takeover and finalization before reading
            # their predicates. In particular, a finalizer locks its follower
            # roster before token decisions; joining after that snapshot must
            # re-observe the terminal run instead of leaving an active orphan.
            conn.execute(
                select(run_coordination_table.c.run_id).where(run_coordination_table.c.run_id == run_id).with_for_update()
            ).one_or_none()
            database_now = read_landscape_decision_time(conn)
            run = conn.execute(select(runs_table.c.status, runs_table.c.config_hash).where(runs_table.c.run_id == run_id)).one_or_none()
            if run is None:
                raise JoinRefusedError(run_id, "run not found")
            if run.status != RunStatus.RUNNING.value:
                raise JoinRefusedError(
                    run_id,
                    f"run status is {run.status!r} — "
                    + ("a terminal run cannot be joined" if run.status == RunStatus.COMPLETED.value else "use `elspeth resume`"),
                )
            if run.config_hash != config_hash:
                raise JoinRefusedError(
                    run_id,
                    f"resolved settings hash {config_hash!r} does not match the run's "
                    f"config_hash {run.config_hash!r}; a joiner must run the identical pipeline",
                )
            seat = conn.execute(
                select(
                    run_coordination_table.c.leader_worker_id,
                    (run_coordination_table.c.leader_heartbeat_expires_at >= database_now).label("seat_live"),
                ).where(run_coordination_table.c.run_id == run_id)
            ).one_or_none()
            seat_live = seat is not None and seat.leader_worker_id is not None and bool(seat.seat_live)
            if not seat_live:
                raise JoinRefusedError(
                    run_id,
                    "no live leader — take the seat with `elspeth resume`, or finalize the run with `elspeth abandon`",
                )
            self._insert_worker_row(
                conn,
                run_id=run_id,
                worker_id=worker_id,
                role="follower",
                window_seconds=window_seconds,
                entry_point="join",
                database_now=database_now,
            )
            record_coordination_event(
                conn,
                run_id=run_id,
                event_type="worker_register",
                worker_id=worker_id,
                leader_epoch=None,
                recorded_at=database_now,
                context={"role": "follower", "entry_point": "join"},
            )
            self._finalize_follower_admission_on(conn, run_id=run_id, worker_id=worker_id, window_seconds=window_seconds)
        return WorkerMembershipToken(run_id=run_id, worker_id=worker_id)

    @staticmethod
    def _finalize_follower_admission_on(conn: Connection, *, run_id: str, worker_id: str, window_seconds: float) -> None:
        """Renew the new member only while the already-locked foreign seat is live."""
        database_now = read_landscape_decision_time(conn)
        live_seat = conn.execute(
            select(run_coordination_table.c.run_id).where(
                run_coordination_table.c.run_id == run_id,
                run_coordination_table.c.leader_worker_id.is_not(None),
                run_coordination_table.c.leader_heartbeat_expires_at >= database_now,
            )
        ).one_or_none()
        if live_seat is None:
            raise JoinRefusedError(run_id, "leader seat expired during follower admission")
        expires = database_now + timedelta(seconds=window_seconds)
        renewed = conn.execute(
            update(run_workers_table)
            .where(
                run_workers_table.c.run_id == run_id,
                run_workers_table.c.worker_id == worker_id,
                run_workers_table.c.role == "follower",
                run_workers_table.c.status == "active",
            )
            .values(heartbeat_expires_at=expires)
        )
        if renewed.rowcount != 1:
            raise AuditIntegrityError(f"Newly admitted follower {worker_id!r} has no active same-run membership")
        record_issued_deadline(
            conn, key=_worker_deadline_key(run_id=run_id, worker_id=worker_id), expires_at=expires, window_seconds=window_seconds
        )

    def depart_worker(self, *, member_token: WorkerMembershipToken) -> None:
        """Member-fenced ``active → departed`` + ``worker_depart`` event. Idempotent.

        SLICE-5 CONSUMER: follower clean exit (§B.1 step 5), always from a
        teardown arm. The membership fence is the first statement (ADR-030
        D4); its refusal — the row already left ``active`` (finalize's
        leftover-member hygiene departed it first, or the leader evicted it)
        — is this verb's declared idempotent no-op: nothing is written and the
        ``fence_refusal`` row the fence recorded is the evidence that a
        departed identity tried to depart again.
        """
        try:
            with fenced_member_transaction(self._engine, member_token=member_token, verb="depart_worker") as conn:
                database_now = read_landscape_decision_time(conn)
                departed = conn.execute(
                    update(run_workers_table)
                    .where(
                        run_workers_table.c.run_id == member_token.run_id,
                        run_workers_table.c.worker_id == member_token.worker_id,
                        run_workers_table.c.status == "active",
                    )
                    .values(status="departed", departed_at=database_now)
                )
                if departed.rowcount != 1:
                    # The fence just matched this exact active row inside the
                    # same IMMEDIATE transaction; unreachable by construction,
                    # kept as a fail-closed integrity backstop.
                    raise AuditIntegrityError(
                        f"depart_worker: worker {member_token.worker_id!r} passed its membership fence but the departure UPDATE "
                        f"matched {departed.rowcount} rows; the run_workers row changed inside one IMMEDIATE transaction."
                    )
                record_coordination_event(
                    conn,
                    run_id=member_token.run_id,
                    event_type="worker_depart",
                    worker_id=member_token.worker_id,
                    leader_epoch=None,
                    recorded_at=database_now,
                    context={},
                )
        except RunMembershipLostError:
            return

    def evict_worker(
        self,
        *,
        token: CoordinationToken,
        target_worker_id: str,
        grace_seconds: float,
        window_seconds: float,
    ) -> bool:
        """§C.2 path 1: leader evicts a dead follower. Returns True iff evicted.

        SLICE-4 CONSUMER: the leader's housekeeping sweep. One leader-fenced
        IMMEDIATE transaction: (1) verify-and-extend epoch fence; (2) the
        target must be an active same-run follower and not the current leader;
        (3) the belt-and-braces no-unexpired-leases precondition (registry
        eviction must never outrun a lease the item layer still considers
        possibly alive); (4) CAS eviction gated on ``heartbeat_expires_at <
        database_now - grace`` (ADR-047). Rowcount 0 anywhere ⇒ benign skip (the worker
        heartbeated, is not an eligible follower, or still holds live leases).
        A fence miss raises
        :class:`RunLeadershipLostError` via :func:`fenced_leader_transaction`
        (refusal evented on a fresh connection).
        """
        # Local import: scheduler schema names live in the same module tree;
        # token_work_items is only needed by this slice-4 surface.
        from elspeth.core.landscape.schema import token_work_items_table

        with fenced_leader_transaction(self._engine, token=token, window_seconds=window_seconds, verb="evict_worker") as conn:
            # Serialize with membership-fenced claim/heartbeat verbs
            # (elspeth-6903f82511): lock the target's registry row BEFORE the
            # no-unexpired-leases precondition read.  Fenced lease verbs take
            # a shared lock on this row before granting/renewing a lease, so
            # once this exclusive lock is held every in-flight fenced lease
            # write has committed (its lease is visible to the precondition
            # below) and every later one blocks until this transaction commits
            # and then observes the evicted status.  Without the lock, a
            # fenced renewal could commit around the unlocked reads here and
            # the eviction would land on a worker holding a live lease.
            # An absent or ineligible registry row is a benign skip.  The
            # positive follower-role guard and explicit current-leader
            # exclusion are repeated in the terminal CAS below so caller
            # preselection is never the safety boundary (RC-07).
            target_registered = conn.execute(
                select(run_workers_table.c.worker_id)
                .where(
                    run_workers_table.c.worker_id == target_worker_id,
                    run_workers_table.c.run_id == token.run_id,
                    run_workers_table.c.role == "follower",
                    run_workers_table.c.worker_id != token.worker_id,
                )
                .with_for_update(of=run_workers_table)
            ).one_or_none()
            if target_registered is None:
                return False
            database_now = read_landscape_decision_time(conn)
            live_lease = conn.execute(
                select(token_work_items_table.c.work_item_id)
                .where(
                    token_work_items_table.c.run_id == token.run_id,
                    token_work_items_table.c.status == TokenWorkStatus.LEASED.value,
                    token_work_items_table.c.lease_owner == target_worker_id,
                    token_work_items_table.c.lease_expires_at >= database_now,
                )
                .limit(1)
            ).one_or_none()
            if live_lease is not None:
                return False
            evicted = conn.execute(
                update(run_workers_table)
                .where(
                    run_workers_table.c.worker_id == target_worker_id,
                    run_workers_table.c.run_id == token.run_id,
                    run_workers_table.c.role == "follower",
                    run_workers_table.c.worker_id != token.worker_id,
                    run_workers_table.c.status == "active",
                    run_workers_table.c.heartbeat_expires_at < database_now - timedelta(seconds=grace_seconds),
                )
                .values(status="evicted", evicted_at=database_now, evicted_by_worker_id=token.worker_id)
            )
            if evicted.rowcount != 1:
                return False
            record_coordination_event(
                conn,
                run_id=token.run_id,
                event_type="worker_evict",
                worker_id=target_worker_id,
                leader_epoch=token.leader_epoch,
                recorded_at=database_now,
                context={"evicted_by_worker_id": token.worker_id, "reason": "liveness_expired"},
            )
            return True

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _insert_worker_row(
        conn: Connection,
        *,
        run_id: str,
        worker_id: str,
        role: str,
        window_seconds: float,
        entry_point: str,
        database_now: datetime,
    ) -> None:
        """Register an ACTIVE member whose first heartbeat deadline is database time + window.

        The private caller passes its own Landscape admission sample so a
        minted seat/member pair starts with identical deadlines and stamps.
        Public mint/admission APIs accept no clock. Their transaction owner
        explicitly finalizes issuance after its remaining composition.
        """
        expires = database_now + timedelta(seconds=window_seconds)
        conn.execute(
            insert(run_workers_table).values(
                worker_id=worker_id,
                run_id=run_id,
                role=role,
                status="active",
                registered_at=database_now,
                heartbeat_expires_at=expires,
                pid=os.getpid(),
                hostname=socket.gethostname(),
                entry_point=entry_point,
            )
        )
        record_issued_deadline(
            conn, key=_worker_deadline_key(run_id=run_id, worker_id=worker_id), expires_at=expires, window_seconds=window_seconds
        )

    def dead_non_leader_workers(
        self,
        *,
        run_id: str,
        leader_worker_id: str,
        grace_seconds: float,
    ) -> tuple[str, ...]:
        """Return worker_ids of ACTIVE non-leader members whose heartbeat has expired.

        Read-only (plain connection, no write lock). Used by the leader
        housekeeping sweep (§C.2 path 1, slice 4) to enumerate candidates for
        individual ``evict_worker`` calls. Only ``role='follower'`` and
        ``status='active'`` rows are returned; the current leader worker ID is
        excluded independently. Departed/evicted rows are already done. The
        grace_seconds MUST equal the value passed to ``evict_worker`` so the
        liveness definition is consistent across the read (who to attempt to evict)
        and the write (the CAS guard inside evict_worker).

        Returns a tuple of worker_ids in deterministic
        ``(registered_at, worker_id)`` order; the identity tie-break prevents
        database-dependent ordering when workers register simultaneously.
        The caller then calls ``evict_worker`` for each, which is idempotent
        (benign skip if the worker heartbeated or holds a live lease).
        """
        with self._engine.connect() as conn:
            grace_threshold = read_landscape_decision_time(conn) - timedelta(seconds=grace_seconds)
            rows = conn.execute(
                select(run_workers_table.c.worker_id)
                .where(
                    run_workers_table.c.run_id == run_id,
                    run_workers_table.c.status == "active",
                    run_workers_table.c.role == "follower",
                    run_workers_table.c.worker_id != leader_worker_id,
                    run_workers_table.c.heartbeat_expires_at < grace_threshold,
                )
                .order_by(run_workers_table.c.registered_at, run_workers_table.c.worker_id)
            ).scalars()
        return tuple(rows)

    def _read_registered_workers(self, run_id: str) -> tuple[RegisteredWorker, ...]:
        """Forensic registry read for the BUSY-takeover diagnostic (§B.4).

        Plain read connection by necessity: the write lock is held by the
        very process we are diagnosing (WAL readers don't block on the
        writer; a write-intent connection here would re-deadlock). Best
        effort — an unreadable registry yields an empty roster, never masks
        the WriteLockHeldError.
        """
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(
                        run_workers_table.c.worker_id,
                        run_workers_table.c.role,
                        run_workers_table.c.status,
                        run_workers_table.c.pid,
                        run_workers_table.c.hostname,
                    )
                    .where(run_workers_table.c.run_id == run_id)
                    .order_by(run_workers_table.c.registered_at)
                ).all()
        except SQLAlchemyError:
            # Forensic best-effort read: a DB-layer failure (the registry is
            # unreadable while the writer we are diagnosing holds the lock)
            # yields an empty roster and must never mask the WriteLockHeldError
            # the caller raises. A non-DB (programmer) error is not an expected
            # outcome here and is allowed to surface.
            logger.warning("could not read run_workers roster for run %r while diagnosing a held write lock", run_id, exc_info=True)
            return ()
        return tuple(
            RegisteredWorker(
                worker_id=row.worker_id,
                role=row.role,
                status=row.status,
                pid=None if row.pid is None else int(row.pid),
                hostname=row.hostname,
            )
            for row in rows
        )
