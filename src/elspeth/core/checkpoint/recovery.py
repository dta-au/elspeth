"""Recovery protocol for resuming failed runs.

Provides the API for determining if and how a failed run can be resumed:
- can_resume(run_id) - Check if run can be resumed (failed status + checkpoint exists)
- get_resume_point(run_id) - Get checkpoint info for resuming

The actual resume logic (Orchestrator.resume()) is implemented separately.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select
from sqlalchemy.engine import Row

from elspeth.contracts import (
    Checkpoint,
    ResumeCheck,
    ResumePoint,
    RunStatus,
    SchemaContract,
)
from elspeth.contracts.barrier_scalars import BarrierScalars
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError, EmptyResumeStateError
from elspeth.contracts.freeze import freeze_fields
from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
from elspeth.core.checkpoint.manager import CheckpointCorruptionError, CheckpointManager
from elspeth.core.checkpoint.serialization import checkpoint_loads
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler import BarrierJournalRepository, SchedulerEventStore, SchedulerReadModel
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.core.landscape.schema import (
    SOURCE_COMPLETE_LIFECYCLE_STATES,
    group_losses_table,
    group_records_table,
    node_states_table,
    run_sources_table,
    runs_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_work_items_table,
)

if TYPE_CHECKING:
    from elspeth.core.dag import ExecutionGraph

_CHECKPOINT_STATE_CACHE_MAX = 16
_RESUMABLE_RUN_STATUSES = frozenset({RunStatus.FAILED, RunStatus.INTERRUPTED})
# (checkpoint_id, barrier_scalars_json) — keyed by payload so a re-read of the
# same checkpoint row with mutated JSON cannot serve a stale deserialization.
_CheckpointStateCacheKey = tuple[str, str | None]


__all__ = [
    "GroupBindingView",
    "GroupSatisfiabilityResumeGate",
    "GroupUnsatisfiableResumeError",
    "NonResumableRunError",
    "RecoveryManager",
    "ResumeCheck",  # Re-exported from contracts for convenience
    "ResumePoint",  # Re-exported from contracts for convenience
    "SourceLifecycleResumeGate",
    "UnsatisfiableGroupMember",
    "check_group_satisfiability_resumable",
    "check_run_status_resumable",
    "check_source_lifecycle_resumable",
    "group_binding_view_from_graph",
]


# TIER-2: Operator-interpretable refuse signal — the audit DB is intact and
# truthful; the run's CURRENT status (e.g. RUNNING: another worker holds the
# run mid-flight) means resuming now would be incorrect. Same register as
# elspeth.contracts.errors.IncompleteSourceResumeError: an operator-facing
# precondition failure carrying run_id + reason, NOT audit corruption
# (contrast the run-immutability guard's AuditIntegrityError in
# RunLifecycleRepository.update_run_status).
class NonResumableRunError(Exception):
    """Operational refusal of resume or export admission with its observed cause.

    ``RecoveryManager.can_resume()`` is ADVISORY — callers may skip it — so
    ``resume()`` re-checks the run status at entry via the same shared
    implementation (:func:`check_run_status_resumable`) and raises this
    error before any mutation (elspeth-2f23292372, operator option b).

    Carries ``run_id`` and the human-readable ``reason`` from the shared
    check so CLI/API callers can surface a precise "not resumable" outcome
    without parsing the exception text.
    """

    def __init__(self, run_id: str, reason: str, *, cause: ResumeRefusalCause) -> None:
        self.run_id = run_id
        self.reason = reason
        self.cause = cause
        super().__init__(f"Cannot resume run {run_id!r}: {reason}")


def _fetch_run(db: LandscapeDB, run_id: str) -> Row[Any] | None:
    """Fetch the ``runs`` row for ``run_id``, or None if absent."""
    with db.engine.connect() as conn:
        return conn.execute(select(runs_table).where(runs_table.c.run_id == run_id)).fetchone()


def check_run_status_resumable(db: LandscapeDB, run_id: str) -> tuple[RunStatus | None, ResumeCheck]:
    """Existence + run-status portion of :meth:`RecoveryManager.can_resume`.

    SINGLE shared implementation for the advisory ``can_resume`` surface and
    the enforcing entry guard in ``ResumeCoordinator.resume()`` — the two
    must never drift (elspeth-2f23292372).

    Returns:
        ``(run_status, check)``: ``run_status`` is ``None`` when the run does
        not exist. ``check`` carries ``can_resume=True`` when the status alone
        does not preclude resume; checkpoint existence, topology, and contract
        integrity remain ``can_resume``'s remit, not this function's.

    Raises:
        CheckpointCorruptionError: If the persisted status is not a valid
            ``RunStatus`` — audit corruption, never a clean refuse.
    """
    run = _fetch_run(db, run_id)
    if run is None:
        return None, ResumeCheck(can_resume=False, reason=f"Run {run_id} not found", cause=ResumeRefusalCause.RUN_NOT_FOUND)

    try:
        run_status = RunStatus(run.status)
    except ValueError as exc:
        raise CheckpointCorruptionError(f"Run {run_id} has invalid status {run.status!r}; audit trail is corrupt") from exc

    if run_status == RunStatus.COMPLETED:
        return run_status, ResumeCheck(can_resume=False, reason="Run already completed successfully", cause=ResumeRefusalCause.RUN_TERMINAL)

    if run_status == RunStatus.RUNNING:
        # §B.3 + §H test #2(c) (epoch 21, ADR-030, slice 4 flip):
        #
        # RUNNING + LIVE seat → REFUSED (name incumbent, direct to `elspeth
        # join`); slice 2 shipped this arm.
        #
        # RUNNING + EXPIRED/ABSENT seat → RESUMABLE (dead-leader takeover):
        # the seat's expiry IS the proof of lost custody; check-then-act here
        # is acceptable because the leadership CAS in ``acquire_run_leadership``
        # (the first durable act of resume()) is the true arbiter.
        #
        # Lives in the SHARED implementation so the advisory can_resume() and
        # the enforcing resume() entry guard produce the SAME verdict
        # (the elspeth-2f23292372 parity contract).
        leader = RunCoordinationRepository(db.engine).live_leader(run_id=run_id)
        if leader is not None and leader.seat_live:
            reason = (
                f"Run is in progress under live leader {leader.leader_worker_id!r} "
                f"(seat expires {leader.leader_heartbeat_expires_at.isoformat()}) — "
                "use `elspeth join` to attach as a follower"
            )
            return run_status, ResumeCheck(can_resume=False, reason=reason, cause=ResumeRefusalCause.LEADER_LIVE)
        # Seat is absent or expired: dead-leader takeover path → resumable.
        return run_status, ResumeCheck(can_resume=True)

    if run_status not in _RESUMABLE_RUN_STATUSES:
        return run_status, ResumeCheck(
            can_resume=False, reason=f"Run status {run_status.value!r} is not resumable", cause=ResumeRefusalCause.RUN_TERMINAL
        )

    return run_status, ResumeCheck(can_resume=True)


@dataclass(frozen=True, slots=True)
class SourceLifecycleResumeGate:
    """Facts + verdict from the shared source-lifecycle resume gate.

    ``lifecycle_by_source`` carries every declared source (name → state);
    ``incomplete_sources`` is the subset outside
    ``SOURCE_COMPLETE_LIFECYCLE_STATES`` — the exact payload
    ``IncompleteSourceResumeError`` requires. ``check`` is the advisory
    verdict: ``can_resume=False`` iff ``incomplete_sources`` is non-empty.
    An empty ``lifecycle_by_source`` passes this gate vacuously; the
    no-recorded-work refuse (``EmptyResumeStateError``) stays with its
    existing owners.
    """

    lifecycle_by_source: Mapping[str, str]
    incomplete_sources: Mapping[str, str]
    check: ResumeCheck

    def __post_init__(self) -> None:
        # Both mappings are built fresh per gate call from one SQL read, and
        # ``incomplete_sources`` is a derived subset of ``lifecycle_by_source``
        # — so the two share str values but no container. Freezing them keeps
        # the gate's evidence identical between the advisory ``can_resume()``
        # read and the enforcing ``resume()`` guard, which is the whole point
        # of the elspeth-1f5b83cd28 parity contract. Both readers are
        # read-only: ``resume()`` truth-tests ``lifecycle_by_source`` and hands
        # ``incomplete_sources`` to ``IncompleteSourceResumeError``, which
        # copies it via ``dict(source_states)``.
        freeze_fields(self, "lifecycle_by_source", "incomplete_sources")


def check_source_lifecycle_resumable(db: LandscapeDB, run_id: str) -> SourceLifecycleResumeGate:
    """Source-lifecycle portion of :meth:`RecoveryManager.can_resume`.

    SINGLE shared implementation for the advisory ``can_resume`` surface and
    the enforcing ``IncompleteSourceResumeError`` guard in
    ``ResumeCoordinator.resume()`` — the two must never drift
    (elspeth-1f5b83cd28; same parity contract as
    :func:`check_run_status_resumable`, elspeth-2f23292372). Resume never
    reopens a source (NullSource stands in): it re-drives only the durable
    scheduler work of rows the run already ingested, so a source that never
    reached a complete lifecycle state (``SOURCE_COMPLETE_LIFECYCLE_STATES``)
    may have unread rows that no resume can recover.
    """
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(run_sources_table.c.source_name, run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == run_id)
        ).fetchall()
    lifecycle_by_source = {str(row.source_name): str(row.lifecycle_state) for row in rows}
    incomplete_sources = {name: state for name, state in lifecycle_by_source.items() if state not in SOURCE_COMPLETE_LIFECYCLE_STATES}
    if incomplete_sources:
        source_summary = ", ".join(f"{source}={state}" for source, state in sorted(incomplete_sources.items()))
        reason = (
            f"source lifecycle is incomplete ({source_summary}) — resume replays only "
            "persisted row payloads, so unread source rows may exist; start a fresh run"
        )
        return SourceLifecycleResumeGate(
            lifecycle_by_source=lifecycle_by_source,
            incomplete_sources=incomplete_sources,
            check=ResumeCheck(can_resume=False, reason=reason, cause=ResumeRefusalCause.SOURCE_NOT_EXHAUSTED),
        )
    return SourceLifecycleResumeGate(
        lifecycle_by_source=lifecycle_by_source,
        incomplete_sources=incomplete_sources,
        check=ResumeCheck(can_resume=True),
    )


@dataclass(frozen=True, slots=True)
class GroupBindingView:
    """Config-derived binding facts for the group-satisfiability gate.

    Built from the builder's group-binding registry by
    :func:`group_binding_view_from_graph` (the ONE seam coupling this gate to
    the DAG layer); unit tests construct it directly. Boundness is a config
    fact, never a durable one — ``group_records`` deliberately carries no
    binding column (spec §4.3).
    """

    fork_branch_closers: Mapping[str, str]
    fork_branch_rosters: Mapping[str, tuple[str, ...]]
    scope_opener_closers: Mapping[str, str]

    def __post_init__(self) -> None:
        freeze_fields(self, "fork_branch_closers", "fork_branch_rosters", "scope_opener_closers")


@dataclass(frozen=True, slots=True)
class UnsatisfiableGroupMember:
    """One minted member of a bound group that no resume can ever settle."""

    closer_name: str
    group_id: str
    member_key: str
    kind: FrameKind


@dataclass(frozen=True, slots=True)
class GroupSatisfiabilityResumeGate:
    """Facts + verdict from the shared group-satisfiability resume gate.

    SINGLE shared implementation for the advisory ``can_resume`` surface and
    the enforcing entry guard in ``ResumeCoordinator.resume()`` — the two
    must never drift (the check_source_lifecycle_resumable precedent,
    elspeth-1f5b83cd28; spec §8).
    """

    unsatisfiable_members: tuple[UnsatisfiableGroupMember, ...]
    check: ResumeCheck


# TIER-2: same operator-refusal register as NonResumableRunError above — the
# audit DB is intact; the durable group state proves the roster can never
# close, so resuming would wedge at the barrier forever (the B3 dishonesty
# spec §5 names). Carries the members so CLI/API callers surface the exact
# scope/group/member without parsing text.
class GroupUnsatisfiableResumeError(Exception):
    """Raised by ``ResumeCoordinator.resume()`` when a bound group can never settle."""

    def __init__(self, run_id: str, members: Sequence[UnsatisfiableGroupMember]) -> None:
        if not members:
            raise ValueError("GroupUnsatisfiableResumeError requires at least one member")
        self.run_id = run_id
        self.members = tuple(members)
        summary = "; ".join(f"closer {m.closer_name!r} group {m.group_id!r} member {m.member_key!r}" for m in self.members)
        super().__init__(
            f"Cannot resume run {run_id!r}: {len(self.members)} bound-group member(s) are terminal "
            f"without settlement — neither arrived at their closer nor named in group_losses ({summary}). "
            "The group roster can never close; investigate the audit evidence instead of resuming over it."
        )


def _group_member_is_settled_or_live(
    conn: Any,
    *,
    run_id: str,
    closer_name: str,
    group_id: str,
    member_key: str,
) -> bool:
    """The three-limb satisfiability check for one minted member (spec §8).

    Lost: named in ``group_losses``. Live: a frame-bearing token with no
    completed terminal. Arrived: a journal row for the member that was HELD
    at this closer's barrier — the address columns name the barrier and
    ``barrier_blocked_at`` proves the hold. The address alone is not
    arrival: ``coalesce_name`` / ``row_union_name`` / ``collector_name`` /
    ``barrier_key`` are the member's barrier BINDING address (schema, spec
    §4.3), stamped on every in-region work item from the moment it is
    created, so a member that died inside the region before reaching its
    closer carries the same address as one the closer consumed. Only
    ``mark_blocked`` writes ``barrier_blocked_at`` (Task 1.3); a released
    or failed-group member keeps its stamp after the hold resolves, a
    diverted or dropped member never gets one (elspeth-76e936568e).
    """
    lost = conn.execute(
        select(group_losses_table.c.loss_id)
        .where(
            group_losses_table.c.run_id == run_id,
            group_losses_table.c.closer_name == closer_name,
            group_losses_table.c.group_id == group_id,
            group_losses_table.c.member_key == member_key,
            # NO adopted_epoch filter: §6.2 full-table-read discipline —
            # adoption is a leader-memory cursor, not a truth filter.
        )
        .limit(1)
    ).fetchone()
    if lost is not None:
        return True

    frames = token_lineage_frames_table
    live = conn.execute(
        select(frames.c.token_id)
        .where(
            frames.c.run_id == run_id,
            frames.c.group_id == group_id,
            frames.c.member_key == member_key,
            ~select(token_outcomes_table.c.outcome_id)
            .where(
                token_outcomes_table.c.run_id == run_id,
                token_outcomes_table.c.token_id == frames.c.token_id,
                token_outcomes_table.c.completed == 1,
            )
            .exists(),
        )
        .limit(1)
    ).fetchone()
    if live is not None:
        return True

    arrived = conn.execute(
        select(token_work_items_table.c.work_item_id)
        .select_from(
            token_work_items_table.join(
                frames,
                (token_work_items_table.c.token_id == frames.c.token_id) & (token_work_items_table.c.run_id == frames.c.run_id),
            )
        )
        .where(
            token_work_items_table.c.run_id == run_id,
            frames.c.group_id == group_id,
            frames.c.member_key == member_key,
            token_work_items_table.c.barrier_blocked_at.is_not(None),
            or_(
                token_work_items_table.c.coalesce_name == closer_name,
                token_work_items_table.c.row_union_name == closer_name,
                # Collector rows: collector_name is the address column; their
                # barrier_key is the compound "collector:<name>:<group-id>"
                # (WS4 Task 6), so the bare-equality barrier_key disjunct
                # below cannot match them — do not drop this disjunct.
                token_work_items_table.c.collector_name == closer_name,
                token_work_items_table.c.barrier_key == closer_name,
                # The compound collector address itself, built by THE single
                # construction site (never re-derived inline; the interlock
                # canary pins this call).
                token_work_items_table.c.barrier_key == collector_barrier_key(closer_name, group_id),
            ),
        )
        .limit(1)
    ).fetchone()
    return arrived is not None


def check_group_satisfiability_resumable(
    db: LandscapeDB,
    run_id: str,
    bindings: GroupBindingView,
) -> GroupSatisfiabilityResumeGate:
    """Group-satisfiability portion of :meth:`RecoveryManager.can_resume` (spec §8).

    SINGLE shared implementation for the advisory ``can_resume`` surface and
    the enforcing ``GroupUnsatisfiableResumeError`` guard in
    ``ResumeCoordinator.resume()`` — the two must never drift (the
    check_source_lifecycle_resumable two-surface precedent). Every minted
    member of every bound group must be non-terminal, arrived at its closer,
    or named in ``group_losses``; otherwise refuse with closer, group, and
    member named. Unbound groups are inert provenance and never refuse.
    """
    unsatisfiable: list[UnsatisfiableGroupMember] = []
    with db.engine.connect() as conn:
        # --- FORK groups: roster authority is the declared branch list. ---
        fork_rows = conn.execute(
            select(token_lineage_frames_table.c.group_id, token_lineage_frames_table.c.member_key)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.kind == FrameKind.FORK.value,
            )
            .distinct()
        ).fetchall()
        fork_member_pairs = [(str(row.group_id), str(row.member_key)) for row in fork_rows]

        for group_id in sorted({pair_group_id for pair_group_id, _ in fork_member_pairs}):
            seen = {member_key for pair_group_id, member_key in fork_member_pairs if pair_group_id == group_id}
            bound = {member for member in seen if member in bindings.fork_branch_closers}
            if not bound:
                continue  # fully unbound fork: pure fan-out, no roster watching
            if bound != seen:
                raise AuditIntegrityError(
                    f"Fork group {group_id!r} in run {run_id!r} violates whole-roster closure "
                    f"(ruling 23): members {sorted(seen - bound)} are unbound while "
                    f"{sorted(bound)} bind a closer. Config/audit disagreement."
                )
            sample = next(iter(bound))
            closer_name = bindings.fork_branch_closers[sample]
            roster = bindings.fork_branch_rosters[sample]
            for member_key in roster:
                if not _group_member_is_settled_or_live(
                    conn, run_id=run_id, closer_name=closer_name, group_id=group_id, member_key=member_key
                ):
                    unsatisfiable.append(
                        UnsatisfiableGroupMember(closer_name=closer_name, group_id=group_id, member_key=member_key, kind=FrameKind.FORK)
                    )

        # --- EXPAND groups: roster authority is group_records + frames. ---
        if bindings.scope_opener_closers:
            expand_rows = conn.execute(
                select(
                    group_records_table.c.group_id,
                    group_records_table.c.member_count,
                    node_states_table.c.node_id,
                )
                .select_from(
                    group_records_table.join(
                        node_states_table,
                        (group_records_table.c.opener_token_id == node_states_table.c.token_id)
                        & (group_records_table.c.run_id == node_states_table.c.run_id),
                    )
                )
                .where(
                    group_records_table.c.run_id == run_id,
                    group_records_table.c.kind == FrameKind.EXPAND.value,
                    node_states_table.c.node_id.in_(sorted(bindings.scope_opener_closers)),
                )
                .distinct()
            ).fetchall()
            for row in expand_rows:
                group_id = str(row.group_id)
                closer_name = bindings.scope_opener_closers[str(row.node_id)]
                minted = {
                    str(r.member_key)
                    for r in conn.execute(
                        select(token_lineage_frames_table.c.member_key)
                        .where(
                            token_lineage_frames_table.c.run_id == run_id,
                            token_lineage_frames_table.c.group_id == group_id,
                            token_lineage_frames_table.c.kind == FrameKind.EXPAND.value,
                        )
                        .distinct()
                    )
                }
                if len(minted) != int(row.member_count):
                    raise AuditIntegrityError(
                        f"Expand group {group_id!r} in run {run_id!r}: group_records.member_count="
                        f"{int(row.member_count)} but {len(minted)} distinct member frames exist. "
                        "Roster cross-check failed (spec §5)."
                    )
                for member_key in sorted(minted):
                    if not _group_member_is_settled_or_live(
                        conn, run_id=run_id, closer_name=closer_name, group_id=group_id, member_key=member_key
                    ):
                        unsatisfiable.append(
                            UnsatisfiableGroupMember(
                                closer_name=closer_name, group_id=group_id, member_key=member_key, kind=FrameKind.EXPAND
                            )
                        )

    if unsatisfiable:
        shown = unsatisfiable[:5]
        detail = "; ".join(f"{m.kind.value} group {m.group_id!r} member {m.member_key!r} at closer {m.closer_name!r}" for m in shown)
        suffix = "" if len(unsatisfiable) <= 5 else f" (+{len(unsatisfiable) - 5} more)"
        reason = (
            f"{len(unsatisfiable)} bound-group member(s) can never settle — each is terminal without "
            f"arriving at its closer and without a group_losses record: {detail}{suffix}"
        )
        return GroupSatisfiabilityResumeGate(
            unsatisfiable_members=tuple(unsatisfiable),
            check=ResumeCheck(can_resume=False, reason=reason, cause=ResumeRefusalCause.GROUP_UNSATISFIABLE),
        )
    return GroupSatisfiabilityResumeGate(unsatisfiable_members=(), check=ResumeCheck(can_resume=True))


def group_binding_view_from_graph(graph: ExecutionGraph) -> GroupBindingView:
    """Project the builder's group-binding registry into the gate's input.

    THE single seam coupling the satisfiability gate to the DAG layer: a
    FORK binding contributes every declared branch (whole-roster, ruling
    23); an EXPAND binding contributes its opener node. The discriminator
    is ``GroupBinding.kind`` — an EXPAND binding's ``member_roster`` is
    ``()`` by contract (runtime roster authority is ``group_records``),
    so roster emptiness must never be used to tell the kinds apart.
    """
    fork_branch_closers: dict[str, str] = {}
    fork_branch_rosters: dict[str, tuple[str, ...]] = {}
    scope_opener_closers: dict[str, str] = {}
    for binding in graph.get_group_bindings().bindings:
        if binding.kind is FrameKind.FORK:
            roster = tuple(binding.member_roster)
            for branch in roster:
                fork_branch_closers[branch] = binding.closer_name
                fork_branch_rosters[branch] = roster
        else:  # FrameKind.EXPAND
            scope_opener_closers[str(binding.opener_node_id)] = binding.closer_name
    return GroupBindingView(
        fork_branch_closers=fork_branch_closers,
        fork_branch_rosters=fork_branch_rosters,
        scope_opener_closers=scope_opener_closers,
    )


class RecoveryManager:
    """Manages recovery of failed runs from checkpoints.

    Recovery protocol:
    1. Check if run can be resumed (failed status + checkpoint exists)
    2. Load checkpoint and barrier scalar metadata
    3. Resume re-drives the run's durable scheduler work (READY / LEASED /
       BLOCKED / PENDING_SINK items); no source row is ever re-derived

    Usage:
        recovery = RecoveryManager(db, checkpoint_manager)

        check = recovery.can_resume(run_id)
        if check.can_resume:
            resume_point = recovery.get_resume_point(run_id)
            # Pass resume_point to Orchestrator.resume()
    """

    def __init__(self, db: LandscapeDB, checkpoint_manager: CheckpointManager) -> None:
        """Initialize with Landscape database and checkpoint manager.

        Args:
            db: LandscapeDB instance for querying run status
            checkpoint_manager: CheckpointManager for loading checkpoints
        """
        self._db = db
        self._checkpoint_manager = checkpoint_manager
        self._checkpoint_state_cache: dict[_CheckpointStateCacheKey, BarrierScalars | None] = {}

    def can_resume(self, run_id: str, graph: ExecutionGraph) -> ResumeCheck:
        """Check if a run can be resumed.

        A run can be resumed if:
        - It exists in the database
        - Its status is "failed" (not "completed" or "running")
        - At least one checkpoint exists for recovery
        - The checkpoint's upstream topology is compatible with current graph
        - Every declared source reached a complete lifecycle state
          (``SOURCE_COMPLETE_LIFECYCLE_STATES``) — the same precondition
          ``resume()`` enforces via ``IncompleteSourceResumeError``
        - Every minted member of every bound group is satisfiable (spec §8) —
          the same precondition ``resume()`` enforces via
          ``GroupUnsatisfiableResumeError``
        - The stored schema contract passes integrity verification (if present)

        Args:
            run_id: The run to check
            graph: The current execution graph to validate against

        Returns:
            ResumeCheck with can_resume=True if resumable,
            or can_resume=False with reason explaining why not.

        Raises:
            CheckpointCorruptionError: If schema contract integrity check fails.
                This is a Tier 1 failure - corruption cannot be silently ignored.
        """
        # Existence + status checks live in check_run_status_resumable so
        # this advisory surface and the enforcing entry guard in
        # ResumeCoordinator.resume() share ONE implementation.
        _run_status, status_check = check_run_status_resumable(self._db, run_id)
        if not status_check.can_resume:
            return status_check

        checkpoint = self._checkpoint_manager.get_latest_checkpoint(run_id)
        if checkpoint is None:
            # F1 Task 3.2: journal-flavoured refuse — the checkpoint row is the
            # run's resume BASELINE (scalars + topology anchor); buffered work
            # itself lives in journal BLOCKED rows. D4 (Task 3.3) guarantees a
            # sequence-0 baseline exists for every checkpointing-enabled run.
            return ResumeCheck(
                can_resume=False,
                reason="Run has no resume baseline (run predates run-start checkpointing or checkpointing was disabled)",
                cause=ResumeRefusalCause.CHECKPOINT_MISSING,
            )

        # Validate topological compatibility
        validator = CheckpointCompatibilityValidator()
        topology_check = validator.validate(checkpoint, graph)
        if not topology_check.can_resume:
            return topology_check

        # Source-lifecycle completeness (elspeth-1f5b83cd28): the same shared
        # gate resume() enforces via IncompleteSourceResumeError. A clean,
        # interpretable refuse, so it precedes the contract-corruption raise.
        lifecycle_gate = check_source_lifecycle_resumable(self._db, run_id)
        if not lifecycle_gate.check.can_resume:
            return lifecycle_gate.check

        # Group satisfiability (spec §8; ADR-038 amendment, ADR-042 D4):
        # every minted member of every bound group must be non-terminal,
        # arrived at its closer, or named in group_losses — otherwise no
        # resume can ever close the roster and the run would wedge at the
        # barrier. Same shared implementation as resume()'s enforcing guard.
        group_gate = check_group_satisfiability_resumable(self._db, run_id, group_binding_view_from_graph(graph))
        if not group_gate.check.can_resume:
            return group_gate.check

        # Verify schema contract integrity (Tier 1 - raises on corruption)
        # This must happen AFTER topology validation passes, as contract
        # corruption is a more serious failure than config mismatch.
        # Note: Returns None if no contract stored (valid for legacy runs)
        self.verify_contract_integrity(run_id)

        return ResumeCheck(can_resume=True)

    def get_resume_point(self, run_id: str, graph: ExecutionGraph) -> ResumePoint:
        """Get the resume point for a failed run.

        Returns all information needed to resume processing:
        - The checkpoint itself (for audit trail)
        - Sequence number for ordering
        - Deserialized barrier scalar metadata (if any)

        Args:
            run_id: The run to get resume point for
            graph: The current execution graph to validate against

        Returns:
            ResumePoint if run can be resumed.

        Raises:
            NonResumableRunError: The observed admission check refused resume.
        """
        check = self.can_resume(run_id, graph)
        if not check.can_resume:
            assert check.reason is not None and check.cause is not None
            raise NonResumableRunError(run_id, check.reason, cause=check.cause)

        # get_latest_checkpoint is a raw persistence read: it returns a
        # checkpoint or None and raises CheckpointCorruptionError on malformed
        # data. Compatibility is the validator's job, below.
        # No handler: corruption propagates.
        checkpoint = self._checkpoint_manager.get_latest_checkpoint(run_id)
        if checkpoint is None:
            raise NonResumableRunError(run_id, "Run has no resume baseline", cause=ResumeRefusalCause.CHECKPOINT_MISSING)

        topology_check = CheckpointCompatibilityValidator().validate(checkpoint, graph)
        if not topology_check.can_resume:
            assert topology_check.reason is not None and topology_check.cause is not None
            raise NonResumableRunError(run_id, topology_check.reason, cause=topology_check.cause)

        self.verify_contract_integrity(run_id)
        barrier_scalars = self._restore_barrier_scalars(checkpoint)

        return ResumePoint(
            checkpoint=checkpoint,
            sequence_number=checkpoint.sequence_number,
            barrier_scalars=barrier_scalars,
        )

    def count_active_scheduler_work(self, run_id: str) -> int:
        """Count the run's non-terminal scheduler work items (READY / LEASED / BLOCKED / PENDING_SINK).

        Public resume-inspection surface: resume re-drives exactly this work
        (it never re-derives a source row), so the CLI resume preflight
        reports it as what a resume will process.
        """
        return SchedulerReadModel(self._db.engine).count_active_work(run_id=run_id)

    def count_blocked_barrier_items(self, run_id: str) -> int:
        """Count journal BLOCKED barrier holds for a run.

        Public resume-inspection surface (F1): "what will be restored rather
        than re-driven" is the journal's BLOCKED rows with a non-NULL
        ``barrier_key``. Used by the resume coordinator's quiescence gate
        (a run whose remaining work is blocked barrier holds must NOT
        early-complete) and by the CLI resume preflight display.
        """
        return BarrierJournalRepository(self._db.engine, events=SchedulerEventStore()).count_blocked_barrier_items(run_id=run_id)

    def _restore_barrier_scalars(self, checkpoint: Checkpoint) -> BarrierScalars | None:
        """Deserialize barrier scalar metadata once per observed checkpoint payload."""
        key = (checkpoint.checkpoint_id, checkpoint.barrier_scalars_json)
        if key in self._checkpoint_state_cache:
            return self._checkpoint_state_cache[key]

        scalars: BarrierScalars | None = None
        if checkpoint.barrier_scalars_json:
            # checkpoint_loads preserves float fidelity for the trigger-offset latches
            raw = checkpoint_loads(checkpoint.barrier_scalars_json)
            scalars = BarrierScalars.from_dict(raw)

        if len(self._checkpoint_state_cache) >= _CHECKPOINT_STATE_CACHE_MAX:
            oldest_key = next(iter(self._checkpoint_state_cache))
            del self._checkpoint_state_cache[oldest_key]
        self._checkpoint_state_cache[key] = scalars
        return scalars

    def _get_run(self, run_id: str) -> Row[Any] | None:
        """Get run metadata from the database.

        Args:
            run_id: The run to fetch

        Returns:
            Row result with run data, or None if not found
        """
        return _fetch_run(self._db, run_id)

    def verify_contract_integrity(self, run_id: str) -> SchemaContract:
        """Verify schema contract integrity for a run.

        Per ADR-025 §3 Decision 5, ``run_sources.schema_contract_json`` is the
        single authoritative writer/reader for per-source schema contracts;
        ``runs.schema_contract_json`` is no longer consulted. Every declared
        source in a run must have a recorded contract before any row from
        that source enters the pipeline (Fix 2 in the multi-source-token-
        scheduler change set), so missing rows here mean the audit trail
        was never populated — Tier-1 corruption.

        Verifies hash integrity on every source's contract. Returns the
        contract of the lowest-ordered ``source_node_id`` (deterministic)
        for the legacy single-source consumer surface — multi-source
        callers must reach into ``RunLifecycleRepository.get_run_source_resume_records``
        for per-source contracts.

        Args:
            run_id: Run to verify

        Returns:
            SchemaContract - the first source's contract, ordered by ``source_node_id``.

        Raises:
            CheckpointCorruptionError: If no ``run_sources`` rows exist, if any
                stored contract is missing, malformed, or has mismatched hash,
                or if the run itself doesn't exist.
                Per the Tier-1 trust model
                (docs/guides/data-trust-and-error-handling.md §The Three-Tier Trust Model),
                bad data in the audit trail crashes immediately.
        """
        factory = RecorderFactory(self._db)

        # Verify the run exists (Tier-1: missing run = corruption surfaced to caller).
        if factory.run_lifecycle.get_run(run_id) is None:
            raise CheckpointCorruptionError(f"Run '{run_id}' not found in audit trail. Resume cannot proceed against an unrecorded run.")

        try:
            source_records = factory.run_lifecycle.get_run_source_resume_records(run_id)
        except AuditIntegrityError as e:
            # get_run_source_resume_records raises AuditIntegrityError on every per-source
            # corruption mode: missing schema JSON, missing contract JSON, missing or
            # mismatched contract hash. Convert to CheckpointCorruptionError so the
            # checkpoint-resume call surface stays a single exception type.
            raise CheckpointCorruptionError(
                f"Contract integrity verification failed for run '{run_id}': {e}. "
                f"Resume aborted - per-source contract metadata is corrupt or missing."
            ) from e
        except (ValueError, KeyError) as e:
            # ContractAuditRecord.from_json() raises json.JSONDecodeError (subclass of
            # ValueError) for malformed JSON, or KeyError for missing required fields.
            # Both indicate Tier-1 data corruption — stored contract JSON is garbage.
            raise CheckpointCorruptionError(
                f"Contract integrity verification failed for run '{run_id}': {e}. "
                f"Resume aborted - stored per-source contract JSON is malformed (database corruption)."
            ) from e

        if not source_records:
            # ADR-025 §3: a run with no ``run_sources`` rows reflects the
            # "nothing to resume, start fresh" outcome — typically an
            # ``on_start`` failure, a source-level abort before the first
            # ingest, or an infrastructure crash before any row was
            # persisted. The audit DB is intact and truthfully records
            # that the run did no work; it is NOT Tier-1 corruption.
            #
            # Raising ``EmptyResumeStateError`` here lets the CLI present
            # a clean "this run is not resumable; start a fresh run"
            # message rather than the audit-corruption traceback the
            # legacy ``CheckpointCorruptionError`` produced — that was
            # the reachability gap reported by elspeth-241608388f, where
            # the CLI's outer ``try`` lacked any handler for the
            # corruption-typed bubble and the operator saw a misleading
            # invariant traceback for a benign outcome.
            #
            # ``EmptyResumeStateError`` is a subclass of
            # ``OrchestrationInvariantError`` so any existing
            # ``except OrchestrationInvariantError`` catch still
            # matches it by type; the CLI catches the typed exception
            # explicitly before the broader Tier-1 handler.
            raise EmptyResumeStateError(run_id=run_id)

        # Deterministic single-source return for the legacy caller surface.
        # Multi-source callers should reach into ``get_run_source_resume_records``
        # for per-source contracts; this method only confirms integrity and
        # exposes the canonical first-source view for the unit-test contract.
        first_source_node_id = sorted(source_records)[0]
        return source_records[first_source_node_id].schema_contract
