"""Barrier subsystem: journal-first intake and resume restore coordinators.

Extracted from RowProcessor (elspeth-e76a186916). Barrier adoption and
restore used to be choreography spread across the processor, the scheduler
repository, and the aggregation/coalesce executors, with the crash-window
ordering (open batch membership -> fenced adoption -> feed executor memory
-> evaluate trigger) preserved only by caller convention and docstring
prose. The two coordinators own that ordering behind one boundary:

- ``BarrierIntakeCoordinator`` — the ADR-030 §E.2/§E.3/§E.3a/§E.5
  journal-first intake pass: adopt intake-pending BLOCKED arrivals, feed
  executor memory with backdated accept timing, replay durable branch
  losses, and evaluate aggregation triggers from the same intake step as
  the triggering arrival's adoption. Each adopted row resolves to a typed
  ``BarrierIntakeDisposition`` (held / terminal / pending-sink /
  ready-continuation / flush-fired).
- ``BarrierRecoveryCoordinator`` — the F1 resume restore: rebuild
  aggregation buffers and coalesce pendings from journal BLOCKED rows +
  audit tables, reconciling the §E.3a/§E.4 crash windows before any
  executor mutation runs.

Both coordinators take their environmental lookups (repositories,
executors, navigator, clock) and the processor-owned continuation seams
(flush execution, coalesce fire completion, telemetry emission) as
injected collaborators; RowProcessor delegates through thin methods so its
public protocol is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from elspeth.contracts import RowResult, TokenInfo, TransformProtocol
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.barrier_scalars import AggregationNodeScalars, BarrierScalars, CoalescePendingScalars
from elspeth.contracts.enums import BatchStatus, FrameKind, TerminalOutcome, TerminalPath, TriggerType
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.identity import LineageFrame, path_fork_group_id
from elspeth.contracts.results import FailureInfo
from elspeth.contracts.scheduler import BatchMembershipSpec, BufferedOutcomeSpec, GroupLossSpec, TokenWorkItem
from elspeth.contracts.types import BranchName, CoalesceName, CollectorName, NodeID, RowUnionName
from elspeth.core.dag.group_bindings import GroupBinding, GroupBindingRegistry
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.core.landscape.scheduler_repository import GroupLoss, token_from_journal_item
from elspeth.engine.work_items import WorkItem, WorkItemFactory, resolve_merged_branch_barrier

if TYPE_CHECKING:
    from elspeth.contracts import (
        Batch,
        CommittedAggregationOutputReceipt,
        CommittedAggregationResidual,
        CommittedCoalesceResidual,
    )
    from elspeth.contracts.coordination import CoordinationToken
    from elspeth.contracts.plugin_context import PluginContext
    from elspeth.core.config import AggregationSettings
    from elspeth.core.landscape.data_flow_repository import DataFlowRepository
    from elspeth.core.landscape.execution_repository import ExecutionRepository
    from elspeth.core.landscape.scheduler import BarrierRestoreReadModel
    from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
    from elspeth.engine.clock import Clock
    from elspeth.engine.coalesce_executor import CoalesceExecutor, CoalesceOutcome
    from elspeth.engine.dag_navigator import DAGNavigator
    from elspeth.engine.executors import AggregationExecutor
    from elspeth.engine.executors.collector import CollectorExecutor, CollectorOutcome
    from elspeth.engine.processor import CollectorRelease, _PreparedAggregationRoute
    from elspeth.engine.row_union_executor import RowUnionExecutor, RowUnionOutcome, RowUnionRestoreEntry

logger = logging.getLogger(__name__)

# spec §6.3 item 5: the bare category token for an ESCALATED loss — an
# inner group's failure consumed the outer member. Never conflated with
# "row_union_group_failed" (a row-union group's OWN direct closure,
# RowUnionExecutor's `_CLOSED_BY_PRIOR_FAILURE`). Shared by
# `_stage_pending_escalations` (intake-only path) and
# `RowProcessor._settle_member_losses`'s in-claim escalation walk (WS3
# Task 9) — both write the SAME natural key in `group_losses` and must
# agree on the reason, or whichever commits first wins with the wrong one.
GROUP_FAILED_REASON = "group_failed"


@dataclass(frozen=True, slots=True)
class _LiveBarrierHold:
    """In-memory companion of one durable BLOCKED barrier hold (ADR-030 §E.2).

    Stashed by the processor at the moment a claimed token is about to block
    at a barrier (aggregation buffering / coalesce hold) and consumed by the
    next drain iteration's journal-first intake: the LIVE token preserves the
    exact post-transform payload and resume provenance the old in-claim accept
    used (N=1 parity). Inherited rows with no stash entry (leader takeover)
    fall back to journal rehydration with audit-derived attempt offsets —
    the same semantics as the restore path.
    """

    token: TokenInfo
    barrier_key: str


@dataclass(frozen=True, slots=True)
class BarrierJournalRestoreContext:
    """Resume inputs for the journal-based barrier restore (F1 design D3).

    Built by the resume path (``ResumeCoordinator``) and handed to
    ``RowProcessor.__init__``; its presence IS the resume signal — a normal
    run passes ``None`` and no restore sweep runs.

    Attributes:
        resume_checkpoint_id: Checkpoint id stamped on every journal-restored
            token (resume provenance).
        barrier_scalars: Underivable scalar barrier metadata from the
            checkpoint row (trigger latches / lost_branches). ``None`` means
            the checkpoint carried no scalars — every node restores with
            unlatched / no-losses defaults. The scalars snapshot is
            NON-transactional vs the journal (D3 staleness model): an absent
            entry always means not-fired / re-derivable, never corruption.
        batch_id_remap: old->retry batch_id mapping returned by
            ``handle_incomplete_batches`` — BUFFERED token_outcomes still
            reference the dead original batch ids, so the restored in-progress
            batch id must be read through this remap.
    """

    resume_checkpoint_id: str
    barrier_scalars: BarrierScalars | None
    batch_id_remap: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.resume_checkpoint_id:
            raise ValueError("BarrierJournalRestoreContext.resume_checkpoint_id must not be empty")
        object.__setattr__(self, "batch_id_remap", deep_freeze(self.batch_id_remap))


@dataclass(frozen=True, slots=True)
class _AggregationRestorePlan:
    """Derived restore inputs for one aggregation node (journal restore).

    Built during the derivation phase of
    ``BarrierRecoveryCoordinator.restore_from_journal`` so every audit read
    completes (and can raise) before any executor mutation runs.
    """

    node_id: NodeID
    items: Sequence[TokenWorkItem]
    member_order: Sequence[str]
    batch_id: str | None
    accepted_count_total: int
    completed_flush_count: int
    scalars: AggregationNodeScalars

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", deep_freeze(self.items))
        object.__setattr__(self, "member_order", deep_freeze(self.member_order))


class BarrierIntakeDispositionKind(StrEnum):
    """Typed disposition taxonomy for one adopted barrier arrival."""

    HELD = "held"
    TERMINAL = "terminal"
    PENDING_SINK = "pending_sink"
    READY_CONTINUATION = "ready_continuation"
    FLUSH_FIRED = "flush_fired"


@dataclass(frozen=True, slots=True)
class BarrierIntakeDisposition:
    """One adopted arrival's resolution at the intake boundary.

    ``results`` and ``child_items`` carry the arrival's outputs in the exact
    order the pre-extraction choreography appended them, so flattening a
    disposition sequence reproduces the historical result ordering.
    """

    kind: BarrierIntakeDispositionKind
    results: tuple[RowResult, ...] = ()
    child_items: tuple[WorkItem, ...] = ()


@dataclass(frozen=True, slots=True)
class BarrierIntakePassOutcome:
    """All dispositions produced by one §E.2 intake pass, in intake order."""

    dispositions: tuple[BarrierIntakeDisposition, ...]

    @property
    def results(self) -> list[RowResult]:
        return [result for disposition in self.dispositions for result in disposition.results]

    @property
    def child_items(self) -> list[WorkItem]:
        return [item for disposition in self.dispositions for item in disposition.child_items]


class BarrierIntakeCoordinator:
    """Journal-first barrier intake (ADR-030 §E.2/§E.3/§E.3a/§E.5, slice 3).

    Owns the ordered adoption sequence the executors' docstrings used to
    delegate to caller convention: batch membership opens BEFORE the fenced
    adoption verb, executor memory is fed ONLY on the adopted=True arm with
    backdated accept timing, and aggregation triggers are evaluated from the
    SAME intake step as the triggering arrival's adoption.
    """

    def __init__(
        self,
        *,
        run_id: str,
        scheduler: TokenSchedulerRepository,
        data_flow: DataFlowRepository,
        execution: ExecutionRepository,
        barrier_restore_reads: BarrierRestoreReadModel | ExecutionRepository,
        aggregation_executor: AggregationExecutor,
        coalesce_executor: CoalesceExecutor | None,
        nav: DAGNavigator,
        work_items: WorkItemFactory,
        clock: Clock,
        aggregation_settings: Mapping[NodeID, AggregationSettings],
        coalesce_node_ids: Mapping[CoalesceName, NodeID],
        branch_to_coalesce: Mapping[BranchName, CoalesceName],
        coordination_token: CoordinationToken | None,
        scheduler_lease_owner: str,
        live_barrier_holds: dict[str, _LiveBarrierHold],
        resume_checkpoint_id: str | None,
        flush_batch: Callable[[NodeID, TransformProtocol, PluginContext, TriggerType], tuple[tuple[RowResult, ...], list[WorkItem]]],
        complete_coalesce_fire: Callable[..., None],
        terminal_coalesce_row_result: Callable[..., RowResult],
        emit_token_completed: Callable[..., None],
        mark_coalesce_consumed_terminal: Callable[..., None],
        record_group_member_terminals: Callable[..., list[RowResult]],
        take_pending_group_losses: Callable[[], tuple[GroupLossSpec, ...]],
        row_union_executor: RowUnionExecutor | None = None,
        row_union_node_ids: Mapping[RowUnionName, NodeID] | None = None,
        branch_to_row_union: Mapping[BranchName, RowUnionName] | None = None,
        complete_row_union_fire: Callable[..., None] | None = None,
        released_row_union_items: Callable[..., tuple[WorkItem, ...]] | None = None,
        group_bindings: GroupBindingRegistry | None = None,
        collector_executor: CollectorExecutor | None = None,
        collector_node_ids: Mapping[CollectorName, NodeID] | None = None,
        complete_collector_fire: Callable[..., None] | None = None,
        route_collector_release: Callable[..., CollectorRelease] | None = None,
        merged_continuation_cursor: Callable[
            [TokenInfo, CoalesceName], tuple[CoalesceName | None, RowUnionName | None, CollectorName | None]
        ]
        | None = None,
    ) -> None:
        self._run_id = run_id
        self._scheduler = scheduler
        self._data_flow = data_flow
        self._execution = execution
        self._barrier_restore_reads = barrier_restore_reads
        self._aggregation_executor = aggregation_executor
        self._coalesce_executor = coalesce_executor
        self._nav = nav
        self._work_items = work_items
        self._clock = clock
        self._aggregation_settings = aggregation_settings
        self._coalesce_node_ids = coalesce_node_ids
        self._branch_to_coalesce = branch_to_coalesce
        self._coordination_token = coordination_token
        self._scheduler_lease_owner = scheduler_lease_owner
        self._live_barrier_holds = live_barrier_holds
        self._resume_checkpoint_id = resume_checkpoint_id
        self._flush_batch = flush_batch
        self._complete_coalesce_fire = complete_coalesce_fire
        self._terminal_coalesce_row_result = terminal_coalesce_row_result
        self._emit_token_completed = emit_token_completed
        self._mark_coalesce_consumed_terminal = mark_coalesce_consumed_terminal
        self._record_group_member_terminals = record_group_member_terminals
        self._take_pending_group_losses = take_pending_group_losses
        self._row_union_executor = row_union_executor
        self._row_union_node_ids: Mapping[RowUnionName, NodeID] = row_union_node_ids or {}
        self._branch_to_row_union: Mapping[BranchName, RowUnionName] = branch_to_row_union or {}
        self._complete_row_union_fire = complete_row_union_fire
        self._released_row_union_items = released_row_union_items
        self._group_bindings: GroupBindingRegistry = group_bindings if group_bindings is not None else GroupBindingRegistry(bindings=())
        self._collector_executor = collector_executor
        self._collector_node_ids: Mapping[CollectorName, NodeID] = collector_node_ids or {}
        self._complete_collector_fire = complete_collector_fire
        self._route_collector_release = route_collector_release
        self._merged_continuation_cursor = merged_continuation_cursor
        # META-31: a resumed processor's collector restore installs any
        # roster that is already complete as PARKED (WS4 fix-round #2/#3);
        # only a PluginContext-bearing sweep AFTER that restore can close
        # it, and the first intake pass is the earliest such point. Owed
        # until every swept outcome has been DISPOSED (22ce94253 review
        # I-1): the executor closes those groups durably before returning
        # them, so an outcome it has handed over is held below until its
        # own disposition succeeds — clearing the flag on the executor
        # call alone lost every outcome after a raising disposition.
        self._collector_restore_sweep_owed: bool = resume_checkpoint_id is not None and collector_executor is not None
        self._undisposed_sweep_outcomes: list[CollectorOutcome] = []
        # spec §6.3 (Task 8): parked closer FAIL verdicts awaiting settlement,
        # keyed on (closer_name, group_id). In-memory only — re-derivable at
        # takeover because replaying the durable losses re-fires the closer's
        # FAIL verdict, which re-parks the note (note_group_failed's own
        # docstring), and escalation staging is idempotent on the ledger's
        # natural key.
        self._failed_group_notes: dict[tuple[str, str], str] = {}

    def _require_coordination_token(self) -> CoordinationToken:
        """The leader fencing token, REQUIRED for the slice-3 adoption verbs."""
        if self._coordination_token is None:
            raise OrchestrationInvariantError(
                "Journal-first barrier intake requires the leader coordination token (ADR-030 §E.2): "
                "adopt_blocked_barrier_item / adopt_group_losses are fenced verbs with no "
                "unfenced arm. Construct RowProcessor with coordination_token — the orchestrator "
                "binds it at begin_run (epoch 1) or at the resume takeover CAS."
            )
        return self._coordination_token

    def _backdated_accept_monotonic(self, row: TokenWorkItem) -> float:
        """Convert a row's durable ``barrier_blocked_at`` onto the monotonic scale.

        §E.2 backdated accept timing — the EXACT clamped wall->monotonic
        transform the journal restore uses (coalesce restore_from_journal /
        aggregation elapsed_age derivation): trigger latches and coalesce
        arrival anchors are pure functions of durable state + config, hence
        invariant under leader takeover (§H 476).
        """
        if row.barrier_blocked_at is None:
            raise AuditIntegrityError(
                f"BLOCKED journal row for token {row.token_id!r} (run {self._run_id!r}) has NULL "
                "barrier_blocked_at — the backdated accept instant cannot be derived; journal "
                "corruption (mark_blocked stamps every hold)."
            )
        now_wall = self._clock.now_utc()
        return self._clock.monotonic() - max(0.0, (now_wall - row.barrier_blocked_at).total_seconds())

    def _token_for_intake(self, row: TokenWorkItem) -> TokenInfo:
        """Resolve the TokenInfo to feed executor memory for one adopted row.

        Live stash first (N=1 parity: the exact post-transform token the old
        in-claim accept used, with its original resume provenance). Without a
        live stash, the durable row is still authoritative: fresh leaders use
        offset zero for normal follower handoffs, while resume leaders use the
        audit-derived offset stamped with checkpoint provenance.
        """
        hold = self._live_barrier_holds.pop(row.token_id, None)
        if hold is not None:
            return hold.token
        if self._resume_checkpoint_id is None:
            return token_from_journal_item(row, attempt_offset=0, resume_checkpoint_id=None)
        max_attempts = self._barrier_restore_reads.get_max_node_state_attempts(self._run_id, [row.token_id])
        return token_from_journal_item(
            row,
            attempt_offset=max_attempts.get(row.token_id, -1) + 1,
            resume_checkpoint_id=self._resume_checkpoint_id,
        )

    def run_intake_pass(self, ctx: PluginContext) -> BarrierIntakePassOutcome:
        """One §E.2 intake pass: adopt arrivals, replay losses, fire triggers.

        Runs at the top of every drain iteration (and from the orchestrator's
        EOF loop via ``RowProcessor.run_barrier_intake``). Steps, in design
        order:

        1. arrival intake — every intake-pending BLOCKED barrier row
           (``barrier_adopted_epoch IS NULL``) is adopted via the fenced
           backdated adoption verb and fed into executor memory; coalesce
           adoption runs the executor accept, surfacing merge fires, group
           failures and late-arrival releases (§E.3a) here;
        2. per-adoption aggregation trigger evaluation — count/condition
           triggers fire from the SAME intake step as the triggering
           arrival's adoption (the §E.2 replacement for the deleted in-claim
           flush arm; batch composition is preserved because the check runs
           after EACH adoption, exactly like the old accept-then-check);
        3. branch-loss replay (§E.5) — unadopted durable losses are marked
           (journal-first) and replayed through ``notify_branch_lost``
           before the next trigger evaluation; at N=1 this is a structural
           no-op (record-then-notify already ran in-claim).

        Returns the typed dispositions in intake order; flattening their
        results/child_items reproduces the pre-extraction append ordering.
        """
        dispositions: list[BarrierIntakeDisposition] = []
        if (
            not self._aggregation_settings
            and self._coalesce_executor is None
            and self._row_union_executor is None
            and self._collector_executor is None
        ):
            return BarrierIntakePassOutcome(dispositions=())

        # ADR-030 §E.2 intake scan shape: only intake-pending rows
        # (barrier_adopted_epoch IS NULL).  The SQL predicate avoids
        # materializing adopted rows on every drain iteration — without it a
        # filling count-N batch costs O(N²/2) full-row hydrations (finding 2).
        pending_rows = self._scheduler.list_pending_blocked_barrier_items(run_id=self._run_id)
        if pending_rows:
            coalesce_keys = {str(name) for name in self._coalesce_node_ids}
            row_union_keys = {str(name) for name in self._row_union_node_ids}
            aggregation_keys = {str(node_id) for node_id in self._aggregation_settings}
            collector_names = {str(name) for name in self._collector_node_ids}
            for row in pending_rows:
                if row.barrier_key in aggregation_keys:
                    disposition = self._adopt_aggregation_row(row, ctx)
                elif row.barrier_key in coalesce_keys:
                    disposition = self._adopt_coalesce_row(row)
                elif row.barrier_key in row_union_keys:
                    disposition = self._adopt_row_union_row(row)
                elif row.collector_name is not None and row.collector_name in collector_names:
                    # A collector row is addressed by its CURSOR, not by a bare
                    # key: one collector spans many concurrent EXPAND groups,
                    # so its barrier_key is the compound
                    # collector:<name>:<group_id> and cannot sit in a name set.
                    disposition = self._adopt_collector_row(row, ctx)
                else:
                    raise AuditIntegrityError(
                        f"Intake-pending BLOCKED row for token {row.token_id!r} (run {self._run_id!r}) carries "
                        f"orphan barrier_key {row.barrier_key!r} (collector_name={row.collector_name!r}): not a "
                        f"configured coalesce ({sorted(coalesce_keys)}), row_union ({sorted(row_union_keys)}), "
                        f"aggregation node ({sorted(aggregation_keys)}), or collector ({sorted(collector_names)})."
                    )
                if disposition is not None:
                    dispositions.append(disposition)

        dispositions.extend(self._replay_group_losses(ctx))
        dispositions.extend(self._flush_restored_collector_groups(ctx))
        # spec §6.3 (Task 8) — escalation runs AFTER durable-loss replay, in
        # the SAME intake step: a note staged into the ledger THIS pass is
        # picked up by the NEXT pass's replay above (one-pass-per-drain-cycle
        # latency, spec-accepted).
        self._stage_pending_escalations()
        return BarrierIntakePassOutcome(dispositions=tuple(dispositions))

    def _adopt_aggregation_row(self, row: TokenWorkItem, ctx: PluginContext) -> BarrierIntakeDisposition | None:
        """Adopt one aggregation barrier row, then evaluate the node's trigger.

        Ordering by construction (the invariant the executors' "caller
        obligations" prose used to delegate to callers): batch membership
        opens BEFORE the fenced adoption verb, and executor memory is fed
        ONLY on the adopted=True arm.
        """
        if row.barrier_key is None:  # pragma: no cover - excluded by the query contract
            raise AuditIntegrityError(f"Intake aggregation row {row.work_item_id!r} has no barrier_key.")
        node_id = NodeID(row.barrier_key)
        coordination_token = self._require_coordination_token()
        # Resolve the token BEFORE the fenced verb so an invalid journal row is
        # refused with ZERO durable mutation. Valid follower handoffs can be
        # rebuilt from the durable row even without a live stash entry.
        token = self._token_for_intake(row)
        batch_id, ordinal = self._aggregation_executor.open_batch_membership(node_id)
        adoption = self._scheduler.adopt_blocked_barrier_item(
            run_id=self._run_id,
            work_item_id=row.work_item_id,
            token_id=row.token_id,
            barrier_key=row.barrier_key,
            membership=BatchMembershipSpec(batch_id=batch_id, ordinal=ordinal),
            buffered_outcome=BufferedOutcomeSpec(batch_id=batch_id),
            now=self._clock.now_utc(),
            coordination_token=coordination_token,
        )
        if not adoption.adopted:
            # Idempotent success-SKIP: already adopted (a racing duplicate of
            # this leader's own pass). MUST NOT re-feed memory (§C.4 row 6a).
            return None
        self._aggregation_executor.accept_adopted_row(node_id, token, accept_time=self._backdated_accept_monotonic(row))

        # Step 2: per-adoption trigger evaluation — the §E.2 home of the old
        # in-claim count/condition flush decision (accept-then-check), so
        # batch composition is byte-identical to the in-claim era.
        should_flush, trigger_type = self._aggregation_executor.check_flush_status(node_id)
        if not should_flush:
            return BarrierIntakeDisposition(kind=BarrierIntakeDispositionKind.HELD)
        transform = self._nav.resolve_plugin_for_node(node_id)
        if not isinstance(transform, TransformProtocol) or not transform.is_batch_aware:
            raise OrchestrationInvariantError(
                f"Aggregation node {node_id!r} fired a {trigger_type} trigger at intake but resolves to "
                f"{type(transform).__name__!r}, not a batch-aware transform. DAG/config inconsistency."
            )
        flush_results, flush_child_items = self._flush_batch(
            node_id,
            transform,
            ctx,
            trigger_type if trigger_type is not None else TriggerType.COUNT,
        )
        return BarrierIntakeDisposition(
            kind=BarrierIntakeDispositionKind.FLUSH_FIRED,
            results=tuple(flush_results),
            child_items=tuple(flush_child_items),
        )

    def _adopt_coalesce_row(self, row: TokenWorkItem) -> BarrierIntakeDisposition | None:
        """Adopt one coalesce barrier row and run the intake-time accept."""
        if row.barrier_key is None:  # pragma: no cover - excluded by the query contract
            raise AuditIntegrityError(f"Intake coalesce row {row.work_item_id!r} has no barrier_key.")
        if self._coalesce_executor is None:  # pragma: no cover - partition guarantees a coalesce key
            raise OrchestrationInvariantError(f"Intake coalesce row for {row.barrier_key!r} but no CoalesceExecutor is configured.")
        coalesce_name = CoalesceName(row.barrier_key)
        coordination_token = self._require_coordination_token()
        # Resolve the token BEFORE the fenced verb (refusal-before-mutation
        # for invalid journal rows — see the aggregation arm).
        token = self._token_for_intake(row)
        adoption = self._scheduler.adopt_blocked_barrier_item(
            run_id=self._run_id,
            work_item_id=row.work_item_id,
            token_id=row.token_id,
            barrier_key=row.barrier_key,
            membership=None,
            buffered_outcome=None,
            now=self._clock.now_utc(),
            coordination_token=coordination_token,
        )
        if not adoption.adopted:
            return None
        outcome = self._coalesce_executor.accept(
            token=token,
            coalesce_name=str(coalesce_name),
            arrival_time=self._backdated_accept_monotonic(row),
        )

        if outcome.held:
            return BarrierIntakeDisposition(kind=BarrierIntakeDispositionKind.HELD)

        if outcome.late_arrival:
            # §E.3a: the group already completed — release THIS row alone in
            # the same drain iteration, with forensic late-arrival context.
            # The executor no longer records the FAILURE outcome itself
            # (Task 6, spec §6.1) — terminalized below through the
            # settlement channel.
            #
            # Final review F1: a late arrival is a MEMBER terminal against
            # a group whose verdict was already rendered when it closed —
            # it is not a group failure, so it neither parks a FAIL note
            # nor escalates (`group_failed=False`). If the group MERGED, the
            # merged token is carrying the enclosing member forward and an
            # escalated loss against it would be semantically false; if the
            # group FAILED, its own failure arm already parked the note and
            # escalated through its consumed siblings, and the intake pass's
            # `_stage_pending_escalations` settles the roster once this
            # straggler's terminal lands. The producer's own fact
            # (`outcome.late_arrival`) is what selects this arm.
            #
            # Fix round 3 (Ruling 43): the settlement channel runs BEFORE
            # the durable scheduler release (mirroring Ruling 39's sweep-path
            # ordering); with no escalation here the drained group_losses
            # are always empty, but the ordering and the single
            # mark_blocked_barrier_terminal transaction stay the one channel.
            late_child_items: list[WorkItem] = []
            late_cascaded_results = self._record_group_member_terminals(
                tuple(outcome.consumed_tokens),
                failure_reason=outcome.failure_reason or "late_arrival_after_merge",
                child_items=late_child_items,
                group_failed=False,
            )
            late_group_losses = self._take_pending_group_losses()
            released = self._scheduler.mark_blocked_barrier_terminal(
                run_id=self._run_id,
                barrier_key=str(coalesce_name),
                token_ids=(token.token_id,),
                now=self._clock.now_utc(),
                coordination_token=coordination_token,
                release_context={
                    "late_arrival": True,
                    "reason": outcome.failure_reason,
                    "released_by": self._scheduler_lease_owner,
                    "scope_row_id": row.row_id,
                },
                group_losses=late_group_losses,
            )
            if released != 1:
                raise AuditIntegrityError(
                    f"Late-arrival release for token {token.token_id!r} at coalesce {coalesce_name!r} "
                    f"(run {self._run_id!r}) terminalized {released} rows; expected exactly one."
                )
            self._emit_token_completed(token, outcome=TerminalOutcome.FAILURE, path=TerminalPath.UNROUTED)
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.TERMINAL,
                results=(
                    RowResult(
                        token=token,
                        final_data=token.row_data,
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.UNROUTED,
                        error=FailureInfo(exception_type="CoalesceFailure", message=outcome.failure_reason or "late_arrival_after_merge"),
                    ),
                    *late_cascaded_results,
                ),
                child_items=tuple(late_child_items),
            )

        if outcome.merged_token is not None:
            return self._fire_coalesce_merge(coalesce_name, outcome, scope_row_id=row.row_id)

        if outcome.failure_reason:
            error_msg = outcome.failure_reason
            # The executor no longer writes any consumed branch's terminal
            # outcome itself (Task 6, spec §6.1) — every consumed token
            # (the arriving token included; it is a member of
            # outcome.consumed_tokens too) is terminalized here, through the
            # settlement channel, which also walks each one's REMAINING
            # lineage for an enclosing bound frame (escalation). Fix round 3
            # (Ruling 43): runs BEFORE the durable "release them all" below
            # so any escalated loss it stages is drained and threaded into
            # THAT SAME call — see the late-arrival arm's comment above for
            # the full rationale (this intake pass is out-of-claim too).
            self._note_coalesce_group_failed_from_token(closer_name=str(coalesce_name), token=token, reason=error_msg)
            cascade_child_items: list[WorkItem] = []
            cascaded_results = self._record_group_member_terminals(
                tuple(outcome.consumed_tokens),
                failure_reason=error_msg,
                child_items=cascade_child_items,
                group_failed=True,
            )
            # Group failure completed by this arrival: every consumed branch
            # (this one included) holds a BLOCKED row — release them all.
            self._mark_coalesce_consumed_terminal(
                coalesce_name=coalesce_name,
                consumed_tokens=tuple(outcome.consumed_tokens),
                group_losses=self._take_pending_group_losses(),
            )
            # Emit TokenCompleted telemetry AFTER Landscape recording. Only
            # the arriving token surfaces a RowResult of its own (the held
            # siblings' outcomes are recorded above with no RowResult of
            # their own) — the pre-§E.2 shape, unchanged by Task 6; any
            # cascaded consequence from the escalation walk above DOES
            # surface below.
            self._emit_token_completed(token, outcome=TerminalOutcome.FAILURE, path=TerminalPath.UNROUTED)
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.TERMINAL,
                results=(
                    RowResult(
                        token=token,
                        final_data=token.row_data,
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.UNROUTED,
                        error=FailureInfo(exception_type="CoalesceFailure", message=error_msg),
                    ),
                    *cascaded_results,
                ),
                child_items=tuple(cascade_child_items),
            )

        raise OrchestrationInvariantError(
            f"CoalesceOutcome for token {token.token_id} in coalesce '{coalesce_name}' is in invalid state: "
            f"held={outcome.held}, merged_token={outcome.merged_token is not None}, "
            f"failure_reason={outcome.failure_reason!r}"
        )

    def _adopt_row_union_row(self, row: TokenWorkItem) -> BarrierIntakeDisposition | None:
        """Adopt one row_union barrier row and run the intake-time accept.

        Mirrors the coalesce arm's fenced adoption and outcome dispatch, with
        the N->N release shape: a completed group emits every ORIGINAL branch
        token as a READY continuation in ONE atomic ``complete_barrier``
        transaction, in declared branch order.
        """
        if row.barrier_key is None:  # pragma: no cover - excluded by the query contract
            raise AuditIntegrityError(f"Intake row_union row {row.work_item_id!r} has no barrier_key.")
        if self._row_union_executor is None:  # pragma: no cover - partition guarantees a row_union key
            raise OrchestrationInvariantError(f"Intake row_union row for {row.barrier_key!r} but no RowUnionExecutor is configured.")
        if self._complete_row_union_fire is None or self._released_row_union_items is None:
            raise OrchestrationInvariantError(f"Intake row_union row for {row.barrier_key!r} but the fire-completion seams are not wired.")
        row_union_name = RowUnionName(row.barrier_key)
        coordination_token = self._require_coordination_token()
        token = self._token_for_intake(row)
        adoption = self._scheduler.adopt_blocked_barrier_item(
            run_id=self._run_id,
            work_item_id=row.work_item_id,
            token_id=row.token_id,
            barrier_key=row.barrier_key,
            membership=None,
            buffered_outcome=None,
            now=self._clock.now_utc(),
            coordination_token=coordination_token,
        )
        if not adoption.adopted:
            return None
        outcome = self._row_union_executor.accept(
            token=token,
            row_union_name=str(row_union_name),
            arrival_time=self._backdated_accept_monotonic(row),
        )

        if outcome.held:
            return BarrierIntakeDisposition(kind=BarrierIntakeDispositionKind.HELD)

        if outcome.late_arrival:
            self._note_row_union_group_failed_from_token(
                closer_name=str(row_union_name), token=token, reason=outcome.failure_reason or "late_arrival_after_release"
            )
            released = self._scheduler.mark_blocked_barrier_terminal(
                run_id=self._run_id,
                barrier_key=str(row_union_name),
                token_ids=(token.token_id,),
                now=self._clock.now_utc(),
                coordination_token=coordination_token,
                release_context={
                    "late_arrival": True,
                    "reason": outcome.failure_reason,
                    "released_by": self._scheduler_lease_owner,
                    "scope_row_id": row.row_id,
                },
            )
            if released != 1:
                raise AuditIntegrityError(
                    f"Late-arrival release for token {token.token_id!r} at row_union {row_union_name!r} "
                    f"(run {self._run_id!r}) terminalized {released} rows; expected exactly one."
                )
            self._emit_token_completed(token, outcome=TerminalOutcome.FAILURE, path=TerminalPath.UNROUTED)
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.TERMINAL,
                results=(
                    RowResult(
                        token=token,
                        final_data=token.row_data,
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.UNROUTED,
                        error=FailureInfo(exception_type="RowUnionFailure", message=outcome.failure_reason or "late_arrival_after_release"),
                    ),
                ),
            )

        if outcome.released_tokens:
            released_items = self._released_row_union_items(
                row_union_name=row_union_name,
                released_tokens=outcome.released_tokens,
            )
            self._complete_row_union_fire(
                row_union_name=row_union_name,
                consumed_tokens=tuple(outcome.consumed_tokens),
                scope_row_id=row.row_id,
                released_items=released_items,
            )
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.READY_CONTINUATION,
                child_items=released_items,
            )

        if outcome.failure_reason:
            # Whole-group failure completed by this arrival (v1 fail-closed):
            # every held branch — this one included — holds a BLOCKED row.
            self._note_row_union_group_failed_from_token(closer_name=str(row_union_name), token=token, reason=outcome.failure_reason)
            self._scheduler.mark_blocked_barrier_terminal(
                run_id=self._run_id,
                barrier_key=str(row_union_name),
                token_ids=tuple(consumed.token_id for consumed in outcome.consumed_tokens),
                now=self._clock.now_utc(),
                coordination_token=coordination_token,
                release_context={
                    "reason": outcome.failure_reason,
                    "released_by": self._scheduler_lease_owner,
                    "scope_row_id": row.row_id,
                },
            )
            self._emit_token_completed(token, outcome=TerminalOutcome.FAILURE, path=TerminalPath.UNROUTED)
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.TERMINAL,
                results=(
                    RowResult(
                        token=token,
                        final_data=token.row_data,
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.UNROUTED,
                        error=FailureInfo(exception_type="RowUnionFailure", message=outcome.failure_reason),
                    ),
                ),
            )

        raise OrchestrationInvariantError(
            f"RowUnionOutcome for token {token.token_id} in row_union '{row_union_name}' is in invalid state: "
            f"held={outcome.held}, released={len(outcome.released_tokens)}, failure_reason={outcome.failure_reason!r}"
        )

    def _adopt_collector_row(self, row: TokenWorkItem, ctx: PluginContext) -> BarrierIntakeDisposition | None:
        """Adopt one collector barrier row and run the intake-time accept (spec §5).

        Coalesce-shaped (no batch membership at arrival, fenced adoption,
        executor memory fed only on the adopted=True arm with the backdated
        arrival) — but ``accept`` takes the intake ``ctx``, because the
        arrival that completes the roster FLUSHES the group through its
        plugin right here. The row's compound ``barrier_key`` must be the
        one ``collector_barrier_key`` derives from its cursor and its own
        EXPAND frame: a cursor/key disagreement is two authorities for one
        fact and fails closed.
        """
        if row.barrier_key is None:  # pragma: no cover - excluded by the query contract
            raise AuditIntegrityError(f"Intake collector row {row.work_item_id!r} has no barrier_key.")
        if row.collector_name is None:  # pragma: no cover - partition guarantees a collector cursor
            raise AuditIntegrityError(f"Intake collector row {row.work_item_id!r} has no collector_name cursor.")
        if self._collector_executor is None:
            raise OrchestrationInvariantError(f"Intake collector row for {row.barrier_key!r} but no CollectorExecutor is configured.")
        frame = row.lineage_path[-1] if row.lineage_path else None
        if frame is None or frame.kind is not FrameKind.EXPAND:
            raise AuditIntegrityError(
                f"Intake collector row for token {row.token_id!r} (run {self._run_id!r}) has no innermost EXPAND "
                f"frame (lineage_path={row.lineage_path!r}); a collector member always carries its own group's frame."
            )
        expected_key = collector_barrier_key(str(row.collector_name), frame.group_id)
        if row.barrier_key != expected_key:
            raise AuditIntegrityError(
                f"Intake collector row for token {row.token_id!r} (run {self._run_id!r}) carries barrier_key "
                f"{row.barrier_key!r} but its cursor/frame derive {expected_key!r}; journal corruption."
            )
        coordination_token = self._require_coordination_token()
        token = self._token_for_intake(row)
        adoption = self._scheduler.adopt_blocked_barrier_item(
            run_id=self._run_id,
            work_item_id=row.work_item_id,
            token_id=row.token_id,
            barrier_key=row.barrier_key,
            membership=None,
            buffered_outcome=None,
            now=self._clock.now_utc(),
            coordination_token=coordination_token,
        )
        if not adoption.adopted:
            return None
        outcome = self._collector_executor.accept(
            token,
            str(row.collector_name),
            ctx,
            arrival_time=self._backdated_accept_monotonic(row),
        )
        return self._dispose_collector_outcome(outcome, scope_row_id=row.row_id)

    def _flush_restored_collector_groups(self, ctx: PluginContext) -> list[BarrierIntakeDisposition]:
        """META-31: the post-restore flush sweep, once per resume, after the restore.

        ``CollectorExecutor.flush_restored_complete_groups(ctx)`` closes
        every group the restore parked with an already-complete roster —
        completed by journaled arrivals, by ledger-rebuilt losses, or both —
        and returns their outcomes, which are disposed exactly like a live
        arrival's. Restore-without-sweep is the wedge the WS4 fix-round
        named — a parked group never settles otherwise. The sweep's only
        ordering constraint is that it follows the restore that parked the
        keys: it is independent of the loss replay in the same pass, because
        the restore rebuilds every restored group's losses from the FULL
        ledger, so the replay dedups those via ``has_replayed_member_loss``
        and never reaches a parked key.

        Resumable at both handoffs. Executor -> coordinator (408d48ed4): the
        executor un-parks each key only after its close succeeds and stashes
        the outcomes already produced when a later close raises, so a raise
        from the executor call propagates with nothing disposed here and the
        retry's executor call delivers the stash first. Coordinator ->
        disposition (22ce94253 review I-1): the executor has closed those
        groups DURABLY by the time they are returned, so each outcome is
        held in ``_undisposed_sweep_outcomes`` and dropped only after its
        own disposition succeeds; a raising disposition propagates with the
        failed outcome and every later one still held, the sweep stays owed,
        and the next intake pass resumes disposing from the held list
        without calling the executor again (it returned normally, so it
        holds nothing). The owed flag clears only once the list is empty.
        Clearing it on the executor call alone turned a lease loss inside
        one disposition into groups closed in the Landscape whose member
        terminals were never written and whose continuation never emitted,
        with no retry and no later resume able to see them (a closed group
        is not restored as pending).
        """
        if not self._collector_restore_sweep_owed or self._collector_executor is None:
            return []
        if not self._undisposed_sweep_outcomes:
            self._undisposed_sweep_outcomes.extend(self._collector_executor.flush_restored_complete_groups(ctx))
        dispositions: list[BarrierIntakeDisposition] = []
        while self._undisposed_sweep_outcomes:
            outcome = self._undisposed_sweep_outcomes[0]
            scope_row_id = outcome.consumed_tokens[0].row_id if outcome.consumed_tokens else ""
            disposition = self._dispose_collector_outcome(outcome, scope_row_id=scope_row_id)
            del self._undisposed_sweep_outcomes[0]
            if disposition is not None:
                dispositions.append(disposition)
        self._collector_restore_sweep_owed = False
        return dispositions

    def _unterminalized(self, tokens: Sequence[TokenInfo]) -> tuple[TokenInfo, ...]:
        """The subset of ``tokens`` with no completed terminal outcome yet."""
        missing: list[TokenInfo] = []
        for token in tokens:
            existing = self._data_flow.get_token_outcome(token.token_id)
            if existing is None or not existing.completed:
                missing.append(token)
        return tuple(missing)

    def _dispose_collector_outcome(self, outcome: CollectorOutcome, *, scope_row_id: str) -> BarrierIntakeDisposition | None:
        """Turn one ``CollectorOutcome`` (arrival OR replayed loss) into its disposition.

        ``held`` keeps the row BLOCKED — including the executor's post-closure
        same-token skip (M-1's mixed signal): at intake that arm is
        unreachable anyway, because a redelivered claim of an already-adopted
        row is ``adopted=False`` and never reaches ``accept``. A release
        consumes every member's BLOCKED row and emits the continuation in
        ONE ``complete_barrier``; a failure terminalizes the arrived members
        through the settle seam (which also walks their remaining lineage
        for an enclosing bound frame — escalation) and releases their rows.
        A plugin-free close with nothing consumed (all members lost under
        best_effort, or an empty expansion) has no rows to move.
        """
        if outcome.held:
            return BarrierIntakeDisposition(kind=BarrierIntakeDispositionKind.HELD)
        if outcome.collector_name is None or outcome.group_id is None:
            raise OrchestrationInvariantError("Non-held CollectorOutcome must carry collector_name and group_id")
        collector_name = CollectorName(outcome.collector_name)
        group_id = outcome.group_id
        barrier_key = collector_barrier_key(str(collector_name), group_id)

        if outcome.released_tokens:
            if self._complete_collector_fire is None or self._route_collector_release is None:
                raise OrchestrationInvariantError(
                    f"Collector {collector_name!r} released a group but the fire-completion seams are not wired."
                )
            # Item 14: the executor writes no survivor terminal (only the
            # Ruling-36 quarantine writes), and CollectorOutcome carries no
            # flag to consult (META-17) — the seam records whatever terminal
            # it finds MISSING among the consumed members, unconditionally.
            # A quarantined member already holds (FAILURE, QUARANTINED_AT_SOURCE)
            # and is skipped; a second write would trip
            # ix_token_outcomes_terminal_unique. Survivors carry
            # (SUCCESS, COALESCED) with sink_name None — the consumed-input
            # disposition coalesce members already carry, discriminated from
            # a routed output by is_counted_coalesced_output. Written BEFORE
            # complete_barrier, mirroring coalesce_tokens (member terminals
            # land with the release mint, then the journal transition).
            self._record_group_member_terminals(
                self._unterminalized(outcome.consumed_tokens),
                failure_reason="",
                child_items=[],
                group_failed=False,
                frame_kind=FrameKind.EXPAND,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.COALESCED,
            )
            release = self._route_collector_release(collector_name=collector_name, released_tokens=outcome.released_tokens)
            self._complete_collector_fire(
                collector_name=collector_name,
                group_id=group_id,
                consumed_tokens=tuple(outcome.consumed_tokens),
                scope_row_id=scope_row_id,
                release=release,
                group_losses=self._take_pending_group_losses(),
            )
            if release.sink_results:
                return BarrierIntakeDisposition(
                    kind=BarrierIntakeDispositionKind.PENDING_SINK,
                    results=tuple(replace(result, scheduler_pending_sink=True) for result in release.sink_results),
                )
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.READY_CONTINUATION,
                child_items=release.items,
            )

        if outcome.failure_reason:
            self.note_group_failed(closer_name=str(collector_name), group_id=group_id, reason=outcome.failure_reason)
            consumed_tokens = tuple(outcome.consumed_tokens)
            failure_child_items: list[WorkItem] = []
            # Arrived members of a failed group: (FAILURE, UNROUTED) — the
            # spec §6.3 "survivors terminate scope_group_failed" write — plus
            # ONE escalation walk over their shared remaining lineage (an
            # enclosing bound frame, if any, is staged a group_failed loss).
            # A failure arm never flushed, so no member holds a prior
            # terminal; the seam's own duplicate detection stands.
            cascaded_results = self._record_group_member_terminals(
                consumed_tokens,
                failure_reason=outcome.failure_reason,
                child_items=failure_child_items,
                group_failed=True,
                frame_kind=FrameKind.EXPAND,
            )
            if consumed_tokens:
                self._scheduler.mark_blocked_barrier_terminal(
                    run_id=self._run_id,
                    barrier_key=barrier_key,
                    token_ids=tuple(token.token_id for token in consumed_tokens),
                    now=self._clock.now_utc(),
                    coordination_token=self._require_coordination_token(),
                    release_context={
                        "reason": outcome.failure_reason,
                        "released_by": self._scheduler_lease_owner,
                        "scope_row_id": scope_row_id,
                    },
                    group_losses=self._take_pending_group_losses(),
                )
            failure_results: list[RowResult] = []
            for consumed in consumed_tokens:
                self._emit_token_completed(consumed, outcome=TerminalOutcome.FAILURE, path=TerminalPath.UNROUTED)
                failure_results.append(
                    RowResult(
                        token=consumed,
                        final_data=consumed.row_data,
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.UNROUTED,
                        error=FailureInfo(exception_type="CollectorGroupFailure", message=outcome.failure_reason),
                    )
                )
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.TERMINAL,
                results=(*failure_results, *cascaded_results),
                child_items=tuple(failure_child_items),
            )

        if outcome.closed_without_plugin is not None and not outcome.consumed_tokens:
            return None

        raise OrchestrationInvariantError(
            f"CollectorOutcome for collector '{collector_name}' group {group_id!r} is in invalid state: "
            f"held={outcome.held}, released={len(outcome.released_tokens)}, consumed={len(outcome.consumed_tokens)}, "
            f"failure_reason={outcome.failure_reason!r}, closed_without_plugin={outcome.closed_without_plugin!r}"
        )

    def _fire_coalesce_merge(
        self,
        coalesce_name: CoalesceName,
        outcome: CoalesceOutcome,
        *,
        scope_row_id: str,
    ) -> BarrierIntakeDisposition:
        """Complete an intake-time coalesce merge fire (terminal or not).

        Non-terminal: mirrors ``complete_coalesce_merge``'s shape — the merged
        child's READY continuation is inserted atomically with the consumption
        (F1/D6) and the same WorkItem is handed back for the caller's
        idempotent enqueue.

        Terminal: the COALESCED sink-bound result is emitted as a fresh
        PENDING_SINK row in the SAME atomic completion — the merged output is
        journal-durable the moment its inputs are consumed (the pre-§E.2
        in-claim ride to ``mark_pending_sink`` left it memory-only between the
        consumption and the claim disposition).
        """
        if outcome.merged_token is None:  # pragma: no cover - caller checks
            raise OrchestrationInvariantError("merged_token is None in _fire_coalesce_merge")
        if outcome.join_group_id is None:  # pragma: no cover - CoalesceOutcome invariant
            raise OrchestrationInvariantError("join_group_id is None but merged_token is set in _fire_coalesce_merge")
        coalesce_node_id = self._coalesce_node_ids[coalesce_name]
        if self._nav.resolve_next_node(coalesce_node_id) is None:
            terminal_result = self._terminal_coalesce_row_result(
                outcome.merged_token,
                coalesce_name,
                join_group_id=outcome.join_group_id,
                context=f"intake coalesce fire for token '{outcome.merged_token.token_id}'",
            )
            self._complete_coalesce_fire(
                coalesce_name=coalesce_name,
                consumed_tokens=tuple(outcome.consumed_tokens),
                scope_row_id=scope_row_id,
                merged_sink_result=terminal_result,
            )
            return BarrierIntakeDisposition(
                kind=BarrierIntakeDispositionKind.PENDING_SINK,
                results=(replace(terminal_result, scheduler_pending_sink=True),),
            )
        # A nested branch's release still carries an OUTER fork frame (this
        # coalesce's own frame is popped) — resolve the continuation's
        # barrier context FRESH from that branch identity, never reuse
        # coalesce_name (the barrier it was just released from): see
        # resolve_merged_branch_barrier's docstring (elspeth-0bd2cde19a / E1b).
        continuation_collector_name: CollectorName | None = None
        if self._merged_continuation_cursor is not None:
            # The processor's cursor derivation covers the scope case too: a
            # coalesce INSIDE a scope releases the scope's member, whose
            # continuation must hold at the collector (the collector cursor
            # replaces the coalesce cursor; see
            # RowProcessor._merged_continuation_cursor).
            continuation_coalesce_name, continuation_row_union_name, continuation_collector_name = self._merged_continuation_cursor(
                outcome.merged_token, coalesce_name
            )
        else:
            continuation_coalesce_name, continuation_row_union_name = resolve_merged_branch_barrier(
                outcome.merged_token.branch_name,
                completed_coalesce_name=coalesce_name,
                branch_to_coalesce=self._branch_to_coalesce,
                branch_to_row_union=self._branch_to_row_union,
            )
        merged_item = self._work_items.create(
            token=outcome.merged_token,
            current_node_id=coalesce_node_id,
            # Flat/unnested: the resolved name is unchanged from the
            # just-completed barrier, so supply coalesce_node_id too —
            # restoring WorkItemFactory.create's mismatch cross-check on
            # this path (elspeth-0bd2cde19a round-2 F4). Nested: only the
            # resolved name is known here; create() re-derives the node id.
            coalesce_node_id=(coalesce_node_id if continuation_coalesce_name == coalesce_name else None),
            coalesce_name=continuation_coalesce_name,
            row_union_name=continuation_row_union_name,
            collector_name=continuation_collector_name,
            join_group_id=outcome.join_group_id,
        )
        self._complete_coalesce_fire(
            coalesce_name=coalesce_name,
            consumed_tokens=tuple(outcome.consumed_tokens),
            scope_row_id=scope_row_id,
            merged_item=merged_item,
        )
        return BarrierIntakeDisposition(
            kind=BarrierIntakeDispositionKind.READY_CONTINUATION,
            child_items=(merged_item,),
        )

    def _row_id_for_loss(self, loss: GroupLoss) -> str:
        """Resolve the row-scoped ``scope_row_id`` for the group-loss replay fires.

        The unified ``group_losses`` ledger row carries no ``row_id``, and
        every executor call in ``_replay_group_losses`` is already keyed on
        ``loss.group_id`` (WS4 re-keyed them; nothing transitional remains).
        The only consumers left are the replay fires' ``scope_row_id`` (one
        per closer-kind arm of ``_replay_group_losses`` — a deliberately
        non-enumerating statement, C1 review M-1) — a genuinely row-scoped
        concept the ledger cannot supply, so this resolves it from the
        token's durable ``tokens`` row (every arm needs it: re-keying one arm
        does not free the shim while another still fires): an intake-path
        DB read (leader, once per unadopted loss), NOT the hot accounting
        path, so the pinned "never a DB query" commitment (§4.1) is
        untouched.
        """
        row_id = self._barrier_restore_reads.row_id_for_token(run_id=self._run_id, token_id=loss.token_id)
        if row_id is None:
            raise AuditIntegrityError(
                f"Group-loss {loss.loss_id!r} names token {loss.token_id!r} with no durable tokens row; "
                "the ledger references a token the audit trail never minted."
            )
        return row_id

    def _replay_group_losses(self, ctx: PluginContext | None) -> list[BarrierIntakeDisposition]:
        """spec §6.2 loss intake: mark unadopted durable losses, replay into memory.

        Journal-first: the fenced cursor mark commits BEFORE the in-memory
        replay — a crash between mark and replay loses nothing because the
        takeover restore derives lost_branches from the FULL loss table. For
        ordinary N=1 claim dispositions the coalesce/row_union replay arms
        are structurally idle: the producer already notified in-claim
        (record-then-notify), so ``has_recorded_branch_loss`` (or the
        executor's completed-keys check) dedups every row. Empty aggregation
        is the deliberate exception: it stages without notifying, commits the
        loss with its own barrier, then calls this intake seam so downstream
        consequences cannot precede the durable loss.

        The COLLECTOR arm is the second such exception, by design: the
        in-claim settle seam carries no ``PluginContext`` and a collector
        loss can complete a roster and flush, so the producer only STAGES
        and this replay — which has ``ctx`` — is the one in-memory notify.
        Its dedup predicate is ``CollectorExecutor.has_replayed_member_loss``
        (in-memory only: a takeover restore rebuilds ``pending.lost`` from
        the FULL ledger, adopted losses included, and this replay must not
        re-notify those) — never ``has_recorded_member_loss``, whose durable
        ledger fallback reports the very loss being replayed as already
        recorded and would make this arm a permanent no-op.
        """
        dispositions: list[BarrierIntakeDisposition] = []
        if self._coalesce_executor is None and self._row_union_executor is None and self._collector_executor is None:
            return dispositions
        losses = self._scheduler.list_unadopted_group_losses(run_id=self._run_id)
        if not losses:
            return dispositions
        coordination_token = self._require_coordination_token()
        self._scheduler.adopt_group_losses(
            run_id=self._run_id,
            loss_ids=[loss.loss_id for loss in losses],
            now=self._clock.now_utc(),
            coordination_token=coordination_token,
        )
        collector_names = {str(name) for name in self._collector_node_ids}
        for loss in losses:
            row_id = self._row_id_for_loss(loss)
            if loss.closer_name in collector_names:
                if self._collector_executor is None:
                    raise OrchestrationInvariantError(
                        f"Durable group loss {loss.loss_id!r} targets collector {loss.closer_name!r}, but no collector executor is configured."
                    )
                if ctx is None:
                    raise OrchestrationInvariantError(
                        f"Durable group loss {loss.loss_id!r} targets collector {loss.closer_name!r} on a replay path "
                        "with no PluginContext (the empty-aggregation flush); ruling 25 makes an aggregation member "
                        "inside a bound region unbuildable, so this loss should not exist. Engine/builder bug."
                    )
                if self._collector_executor.has_replayed_member_loss(loss.closer_name, loss.group_id, loss.member_key):
                    continue
                collector_outcome = self._collector_executor.notify_member_lost(
                    loss.closer_name,
                    loss.group_id,
                    loss.member_key,
                    loss.reason,
                    ctx,
                )
                if collector_outcome is None:
                    continue
                collector_disposition = self._dispose_collector_outcome(collector_outcome, scope_row_id=row_id)
                if collector_disposition is not None:
                    dispositions.append(collector_disposition)
                continue
            if loss.closer_name in {str(name) for name in self._row_union_node_ids}:
                if self._row_union_executor is None or self._complete_row_union_fire is None:
                    raise OrchestrationInvariantError(
                        f"Durable group loss {loss.loss_id!r} targets row_union {loss.closer_name!r}, "
                        "but no row_union executor/completion seam is configured."
                    )
                if self._row_union_executor.has_recorded_branch_loss(loss.closer_name, loss.group_id, loss.member_key):
                    continue
                row_union_outcome = self._row_union_executor.notify_branch_lost(
                    row_union_name=loss.closer_name,
                    fork_group_id=loss.group_id,
                    lost_branch=loss.member_key,
                    reason=loss.reason,
                )
                if row_union_outcome is None:
                    continue
                if not row_union_outcome.failure_reason:
                    raise OrchestrationInvariantError(f"Replayed row_union group loss {loss.loss_id!r} produced a non-failure outcome.")
                consumed_tokens = tuple(row_union_outcome.consumed_tokens)
                # loss.group_id IS the failed group's own id — no need to
                # re-derive it from a consumed token's lineage.
                self.note_group_failed(closer_name=loss.closer_name, group_id=loss.group_id, reason=row_union_outcome.failure_reason)
                self._complete_row_union_fire(
                    row_union_name=RowUnionName(loss.closer_name),
                    consumed_tokens=consumed_tokens,
                    scope_row_id=row_id,
                )
                row_union_failure_results: list[RowResult] = []
                for consumed_token in consumed_tokens:
                    self._emit_token_completed(consumed_token, outcome=TerminalOutcome.FAILURE, path=TerminalPath.UNROUTED)
                    row_union_failure_results.append(
                        RowResult(
                            token=consumed_token,
                            final_data=consumed_token.row_data,
                            outcome=TerminalOutcome.FAILURE,
                            path=TerminalPath.UNROUTED,
                            error=FailureInfo(exception_type="RowUnionFailure", message=row_union_outcome.failure_reason),
                        )
                    )
                dispositions.append(
                    BarrierIntakeDisposition(
                        kind=BarrierIntakeDispositionKind.TERMINAL,
                        results=tuple(row_union_failure_results),
                    )
                )
                continue
            if self._coalesce_executor is None:
                raise OrchestrationInvariantError(
                    f"Durable group loss {loss.loss_id!r} targets coalesce {loss.closer_name!r}, but no coalesce executor is configured."
                )
            if self._coalesce_executor.has_recorded_branch_loss(loss.closer_name, loss.group_id, loss.member_key):
                continue
            outcome = self._coalesce_executor.notify_branch_lost(
                coalesce_name=loss.closer_name,
                fork_group_id=loss.group_id,
                lost_branch=loss.member_key,
                reason=loss.reason,
            )
            if outcome is None:
                continue
            coalesce_name = CoalesceName(loss.closer_name)
            if outcome.merged_token is not None:
                dispositions.append(self._fire_coalesce_merge(coalesce_name, outcome, scope_row_id=row_id))
                continue
            if outcome.failure_reason:
                # Replayed must-fail (§6.2: a must-fail group fails within one
                # drain iteration of the loss becoming visible): mirror the
                # group-loss notification failure arm — RowResults for the
                # held siblings the failure consumed. The executor no longer
                # writes their terminal outcomes itself (Task 6, spec §6.1);
                # terminalized here through the settlement channel, which
                # also walks each one's REMAINING lineage for an enclosing
                # bound frame (escalation). Fix round 3 (Ruling 43): runs
                # BEFORE the durable "release them all" below so any
                # escalated loss it stages is drained and threaded into
                # THAT SAME call — this replay loop is out-of-claim too.
                # loss.group_id IS the failed group's own id — no need to
                # re-derive it from a consumed token's lineage.
                self.note_group_failed(closer_name=loss.closer_name, group_id=loss.group_id, reason=outcome.failure_reason)
                replay_child_items: list[WorkItem] = []
                cascaded_replay_results = self._record_group_member_terminals(
                    tuple(outcome.consumed_tokens),
                    failure_reason=outcome.failure_reason,
                    child_items=replay_child_items,
                    group_failed=True,
                )
                self._mark_coalesce_consumed_terminal(
                    coalesce_name=coalesce_name,
                    consumed_tokens=tuple(outcome.consumed_tokens),
                    group_losses=self._take_pending_group_losses(),
                )
                coalesce_failure_results: list[RowResult] = []
                for consumed_token in outcome.consumed_tokens:
                    self._emit_token_completed(consumed_token, outcome=TerminalOutcome.FAILURE, path=TerminalPath.UNROUTED)
                    coalesce_failure_results.append(
                        RowResult(
                            token=consumed_token,
                            final_data=consumed_token.row_data,
                            outcome=TerminalOutcome.FAILURE,
                            path=TerminalPath.UNROUTED,
                            error=FailureInfo(exception_type="CoalesceFailure", message=outcome.failure_reason),
                        )
                    )
                dispositions.append(
                    BarrierIntakeDisposition(
                        kind=BarrierIntakeDispositionKind.TERMINAL,
                        results=(*coalesce_failure_results, *cascaded_replay_results),
                        child_items=tuple(replay_child_items),
                    )
                )
                continue
            raise OrchestrationInvariantError(
                f"Replayed group loss {loss.loss_id!r} ({loss.closer_name!r}/{row_id!r}/{loss.member_key!r}) "
                f"produced an invalid CoalesceOutcome: held={outcome.held}, merged=None, failure_reason=None."
            )
        return dispositions

    def replay_durable_group_losses(self, ctx: PluginContext | None = None) -> tuple[BarrierIntakeDisposition, ...]:
        """Replay committed loss-ledger entries after their producer commits.

        The one caller without a ``ctx`` is the empty-aggregation flush
        (``RowProcessor._complete_aggregation_flush``): ruling 25 bans an
        aggregation inside any bound region, so its members carry no bound
        frame and it can stage no collector loss — the collector arm below
        fails closed if that ever stops being true, rather than flushing
        with a context it does not have.
        """
        return tuple(self._replay_group_losses(ctx))

    # ─────────────────────────────────────────────────────────────────────
    # Escalation — intake-only, verdicts wait for settlement (spec §6.3, Task 8)
    # ─────────────────────────────────────────────────────────────────────

    def note_group_failed(self, *, closer_name: str, group_id: str, reason: str) -> None:
        """Park a closer FAIL verdict for intake-time escalation (spec §6.3).

        Idempotent in-memory park; re-derivable at takeover because replaying
        the durable losses re-fires the closer's FAIL verdict, which re-parks
        the note, and escalation staging is idempotent on the ledger's
        natural key."""
        self._failed_group_notes[(closer_name, group_id)] = reason

    def _note_coalesce_group_failed_from_token(self, *, closer_name: str, token: TokenInfo, reason: str) -> None:
        """Park a FAIL verdict keyed on the arriving/consumed token's own
        innermost lineage frame (spec §6.3). Sound to index ``[-1]`` here
        ONLY because coalesce and fork are strictly LIFO-nested by
        construction (`pop_closer_frame`'s own docstring,
        `contracts/identity.py`) — a coalesce closer's own frame IS always
        the token's innermost frame. Do NOT reuse this for row_union (see
        `_note_row_union_group_failed_from_token`): unlike coalesce, a
        row-multiplying transform inside a row_union branch can legally
        stack a further frame on top of the branch's FORK frame before the
        token reaches the union. Empty ``lineage_path`` is unreachable in
        production (every fork child mints one) — tolerated as a silent
        no-op here only because dispatch-only synthetic test fixtures build
        lineage-free tokens on purpose, mirroring
        `_record_group_member_terminals`'s own empty-input tolerance."""
        if not token.lineage_path:
            return
        self.note_group_failed(closer_name=closer_name, group_id=token.lineage_path[-1].group_id, reason=reason)

    def _note_row_union_group_failed_from_token(self, *, closer_name: str, token: TokenInfo, reason: str) -> None:
        """Park a FAIL verdict using the token's innermost FORK frame (spec
        §6.3) — a SEARCH, never ``[-1]``. Unlike coalesce, row_union does
        not strictly LIFO-nest with an intervening multi-row transform: a
        row-multiplying transform inside the branch (e.g. an expand) can
        legally stack an EXPAND frame on top of the branch's FORK frame
        before the token reaches the union (elspeth-a5b86149d4,
        `pop_fork_frame`'s own docstring,
        tests/integration/pipeline/test_row_union_branch_cardinality.py) —
        the same reason `pop_fork_frame` searches for its FORK frame
        "wherever it sits" rather than popping the innermost one.
        `path_fork_group_id` performs that same innermost-first search. A
        token that reached a row_union closer always carries SOME FORK
        frame (that is how it got routed here); a lineage with no FORK
        frame at all is lineage corruption, not a legal shape — this fails
        closed rather than silently discarding the verdict."""
        group_id = path_fork_group_id(token.lineage_path)
        if group_id is None:
            raise OrchestrationInvariantError(
                f"row_union closer {closer_name!r} FAIL verdict for token {token.token_id!r} carries no FORK "
                f"frame anywhere in its lineage_path ({token.lineage_path!r}); a token cannot reach a "
                "row_union closer without one — lineage corruption."
            )
        self.note_group_failed(closer_name=closer_name, group_id=group_id, reason=reason)

    def _binding_for_closer_name(self, closer_name: str) -> GroupBinding | None:
        """The FAILED group's own binding (spec §7 rule 1: a closer closes at
        most one group, so ``closer_name`` resolves at most one binding).
        ``None`` means this build's registry carries no binding for the
        closer at all — a bare legacy processor wired only through
        ``branch_to_coalesce``/``branch_to_row_union`` (no ``group_bindings``),
        never a real built graph (``build_group_binding_registry`` mints a
        binding for every coalesce/row_union node a bound fork actually
        routes to). Treated as inert by the caller, matching
        ``GroupBindingRegistry.binding_for``'s own "no binding = nothing
        tracked" contract (spec §2) rather than raising."""
        for binding in self._group_bindings.bindings:
            if binding.closer_name == closer_name:
                return binding
        return None

    def _group_roster_settled(self, *, closer_name: str, group_id: str, binding: GroupBinding) -> bool:
        """Whether every minted member of the failed group has durably closed
        (spec §6.3 item 2): a `group_losses` row OR a completed
        `token_outcomes` row for the member's own token. FORK rosters are the
        binding's static `member_roster` (the config roster authority); EXPAND
        rosters are runtime-minted, so they are read from `token_lineage_frames`
        and cross-checked against `group_records.member_count` (spec §5)."""
        if binding.kind is FrameKind.FORK:
            roster = binding.member_roster
        else:
            roster = self._execution.member_keys_for_group(run_id=self._run_id, group_id=group_id)
            record = self._execution.get_group_record(run_id=self._run_id, group_id=group_id)
            if record is None or record.member_count != len(roster):
                raise AuditIntegrityError(
                    f"EXPAND group {group_id!r} (run {self._run_id!r}) roster mismatch: "
                    f"group_records.member_count={None if record is None else record.member_count!r}, "
                    f"DISTINCT token_lineage_frames.member_key count={len(roster)}."
                )
        settled_members = {
            loss.member_key
            for loss in self._scheduler.list_group_losses(run_id=self._run_id, closer_names=frozenset({closer_name}))
            if loss.group_id == group_id
        }
        for member_key in roster:
            if member_key in settled_members:
                continue
            token_id = self._execution.resolve_group_member_token(
                run_id=self._run_id, kind=binding.kind, group_id=group_id, member_key=member_key
            )
            outcome = self._data_flow.get_token_outcome(token_id)
            if outcome is None or not outcome.completed:
                return False
        return True

    def _enclosing_bound_frame(self, group_id: str) -> tuple[LineageFrame, GroupBinding] | None:
        """The ENCLOSING bound frame for a failed group (spec §6.3 item 3):
        read any member token's durable lineage frames, drop the failed
        group's own frame, walk the remainder innermost-first to the first
        BOUND frame. `None` means outermost — today's behaviour verbatim
        (ruling 19)."""
        witness_token_id = self._execution.any_member_token_for_group(run_id=self._run_id, group_id=group_id)
        if witness_token_id is None:
            raise AuditIntegrityError(
                f"Escalation for group {group_id!r} (run {self._run_id!r}) found no token_lineage_frames "
                "witness — the ledger references a group the audit trail never minted."
            )
        full_path = self._data_flow.load_lineage_paths(self._run_id, [witness_token_id])[witness_token_id]
        own_frame_indexes = [i for i, frame in enumerate(full_path) if frame.group_id == group_id]
        if not own_frame_indexes:
            raise AuditIntegrityError(
                f"Escalation for group {group_id!r} (run {self._run_id!r}): witness token {witness_token_id!r} was "
                f"selected BY membership of that group, yet its loaded lineage path carries no frame for it "
                f"({full_path!r}) — token_lineage_frames disagrees with itself."
            )
        for frame in reversed(full_path[: own_frame_indexes[0]]):
            binding = self._group_bindings.binding_for(frame)
            if binding is not None:
                return frame, binding
        return None

    def _escalated_member_token_id(self, *, failed_group_id: str, enclosing: LineageFrame) -> str:
        """The ONE token identity an escalated loss is recorded against
        (final review F1): the LIVE token at the enclosing frame, resolved by
        the same `resolve_group_member_token` the in-claim escalated walk
        uses (`RowProcessor._resolve_member_token_id`, Ruling 42) — so the
        two escalation write sites cannot disagree on a natural key and trip
        `record_group_loss`'s same-key-different-token Tier-1 check.

        Cross-checked against `group_records.opener_token_id` (spec §6.3
        item 4): the failed group's opener IS the enclosing member it
        consumed, and a group that FAILED minted no successor, so the two
        must coincide. A mismatch means a successor was minted at that frame
        by a group the ledger says failed — lineage corruption, fail closed."""
        record = self._execution.get_group_record(run_id=self._run_id, group_id=failed_group_id)
        if record is None:
            raise AuditIntegrityError(
                f"Escalation for group {failed_group_id!r} (run {self._run_id!r}) has no group_records row — "
                "the ledger references a group the audit trail never minted."
            )
        resolved = self._execution.resolve_group_member_token(
            run_id=self._run_id, kind=enclosing.kind, group_id=enclosing.group_id, member_key=enclosing.member_key
        )
        if resolved != record.opener_token_id:
            raise AuditIntegrityError(
                f"Escalation for failed group {failed_group_id!r} (run {self._run_id!r}): the live token at the "
                f"enclosing frame {enclosing!r} is {resolved!r}, but the failed group's opener is "
                f"{record.opener_token_id!r} — a successor was minted at that frame by a group the ledger says failed."
            )
        return resolved

    def _stage_escalation_loss(self, spec: GroupLossSpec, *, frame_kind: FrameKind, binding: GroupBinding) -> None:
        """Durable escalation write (spec §6.3): authenticate against the
        roster authority, then append — inside the fenced adoption
        transaction (no claimed token exists at intake time, so this
        substitutes for the in-claim claim-guard `_stage_group_loss` uses).
        The staged row is picked up by the NEXT intake pass's
        `_replay_group_losses` (which notifies the enclosing executor) —
        that is the one-pass-per-drain-cycle latency the spec accepts."""
        coordination_token = self._require_coordination_token()
        self._scheduler.stage_escalation_loss(
            run_id=self._run_id,
            spec=spec,
            frame_kind=frame_kind,
            declared_roster=binding.member_roster if frame_kind is FrameKind.FORK else None,
            recorded_by=self._scheduler_lease_owner,
            now=self._clock.now_utc(),
            coordination_token=coordination_token,
        )

    def _stage_pending_escalations(self) -> None:
        """Leader-only escalation pass, run once per intake iteration AFTER
        durable-loss replay: for each parked FAIL verdict whose roster has
        durably closed, stage ONE loss against the enclosing bound frame in
        the adoption transaction, authenticated against the roster
        authority, then notify the enclosing closer via the normal replay
        machinery on the next pass.

        Reason is the bare category token "group_failed" (spec §6.3 item 5)
        — NEVER conflated with "row_union_group_failed" (a direct row-union
        closure): "group_failed" names an ESCALATED loss, where an inner
        group's failure consumed the outer member; "row_union_group_failed"
        names a row-union group's OWN direct closure. Consumers key routing
        decisions on this distinction.
        """
        for (closer_name, group_id), _reason in list(self._failed_group_notes.items()):
            failed_binding = self._binding_for_closer_name(closer_name)
            if failed_binding is None:
                # No roster authority for this closer at all (see
                # _binding_for_closer_name) — cannot authenticate or even
                # check settlement; discard rather than crash a run over a
                # test-harness/legacy-wiring gap that never arises from a
                # real built graph: build_group_binding_registry guarantees a
                # binding for every executable coalesce/row_union/collector
                # closer (collector closers are reachable here since the
                # integration lift; filigree elspeth-c00a82bf97).
                del self._failed_group_notes[(closer_name, group_id)]
                continue
            if not self._group_roster_settled(closer_name=closer_name, group_id=group_id, binding=failed_binding):
                continue
            enclosing = self._enclosing_bound_frame(group_id)
            if enclosing is None:
                del self._failed_group_notes[(closer_name, group_id)]
                continue  # outermost: declared terminal handling already ran (ruling 19)
            frame, binding = enclosing
            spec = GroupLossSpec(
                closer_name=binding.closer_name,
                group_id=frame.group_id,
                member_key=frame.member_key,
                token_id=self._escalated_member_token_id(failed_group_id=group_id, enclosing=frame),
                reason=GROUP_FAILED_REASON,
            )
            self._stage_escalation_loss(spec, frame_kind=frame.kind, binding=binding)
            del self._failed_group_notes[(closer_name, group_id)]


class BarrierRecoveryCoordinator:
    """F1 resume restore: rebuild barrier state from journal + audit tables."""

    def __init__(
        self,
        *,
        run_id: str,
        scheduler: TokenSchedulerRepository,
        barrier_restore_reads: BarrierRestoreReadModel,
        execution: ExecutionRepository,
        aggregation_executor: AggregationExecutor,
        coalesce_executor: CoalesceExecutor | None,
        clock: Clock,
        aggregation_settings: Mapping[NodeID, AggregationSettings],
        coalesce_node_ids: Mapping[CoalesceName, NodeID],
        coordination_token: CoordinationToken,
        scheduler_lease_owner: str,
        row_union_executor: RowUnionExecutor | None = None,
        row_union_node_ids: Mapping[RowUnionName, NodeID] | None = None,
        released_row_union_items: Callable[..., tuple[WorkItem, ...]] | None = None,
        complete_row_union_fire: Callable[..., None] | None = None,
        emit_token_completed: Callable[..., None] | None = None,
        complete_committed_aggregation_residual: Callable[[CommittedAggregationResidual, Sequence[TokenWorkItem]], None] | None = None,
        prepare_committed_aggregation_output: (
            Callable[[CommittedAggregationOutputReceipt, Sequence[TokenWorkItem]], _PreparedAggregationRoute] | None
        ) = None,
        complete_committed_aggregation_output: Callable[[_PreparedAggregationRoute], None] | None = None,
        complete_committed_coalesce_residual: Callable[[CommittedCoalesceResidual, Sequence[TokenWorkItem]], None] | None = None,
        collector_executor: CollectorExecutor | None = None,
        collector_node_ids: Mapping[CollectorName, NodeID] | None = None,
    ) -> None:
        self._run_id = run_id
        self._scheduler = scheduler
        self._barrier_restore_reads = barrier_restore_reads
        self._collector_executor = collector_executor
        self._collector_node_ids: Mapping[CollectorName, NodeID] = collector_node_ids or {}
        self._execution = execution
        self._aggregation_executor = aggregation_executor
        self._coalesce_executor = coalesce_executor
        self._clock = clock
        self._aggregation_settings = aggregation_settings
        self._coalesce_node_ids = coalesce_node_ids
        self._coordination_token = coordination_token
        self._scheduler_lease_owner = scheduler_lease_owner
        self._row_union_executor = row_union_executor
        self._row_union_node_ids: Mapping[RowUnionName, NodeID] = row_union_node_ids or {}
        self._released_row_union_items = released_row_union_items
        self._complete_row_union_fire = complete_row_union_fire
        self._emit_token_completed = emit_token_completed
        self._complete_committed_aggregation_residual = complete_committed_aggregation_residual
        self._prepare_committed_aggregation_output = prepare_committed_aggregation_output
        self._complete_committed_aggregation_output = complete_committed_aggregation_output
        self._complete_committed_coalesce_residual = complete_committed_coalesce_residual

    def restore_from_journal(self, restore: BarrierJournalRestoreContext) -> None:
        """Rebuild aggregation buffers and coalesce pendings from journal BLOCKED rows.

        F1 resume path. The journal (token_work_items BLOCKED rows with a
        non-NULL barrier_key) is authoritative for buffered/held token
        payloads; counters, batch membership, hold state ids and attempt
        offsets derive from audit tables; the checkpoint contributes only
        the underivable scalars (``restore.barrier_scalars``).

        Discipline: ALL derivations complete (and raise, if they must) before
        any executor restore call mutates state. The coalesce restore is a
        single all-or-nothing call (its contract — a second call would discard
        the first); aggregation restores run per node afterwards, each
        internally validate-before-mutate.

        BLOCKED rows manufactured by the pre-epoch-20 blob-materialization
        restore path lacked ``barrier_blocked_at``; that writer is deleted and
        the epoch-20 delete-the-DB policy retired its rows, so the executors'
        NULL-means-corruption assert is honest — no live DB carries them.

        Raises:
            AuditIntegrityError: On any journal/audit disagreement (orphan
                barrier_key, missing/foreign batch ids, membership mismatch,
                NULL barrier_blocked_at, missing hold state ...).
        """
        now = self._clock.now_utc()
        # ADR-030 §E.4 belt: one run-wide duplicate-acceptance sweep at restore
        # entry. token_outcomes has NO non-terminal uniqueness — the adoption
        # CAS is the structural guard; >1 live BUFFERED rows for a token means
        # a deposed leader's unfenced intake wrote a second acceptance.
        duplicate_acceptances = self._barrier_restore_reads.find_duplicate_live_buffered_acceptances(self._run_id)
        if duplicate_acceptances:
            details = ", ".join(f"{token_id} ({count} live BUFFERED)" for token_id, count in duplicate_acceptances)
            raise AuditIntegrityError(
                f"Barrier journal restore for run {self._run_id!r} (resume checkpoint "
                f"{restore.resume_checkpoint_id!r}) found duplicate live BUFFERED acceptances: {details}. "
                "A deposed leader's unfenced intake wrote a second acceptance — refusing silent latest-wins."
            )
        items = self._scheduler.list_blocked_barrier_items(run_id=self._run_id)
        # Spec §4.3 codec-vs-table bidirectional check: each journal row's
        # decoded lineage_path must equal its token_lineage_frames rows
        # exactly, before any executor restore call mutates state.
        self._barrier_restore_reads.verify_lineage_journal_consistency(self._run_id, items)
        scalars = restore.barrier_scalars if restore.barrier_scalars is not None else BarrierScalars(aggregation={}, coalesce={})

        # ---- Partition (design D1) ----------------------------------------
        # A BLOCKED row's barrier KIND is decided by its barrier_key ONLY:
        # barrier_key == coalesce_name        -> coalesce barrier
        # barrier_key == str(aggregation node_id) -> aggregation barrier
        # NEVER partition on node_id (a BLOCKED row's node_id is the
        # enqueue-time cursor, not the barrier owner) and NEVER on
        # coalesce_name (aggregation rows may carry non-NULL coalesce_name
        # LINEAGE for tokens that will coalesce after the flush).
        coalesce_keys: set[str] = {str(name) for name in self._coalesce_node_ids}
        row_union_keys: set[str] = {str(name) for name in self._row_union_node_ids}
        aggregation_keys: set[str] = {str(node_id) for node_id in self._aggregation_settings}
        ambiguous = (coalesce_keys & aggregation_keys) | (row_union_keys & aggregation_keys) | (coalesce_keys & row_union_keys)
        if ambiguous:
            raise OrchestrationInvariantError(
                f"Barrier-key namespace collision between coalesce names and aggregation node ids: {sorted(ambiguous)}. "
                "The journal restore partition cannot disambiguate BLOCKED rows for these keys."
            )

        collector_names: set[str] = {str(name) for name in self._collector_node_ids}
        agg_items_by_node: dict[NodeID, list[TokenWorkItem]] = {}
        coalesce_items: list[TokenWorkItem] = []
        row_union_items: list[TokenWorkItem] = []
        collector_items: list[TokenWorkItem] = []
        intake_pending_count = 0
        for item in items:
            if item.barrier_key is None:  # pragma: no cover - excluded by the query contract
                raise AuditIntegrityError(
                    f"list_blocked_barrier_items returned a row without barrier_key "
                    f"(work_item_id={item.work_item_id!r}, run {self._run_id!r})."
                )
            # A collector row is recognised by its CURSOR (its compound
            # collector:<name>:<group_id> key cannot sit in a name set), and
            # the key must agree with that cursor plus the row's own EXPAND
            # frame — the same two-authority check the live intake applies.
            is_collector_row = item.collector_name is not None and item.collector_name in collector_names
            if is_collector_row:
                frame = item.lineage_path[-1] if item.lineage_path else None
                expected_key = (
                    collector_barrier_key(str(item.collector_name), frame.group_id)
                    if frame is not None and frame.kind is FrameKind.EXPAND
                    else None
                )
                if item.barrier_key != expected_key:
                    raise AuditIntegrityError(
                        f"BLOCKED collector journal row for token {item.token_id!r} (run {self._run_id!r}) carries "
                        f"barrier_key {item.barrier_key!r} but its cursor {item.collector_name!r} and lineage "
                        f"{item.lineage_path!r} derive {expected_key!r}; journal corruption — refusing to resume."
                    )
            elif (
                item.barrier_key not in coalesce_keys
                and item.barrier_key not in row_union_keys
                and item.barrier_key not in aggregation_keys
            ):
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} (run {self._run_id!r}, resume "
                    f"checkpoint {restore.resume_checkpoint_id!r}) carries orphan barrier_key "
                    f"{item.barrier_key!r} (collector_name={item.collector_name!r}): not a configured coalesce "
                    f"({sorted(coalesce_keys)}), row_union ({sorted(row_union_keys)}), aggregation node "
                    f"({sorted(aggregation_keys)}), or collector ({sorted(collector_names)}). The journal references a "
                    "barrier this pipeline no longer has — refusing to resume."
                )
            if item.barrier_adopted_epoch is None:
                # ADR-030 §E.2/§E.4: an intake-pending row was deposited but
                # never adopted (crash between mark_blocked and adoption, or a
                # mid-adoption rollback). It has NO batch_members/BUFFERED
                # rows and NO executor memory to restore — the legitimate
                # disposition is the next drain iteration's journal-first
                # intake, which adopts it under THIS leader's epoch.
                intake_pending_count += 1
                continue
            if is_collector_row:
                collector_items.append(item)
            elif item.barrier_key in coalesce_keys:
                coalesce_items.append(item)
            elif item.barrier_key in row_union_keys:
                row_union_items.append(item)
            else:
                agg_items_by_node.setdefault(NodeID(item.barrier_key), []).append(item)
        if intake_pending_count:
            logger.info(
                "barrier journal restore: %d intake-pending BLOCKED row(s) left for the journal-first intake (run %s)",
                intake_pending_count,
                self._run_id,
            )
        # Adopted rows only from here down: derivations (attempt offsets,
        # batch ids, hold state ids) cover restored memory, not intake-pending
        # rows.
        items = [
            *coalesce_items,
            *row_union_items,
            *collector_items,
            *(item for node_items in agg_items_by_node.values() for item in node_items),
        ]
        if collector_items and self._collector_executor is None:
            raise OrchestrationInvariantError(
                f"Journal has {len(collector_items)} adopted BLOCKED collector rows but no CollectorExecutor is configured."
            )
        if coalesce_items and self._coalesce_executor is None:
            raise OrchestrationInvariantError(
                f"Journal has {len(coalesce_items)} BLOCKED coalesce rows but no CoalesceExecutor is configured."
            )
        if row_union_items and self._row_union_executor is None:
            raise OrchestrationInvariantError(
                f"Journal has {len(row_union_items)} adopted BLOCKED row_union rows but no RowUnionExecutor is configured."
            )

        # ---- Audit derivations (no mutation yet) ---------------------------
        # Attempt offsets: max node_states attempt per journal token, + 1.
        # Derived here with ONE focused query rather than plumbed from
        # recovery's incomplete_by_row map: that map's exclusion set reads
        # journal BLOCKED rows (so the resume loop does not re-drive blocked
        # tokens), which excludes exactly the tokens this restore needs
        # offsets for.
        token_ids = [item.token_id for item in items]
        max_attempts = self._barrier_restore_reads.get_max_node_state_attempts(self._run_id, token_ids) if token_ids else {}
        attempt_offsets: dict[str, int] = {
            token_id: (max_attempts[token_id] if token_id in max_attempts else -1) + 1 for token_id in token_ids
        }

        # Per-node batch metadata for every configured aggregation node.
        agg_plans: list[_AggregationRestorePlan] = []
        committed_aggregation_plans: list[tuple[CommittedAggregationResidual, tuple[TokenWorkItem, ...]]] = []
        committed_aggregation_output_plans: list[_PreparedAggregationRoute] = []
        committed_coalesce_plans: list[tuple[CommittedCoalesceResidual, tuple[TokenWorkItem, ...]]] = []
        if self._aggregation_settings:
            members_by_batch: dict[str, list[str]] = {}
            for member in self._execution.get_all_batch_members_for_run(self._run_id):
                members_by_batch.setdefault(member.batch_id, []).append(member.token_id)
            batches_by_node: dict[str, list[Batch]] = {}
            for batch in self._execution.get_batches(self._run_id):
                batches_by_node.setdefault(str(batch.aggregation_node_id), []).append(batch)

            for node_id in sorted(self._aggregation_settings, key=str):
                node_items = agg_items_by_node[node_id] if node_id in agg_items_by_node else []
                # Reconciliation join (configured nodes against audit batches): a
                # configured node with no batches yet (batches are created
                # lazily on first row arrival) legitimately has zero rows, so
                # absence is an empty bucket, not Tier-1 corruption.
                node_batches = batches_by_node[str(node_id)] if str(node_id) in batches_by_node else []
                # COUNT(DISTINCT token_id), not raw COUNT: retry_batch COPIES
                # members into the retry batch, and handle_incomplete_batches
                # runs BEFORE this derivation on resume — a raw count would
                # double-count every member of a retried batch.
                accepted_count_total = len(
                    {
                        member_token
                        for node_batch in node_batches
                        for member_token in (members_by_batch[node_batch.batch_id] if node_batch.batch_id in members_by_batch else ())
                    }
                )
                completed_flush_count = sum(1 for node_batch in node_batches if node_batch.status is BatchStatus.COMPLETED)
                node_scalars = (
                    scalars.aggregation[str(node_id)] if str(node_id) in scalars.aggregation else AggregationNodeScalars(None, None)
                )
                # ---- ADR-030 §E.3a aggregation reconcile (elspeth-55546a6fd6) ---
                # A FAILED out-of-claim flush records terminal FAILURE/UNROUTED
                # token_outcomes for every buffered token (_handle_flush_error)
                # and THEN releases their BLOCKED scheduler rows in a SEPARATE
                # transaction (_mark_buffered_scheduler_work_terminal). A crash
                # between the two strands durable BLOCKED rows whose tokens are
                # already terminally failed: they carry NO live BUFFERED outcome,
                # so _derive_restored_batch_id below would refuse loudly and
                # brick EVERY resume attempt. Mirror the coalesce §E.3a holdless
                # path: the tokens are done — journal-release their orphaned
                # BLOCKED rows here (under this leader's coordination token) and
                # drop them from the restore set so the deriver sees only live
                # tokens. A fully-reconciled node then falls through to the
                # counter-only branch below ("flushes all FAILED" — exactly the
                # state that branch already anticipates).
                #
                # A successful transform-mode flush may have committed its
                # batch result and expanded children before complete_barrier.
                # Rebuild that continuation from the durable expansion receipt
                # first; the processor callback publishes it with the exact
                # BLOCKED membership through strict complete_barrier.
                if node_items:
                    committed_residuals = self._barrier_restore_reads.list_committed_aggregation_residuals(
                        self._run_id,
                        aggregation_node_id=str(node_id),
                        blocked_token_ids=[item.token_id for item in node_items],
                    )
                    for aggregation_residual in committed_residuals:
                        if self._complete_committed_aggregation_residual is None:
                            raise OrchestrationInvariantError(
                                "Committed aggregation residual recovery requires the processor continuation callback"
                            )
                        member_ids = frozenset(aggregation_residual.member_token_ids)
                        residual_items = tuple(item for item in node_items if item.token_id in member_ids)
                        if len(residual_items) != len(aggregation_residual.member_token_ids):
                            raise AuditIntegrityError(
                                f"Committed aggregation residual {aggregation_residual.batch_id!r} at node {node_id!r} "
                                "does not match its exact BLOCKED journal snapshot"
                            )
                        committed_aggregation_plans.append((aggregation_residual, residual_items))
                        node_items = [item for item in node_items if item.token_id not in member_ids]

                if node_items:
                    output_receipts = self._barrier_restore_reads.list_committed_aggregation_output_receipts(
                        self._run_id,
                        aggregation_node_id=str(node_id),
                        blocked_token_ids=[item.token_id for item in node_items],
                    )
                    for output_receipt in output_receipts:
                        if self._prepare_committed_aggregation_output is None or self._complete_committed_aggregation_output is None:
                            raise OrchestrationInvariantError(
                                "Committed aggregation output recovery requires prepare and completion callbacks"
                            )
                        member_ids = frozenset(output_receipt.member_token_ids)
                        residual_items = tuple(item for item in node_items if item.token_id in member_ids)
                        if len(residual_items) != len(output_receipt.member_token_ids):
                            raise AuditIntegrityError(
                                f"Committed aggregation output {output_receipt.batch_id!r} at node {node_id!r} "
                                "does not match its exact BLOCKED journal snapshot"
                            )
                        prepared_route = self._prepare_committed_aggregation_output(output_receipt, residual_items)
                        committed_aggregation_output_plans.append(prepared_route)
                        node_items = [item for item in node_items if item.token_id not in member_ids]

                # Scoped to (FAILURE, UNROUTED): this has no output receipt;
                # the already-terminal failed inputs are simply released.
                if node_items:
                    failed_terminal_ids = self._barrier_restore_reads.find_failed_unrouted_terminal_token_ids(
                        self._run_id, [item.token_id for item in node_items]
                    )
                    if failed_terminal_ids:
                        reconciled = [item for item in node_items if item.token_id in failed_terminal_ids]
                        released = self._scheduler.mark_blocked_barrier_terminal(
                            run_id=self._run_id,
                            barrier_key=str(node_id),
                            token_ids=tuple(item.token_id for item in reconciled),
                            now=now,
                            coordination_token=self._coordination_token,
                            release_context={
                                "reason": "failed_flush_crash_reconcile",
                                "released_by": self._scheduler_lease_owner,
                                "restore_reconcile": True,
                            },
                        )
                        if released != len(reconciled):
                            raise AuditIntegrityError(
                                f"Restore §E.3a aggregation reconcile: FAILED-flush release at aggregation node "
                                f"{node_id!r} (run {self._run_id!r}, resume checkpoint {restore.resume_checkpoint_id!r}) "
                                f"terminalized {released} rows; expected exactly {len(reconciled)} orphaned "
                                "terminally-failed BLOCKED row(s)."
                            )
                        logger.info(
                            "barrier journal restore: §E.3a aggregation reconcile released %d orphaned BLOCKED row(s) "
                            "with terminal FAILURE/UNROUTED outcomes at node %s (run %s)",
                            len(reconciled),
                            node_id,
                            self._run_id,
                        )
                        node_items = [item for item in node_items if item.token_id not in failed_terminal_ids]
                if node_items:
                    batch_id = self._derive_restored_batch_id(node_id, node_items, restore)
                    agg_plans.append(
                        _AggregationRestorePlan(
                            node_id=node_id,
                            items=node_items,
                            member_order=(members_by_batch[batch_id] if batch_id in members_by_batch else []),
                            batch_id=batch_id,
                            accepted_count_total=accepted_count_total,
                            completed_flush_count=completed_flush_count,
                            scalars=node_scalars,
                        )
                    )
                elif completed_flush_count > 0 or accepted_count_total > 0 or str(node_id) in scalars.aggregation:
                    # Counter-only node: nothing buffered, but the audit trail
                    # shows prior activity — completed flushes, accepted rows
                    # (a node whose flushes all FAILED has accepted > 0 with
                    # zero COMPLETED batches and an empty buffer), or a stale
                    # scalars entry. Restore the derived counters so post-flush
                    # pagination metadata survives the resume.
                    agg_plans.append(
                        _AggregationRestorePlan(
                            node_id=node_id,
                            items=[],
                            member_order=[],
                            batch_id=None,
                            accepted_count_total=accepted_count_total,
                            completed_flush_count=completed_flush_count,
                            scalars=node_scalars,
                        )
                    )

        # Coalesce hold state ids: the OPEN node_state written at accept()
        # time at the coalesce node (the executor calls it the PENDING hold).
        coalesce_state_ids: Mapping[str, str] = {}
        if coalesce_items:
            coalesce_state_ids = self._barrier_restore_reads.get_open_node_state_ids(
                self._run_id,
                node_ids=[str(node_id) for node_id in self._coalesce_node_ids.values()],
                token_ids=[item.token_id for item in coalesce_items],
            )

        row_union_state_ids: Mapping[str, str] = {}
        row_union_holdless_items: list[TokenWorkItem] = []
        row_union_released_groups: list[list[TokenWorkItem]] = []
        if row_union_items:
            # F-1 (elspeth-14660ce1c0): the holdless-reconcile below groups by
            # fork_group_id, not row_id — two sibling fork groups can share a
            # row_id at one row_union node (spec §5, arch-M1, ruled authorable
            # ahead of this fix), and a row-keyed grouping would let a
            # released sibling group misclassify a still-pending sibling's
            # holdless item. TokenWorkItem carries lineage_path directly (no
            # derived accessor the way TokenInfo does), so this dict is
            # computed once and every "group identity" comparison below reads
            # from it — never `item.row_id` (that stays reserved for
            # `scope_row_id`, a genuinely different, row-scoped concept).
            fork_group_id_by_token_id: dict[str, str] = {}
            for item in row_union_items:
                group_id = path_fork_group_id(item.lineage_path)
                if group_id is None:
                    raise AuditIntegrityError(
                        f"Restore reconcile: row_union journal item for token {item.token_id!r} at "
                        f"{item.barrier_key!r} (run {self._run_id!r}) has no innermost FORK frame — "
                        "only forked branch tokens block at a row_union barrier; journal corruption."
                    )
                fork_group_id_by_token_id[item.token_id] = group_id

            row_union_state_ids = self._barrier_restore_reads.get_open_node_state_ids(
                self._run_id,
                node_ids=[str(node_id) for node_id in self._row_union_node_ids.values()],
                token_ids=[item.token_id for item in row_union_items],
            )
            holdless = [item for item in row_union_items if item.token_id not in row_union_state_ids]
            if holdless:
                node_id_to_row_union_name = {str(node_id): str(name) for name, node_id in self._row_union_node_ids.items()}
                # Released-only read: a group failed closed by _fail_pending
                # (timeout / EOF-incomplete / branch loss) has FAILED node
                # states with completed_at set, but it never released —
                # reconcile_released_group would refuse it and wedge the
                # resume. Failed-closure holdless rows fall through to the
                # intake-pending reset below and fail closed as late arrivals
                # on re-accept.
                released_pairs = self._barrier_restore_reads.get_released_group_ids_for_nodes(
                    self._run_id,
                    frozenset(node_id_to_row_union_name),
                )
                released_group_keys = {
                    (node_id_to_row_union_name[node_id], group_id)
                    for node_id, group_id in released_pairs
                    if node_id in node_id_to_row_union_name
                }
                released_candidates = [
                    item for item in holdless if (str(item.barrier_key), fork_group_id_by_token_id[item.token_id]) in released_group_keys
                ]
                # Token-scoped membership (§E.3a residuals): release evidence
                # proves the KEY released, not that THIS token was in the
                # released group. A surplus branch token that failed late
                # AFTER the release shares the row id, but its own closure at
                # the union node is FAILED — grouping it here would hand
                # reconcile_released_group an incomplete branch set and wedge
                # the resume. Only tokens the release itself completed
                # (status-COMPLETED state at the union node) reconstruct the
                # group; everything else under a released key is a stranded
                # late-arrival residual. Scoped per candidate's OWN barrier
                # node: released tokens keep their token ids downstream, so a
                # residual at a later union in a chained-union pipeline still
                # holds a COMPLETED state at the earlier union and must not
                # borrow membership from it.
                member_token_ids: set[str] = set()
                candidates_by_node: dict[str, list[TokenWorkItem]] = {}
                for item in released_candidates:
                    node_id_str = str(self._row_union_node_ids[RowUnionName(str(item.barrier_key))])
                    candidates_by_node.setdefault(node_id_str, []).append(item)
                for node_id_str in sorted(candidates_by_node):
                    member_token_ids.update(
                        self._barrier_restore_reads.find_released_node_state_token_ids(
                            self._run_id,
                            node_ids=[node_id_str],
                            token_ids=[item.token_id for item in candidates_by_node[node_id_str]],
                        )
                    )
                residuals = [item for item in released_candidates if item.token_id not in member_token_ids]
                residual_token_ids = {item.token_id for item in residuals}
                residual_terminal_ids: frozenset[str] = frozenset()
                if residuals:
                    residual_terminal_ids = self._barrier_restore_reads.find_failed_unrouted_terminal_token_ids(
                        self._run_id, [item.token_id for item in residuals]
                    )
                for item in residuals:
                    if item.token_id not in residual_terminal_ids:
                        continue
                    # The residual's audit trail is already terminal
                    # (_fail_late_arrival committed the FAILED state and the
                    # FAILURE/UNROUTED outcome); only its BLOCKED journal row
                    # survived the crash. Journal-release it under this
                    # leader's coordination token, mirroring the live arm.
                    released = self._scheduler.mark_blocked_barrier_terminal(
                        run_id=self._run_id,
                        barrier_key=str(item.barrier_key),
                        token_ids=(item.token_id,),
                        now=now,
                        coordination_token=self._coordination_token,
                        release_context={
                            "late_arrival": True,
                            "reason": "late_arrival_after_release",
                            "released_by": self._scheduler_lease_owner,
                            "scope_row_id": item.row_id,
                            "restore_reconcile": True,
                        },
                    )
                    if released != 1:
                        raise AuditIntegrityError(
                            f"Restore §E.3a row_union reconcile: late-arrival release for token {item.token_id!r} "
                            f"at row_union {item.barrier_key!r} (run {self._run_id!r}) terminalized "
                            f"{released} rows; expected exactly one."
                        )
                    logger.info(
                        "barrier journal restore: §E.3a reconcile released late-arrival residual token %s at row_union %s/%s (run %s)",
                        item.token_id,
                        item.barrier_key,
                        item.row_id,
                        self._run_id,
                    )
                member_keys = {
                    (str(item.barrier_key), fork_group_id_by_token_id[item.token_id])
                    for item in released_candidates
                    if item.token_id in member_token_ids
                }
                for released_key in sorted(member_keys):
                    row_union_released_groups.append(
                        [
                            item
                            for item in row_union_items
                            if (str(item.barrier_key), fork_group_id_by_token_id[item.token_id]) == released_key
                            and item.token_id not in residual_token_ids
                        ]
                    )
                # A residual without a recorded terminal outcome (crash before
                # record_token_outcome, or adoption committed but accept()
                # never ran) has an incomplete audit trail: reset it to
                # intake-pending with the other holdless non-members so the
                # live late-arrival arm replays state + outcome + release on
                # re-accept.
                row_union_holdless_items = [
                    item
                    for item in holdless
                    if (str(item.barrier_key), fork_group_id_by_token_id[item.token_id]) not in released_group_keys
                    or (item.token_id in residual_token_ids and item.token_id not in residual_terminal_ids)
                ]
                row_union_items = [
                    item
                    for item in row_union_items
                    if item.token_id in row_union_state_ids
                    and (str(item.barrier_key), fork_group_id_by_token_id[item.token_id]) not in member_keys
                ]

            if row_union_holdless_items:
                # ---- ADR-030 §E.3a row_union failed-closure reconcile -------
                # (elspeth-e18928f7cb) A holdless row whose token already
                # carries a terminal (FAILURE, UNROUTED) outcome is the crash
                # suffix of a committed group failure: _fail_pending wrote the
                # FAILED state and the terminal outcome, and the process died
                # before mark_blocked_barrier_terminal released the BLOCKED
                # journal row. Resetting it to intake-pending would re-drive
                # accept() at the closed key, whose late-arrival arm records a
                # SECOND terminal outcome — an ix_token_outcomes_terminal_unique
                # IntegrityError on every subsequent resume attempt. Journal-
                # release these rows here (aggregation §E.3a mirror); only rows
                # with no recorded outcome fall through to the intake reset.
                terminal_ids = self._barrier_restore_reads.find_failed_unrouted_terminal_token_ids(
                    self._run_id, [item.token_id for item in row_union_holdless_items]
                )
                if terminal_ids:
                    for item in [i for i in row_union_holdless_items if i.token_id in terminal_ids]:
                        released = self._scheduler.mark_blocked_barrier_terminal(
                            run_id=self._run_id,
                            barrier_key=str(item.barrier_key),
                            token_ids=(item.token_id,),
                            now=now,
                            coordination_token=self._coordination_token,
                            release_context={
                                "reason": "row_union_failed_closure_crash_reconcile",
                                "released_by": self._scheduler_lease_owner,
                                "scope_row_id": item.row_id,
                                "restore_reconcile": True,
                            },
                        )
                        if released != 1:
                            raise AuditIntegrityError(
                                f"Restore §E.3a row_union reconcile: failed-closure release for token "
                                f"{item.token_id!r} at row_union {item.barrier_key!r} (run {self._run_id!r}) "
                                f"terminalized {released} rows; expected exactly one."
                            )
                        logger.info(
                            "barrier journal restore: §E.3a reconcile released terminally-failed holdless "
                            "row_union token %s at %s/%s (run %s)",
                            item.token_id,
                            item.barrier_key,
                            item.row_id,
                            self._run_id,
                        )
                    row_union_holdless_items = [i for i in row_union_holdless_items if i.token_id not in terminal_ids]

        # ---- ADR-030 §E.3a/§E.4 crash-window reconcile (findings 1 & 3) -----
        # Adopted coalesce rows with no OPEN state_id are in a crash window:
        # the adoption CAS committed (barrier_adopted_epoch non-NULL) but
        # accept() never wrote the PENDING hold node_state (the leader died
        # between steps 1 and 2 of the coalesce intake adoption).
        #
        # Two sub-cases, identified by whether the group's (coalesce_name,
        # fork_group_id) key is Landscape-completed via
        # get_completed_group_ids_for_nodes — re-keyed (elspeth-14660ce1c0,
        # F-1): the a195a3512 checkpoint report flagged this classification
        # as still row_id-keyed and exposed to arch-M1 (two sibling fork
        # groups sharing one row_id, a completed sibling misclassifying a
        # still-pending sibling's holdless item). Authorability of that
        # shared-row_id sibling-group shape was adjudicated (comment on the
        # ticket) as YES — real, builder-validated topology, not a
        # theoretical concern — so the ruling is re-key, not pin-as-safe.
        #
        # a. Key completed (late-arrival crash §E.3a): the group already
        #    resolved; journal-release the row here at restore exactly like the
        #    live §E.3a path (mark_blocked_barrier_terminal with late_arrival
        #    context), using the new leader's coordination token.
        #
        # b. Key NOT completed (normal adoption crash §E.3): accept() never ran
        #    — re-run it after restore_from_journal populates executor state,
        #    writing the missing hold node_state under a fresh attempt offset.
        #    The row was already adopted, so the intake IS-NULL filter won't
        #    pick it up; this post-restore accept is the only recovery path.
        #
        # This replaces the old hard-refusal in restore_from_journal's
        # state_ids check, which fired on both reachable crash states.
        coalesce_holdless_items: list[TokenWorkItem] = []
        if coalesce_items:
            recovered_coalesce_member_ids: set[str] = set()
            for coalesce_name, coalesce_node_id in self._coalesce_node_ids.items():
                node_items = [item for item in coalesce_items if item.barrier_key == str(coalesce_name)]
                # elspeth-8655045f98 (arch-M1 site #4): group identity, not
                # row_id — two sibling fork groups can share row_id and each
                # commit their own completed coalesce_effects row at this
                # node; a row_id-keyed loop lumps their BLOCKED items
                # together and asks get_committed_coalesce_residual a
                # row-scoped question the durable ledger can no longer
                # answer with one row. Same derivation shape as the
                # holdless-reconcile classification below.
                coalesce_residual_group_ids_by_token_id: dict[str, str] = {}
                for item in node_items:
                    group_id = path_fork_group_id(item.lineage_path)
                    if group_id is None:
                        raise AuditIntegrityError(
                            f"Restore reconcile: coalesce journal item for token {item.token_id!r} at "
                            f"{item.barrier_key!r} (run {self._run_id!r}) has no innermost FORK frame — "
                            "only forked branch tokens block at a coalesce barrier; journal corruption."
                        )
                    coalesce_residual_group_ids_by_token_id[item.token_id] = group_id
                group_ids = sorted(set(coalesce_residual_group_ids_by_token_id.values()))
                for group_id in group_ids:
                    group_items = tuple(item for item in node_items if coalesce_residual_group_ids_by_token_id[item.token_id] == group_id)
                    coalesce_residual = self._barrier_restore_reads.get_committed_coalesce_residual(
                        self._run_id,
                        coalesce_node_id=str(coalesce_node_id),
                        coalesce_name=str(coalesce_name),
                        group_id=group_id,
                        blocked_token_ids=tuple(item.token_id for item in group_items),
                    )
                    if coalesce_residual is None:
                        continue
                    if self._complete_committed_coalesce_residual is None:
                        raise OrchestrationInvariantError(
                            "Committed coalesce residual recovery requires the processor continuation callback"
                        )
                    member_ids = frozenset(coalesce_residual.member_token_ids)
                    residual_items = tuple(item for item in group_items if item.token_id in member_ids)
                    if len(residual_items) != len(member_ids):
                        raise AuditIntegrityError(
                            f"Committed coalesce residual {coalesce_residual.effect_id!r} does not match its BLOCKED journal snapshot"
                        )
                    committed_coalesce_plans.append((coalesce_residual, residual_items))
                    recovered_coalesce_member_ids.update(member_ids)
            if recovered_coalesce_member_ids:
                coalesce_items = [item for item in coalesce_items if item.token_id not in recovered_coalesce_member_ids]

            holdless = [item for item in coalesce_items if item.token_id not in coalesce_state_ids]
            if holdless:
                # Group identity for classification (F-1): derived from each
                # item's innermost FORK frame, not row_id. scope_row_id below
                # stays item.row_id deliberately — a different, row-scoped
                # concept, same split as the coalesce/row_union live paths.
                coalesce_fork_group_id_by_token_id: dict[str, str] = {}
                for item in holdless:
                    group_id = path_fork_group_id(item.lineage_path)
                    if group_id is None:
                        raise AuditIntegrityError(
                            f"Restore reconcile: coalesce journal item for token {item.token_id!r} at "
                            f"{item.barrier_key!r} (run {self._run_id!r}) has no innermost FORK frame — "
                            "only forked branch tokens block at a coalesce barrier; journal corruption."
                        )
                    coalesce_fork_group_id_by_token_id[item.token_id] = group_id
                # Resolve the Landscape completed set once for all holdless groups.
                node_id_to_coalesce_name: dict[str, str] = {str(nid): str(name) for name, nid in self._coalesce_node_ids.items()}
                completed_pairs = self._barrier_restore_reads.get_completed_group_ids_for_nodes(
                    self._run_id,
                    frozenset(node_id_to_coalesce_name.keys()),
                )
                completed_keys_set: frozenset[tuple[str, str]] = frozenset(
                    (node_id_to_coalesce_name[node_id_str], group_id)
                    for node_id_str, group_id in completed_pairs
                    if node_id_str in node_id_to_coalesce_name
                )
                for item in holdless:
                    coalesce_name_str = item.coalesce_name or item.barrier_key
                    key = (str(coalesce_name_str), coalesce_fork_group_id_by_token_id[item.token_id])
                    if key in completed_keys_set:
                        # §E.3a at restore: adopted-but-unreleased late row
                        # against a completed key — journal-release it now.
                        released = self._scheduler.mark_blocked_barrier_terminal(
                            run_id=self._run_id,
                            barrier_key=str(coalesce_name_str),
                            token_ids=(item.token_id,),
                            now=now,
                            coordination_token=self._coordination_token,
                            release_context={
                                "late_arrival": True,
                                "reason": "late_arrival_after_merge",
                                "released_by": self._scheduler_lease_owner,
                                "scope_row_id": item.row_id,
                                "restore_reconcile": True,
                            },
                        )
                        if released != 1:
                            raise AuditIntegrityError(
                                f"Restore §E.3a reconcile: late-arrival release for token {item.token_id!r} "
                                f"at coalesce {coalesce_name_str!r} (run {self._run_id!r}) terminalized "
                                f"{released} rows; expected exactly one."
                            )
                        logger.info(
                            "barrier journal restore: §E.3a reconcile released adopted-holdless late-arrival "
                            "token %s at coalesce %s/%s (run %s)",
                            item.token_id,
                            coalesce_name_str,
                            item.row_id,
                            self._run_id,
                        )
                    else:
                        # Normal adoption crash: accept() never ran — collect for
                        # post-restore accept (after restore_from_journal populates
                        # completed_keys so the executor can do late-arrival detection).
                        coalesce_holdless_items.append(item)
                # Remove ALL holdless items from the restore list; reconciled ones
                # are journal-released above, deferred ones handled post-restore.
                coalesce_items = [item for item in coalesce_items if item.token_id in coalesce_state_ids]

        # spec §6.2/§E.4: the durable group_losses ledger is the restore
        # source of truth for lost_branches across takeover — read the FULL
        # table regardless of adopted_epoch (stated requirement); the D3
        # checkpoint scalar is retained as a cross-check only (union, table
        # wins on a reason disagreement — the first durable record may
        # already have fired a must-fail policy).
        durable_group_losses = (
            self._scheduler.list_group_losses(
                run_id=self._run_id,
                closer_names=frozenset(coalesce_keys),
            )
            if self._coalesce_executor is not None
            else []
        )
        effective_coalesce_scalars: dict[tuple[str, str], CoalescePendingScalars] = dict(scalars.coalesce)
        if self._coalesce_executor is not None:
            for loss in durable_group_losses:
                if loss.closer_name in row_union_keys:
                    continue
                # WS4 Task 9 (C-2): keyed directly on loss.group_id — the
                # durable group_losses ledger already carries the group id
                # (group_losses.py:51), so no row_id translation is needed
                # here any more. This key must agree with what Task 8's
                # get_barrier_scalars() emits (fork_group_id-keyed) and what
                # Task 10's CoalesceJournalRestorer groups journal items by
                # (innermost FORK frame's group_id) — a mismatch on any one
                # of the three silently drops a checkpointed/durable loss.
                key = (loss.closer_name, loss.group_id)
                existing = effective_coalesce_scalars[key] if key in effective_coalesce_scalars else None
                lost_branches = dict(existing.lost_branches) if existing is not None else {}
                checkpoint_reason = lost_branches[loss.member_key] if loss.member_key in lost_branches else None
                if checkpoint_reason is not None and checkpoint_reason != loss.reason:
                    logger.warning(
                        "coalesce restore: checkpoint lost-branch reason %r for %s/%s/%s disagrees with the durable "
                        "loss ledger %r; the ledger wins",
                        checkpoint_reason,
                        loss.closer_name,
                        loss.group_id,
                        loss.member_key,
                        loss.reason,
                    )
                lost_branches[loss.member_key] = loss.reason
                effective_coalesce_scalars[key] = CoalescePendingScalars(lost_branches=lost_branches)

        # ---- Mutate ---------------------------------------------------------
        # Apply committed-result continuations only after every barrier's
        # journal/audit derivation has succeeded. A corrupt later barrier must
        # not allow an earlier valid residual to publish successor work.
        for aggregation_residual, residual_items in committed_aggregation_plans:
            if self._complete_committed_aggregation_residual is None:  # pragma: no cover - checked while planning
                raise OrchestrationInvariantError("Committed aggregation residual recovery requires the processor continuation callback")
            self._complete_committed_aggregation_residual(aggregation_residual, residual_items)
        for prepared_route in committed_aggregation_output_plans:
            if self._complete_committed_aggregation_output is None:  # pragma: no cover - checked while planning
                raise OrchestrationInvariantError("Committed aggregation output recovery requires the completion callback")
            self._complete_committed_aggregation_output(prepared_route)
        for coalesce_residual, residual_items in committed_coalesce_plans:
            if self._complete_committed_coalesce_residual is None:  # pragma: no cover - checked while planning
                raise OrchestrationInvariantError("Committed coalesce residual recovery requires the processor continuation callback")
            self._complete_committed_coalesce_residual(coalesce_residual, residual_items)

        # Coalesce first: ONE call for the whole executor (a second call would
        # discard this one — see CoalesceExecutor.restore_from_journal's caller
        # obligations). Called whenever a coalesce executor exists so completed
        # keys are reconstructed from the Landscape even with nothing pending,
        # and stale lost-branch scalars are dropped-with-log.
        if self._coalesce_executor is not None:
            # ---- §E.3 crash-window recovery: reset holdless non-completed rows ---
            # Adopted BLOCKED coalesce rows with no OPEN state_id whose key is NOT
            # completed are in the crash window between adopt_blocked_barrier_item
            # commit and CoalesceExecutor.accept() — accept() never wrote the hold.
            # Resetting barrier_adopted_epoch to NULL re-classifies them as
            # intake-pending: the first drain iteration's journal-first intake adopts
            # them afresh and runs the full accept + trigger path (merge, failure,
            # late-arrival), which the restore phase cannot safely produce (accept()
            # may fire a merge whose RowResult the __init__ path cannot commit to the
            # journal).  This reset is safe because the takeover CAS has already
            # committed — the old leader cannot concurrently re-adopt these rows.
            if coalesce_holdless_items:
                reset_count = self._scheduler.reset_adoption_marker_to_pending(
                    work_item_ids=[item.work_item_id for item in coalesce_holdless_items],
                    run_id=self._run_id,
                )
                if reset_count != len(coalesce_holdless_items):
                    logger.warning(
                        "barrier journal restore: §E.3 crash-window reset expected %d rows but "
                        "reset %d (run %s); the intake will re-classify any missed rows",
                        len(coalesce_holdless_items),
                        reset_count,
                        self._run_id,
                    )
                else:
                    logger.info(
                        "barrier journal restore: §E.3 crash-window reset %d holdless-non-completed "
                        "BLOCKED coalesce row(s) to intake-pending (run %s)",
                        reset_count,
                        self._run_id,
                    )
            self._coalesce_executor.restore_from_journal(
                items=coalesce_items,
                scalars=effective_coalesce_scalars,
                state_ids=coalesce_state_ids,
                attempt_offsets=attempt_offsets,
                resume_checkpoint_id=restore.resume_checkpoint_id,
                now=now,
            )

        if self._row_union_executor is not None:
            from elspeth.engine.row_union_executor import RowUnionRestoreEntry

            if row_union_holdless_items:
                reset_count = self._scheduler.reset_adoption_marker_to_pending(
                    work_item_ids=[item.work_item_id for item in row_union_holdless_items],
                    run_id=self._run_id,
                )
                if reset_count != len(row_union_holdless_items):
                    raise AuditIntegrityError(
                        f"Row_union restore reset {reset_count} adopted holdless rows; expected {len(row_union_holdless_items)}."
                    )

            now_monotonic = self._clock.monotonic()
            for group in row_union_released_groups:
                group_entries: list[RowUnionRestoreEntry] = []
                for item in group:
                    if item.barrier_blocked_at is None:
                        raise AuditIntegrityError(f"BLOCKED row_union journal row for token {item.token_id!r} has NULL barrier_blocked_at.")
                    group_entries.append(
                        RowUnionRestoreEntry(
                            token=token_from_journal_item(
                                item,
                                attempt_offset=attempt_offsets[item.token_id],
                                resume_checkpoint_id=restore.resume_checkpoint_id,
                            ),
                            row_union_name=str(item.barrier_key),
                            state_id=row_union_state_ids.get(item.token_id),
                            arrival_time=now_monotonic - max(0.0, (now - item.barrier_blocked_at).total_seconds()),
                        )
                    )
                release_outcome = self._row_union_executor.reconcile_released_group(entries=tuple(group_entries))
                self._commit_restored_row_union_outcome(release_outcome)

            restore_entries: list[RowUnionRestoreEntry] = []
            for item in row_union_items:
                if item.barrier_blocked_at is None:
                    raise AuditIntegrityError(f"BLOCKED row_union journal row for token {item.token_id!r} has NULL barrier_blocked_at.")
                restore_entries.append(
                    RowUnionRestoreEntry(
                        token=token_from_journal_item(
                            item,
                            attempt_offset=attempt_offsets[item.token_id],
                            resume_checkpoint_id=restore.resume_checkpoint_id,
                        ),
                        row_union_name=str(item.barrier_key),
                        state_id=row_union_state_ids[item.token_id],
                        arrival_time=now_monotonic - max(0.0, (now - item.barrier_blocked_at).total_seconds()),
                    )
                )
            outcomes = self._row_union_executor.restore_from_journal(entries=restore_entries)
            for outcome in outcomes:
                self._commit_restored_row_union_outcome(outcome)

        if collector_items and self._collector_executor is not None:
            # Collector holds: every arrival journals an OPEN node_state at
            # the collector node (canon item 11); a token with none is a
            # post-closure residual the executor's restorer drops itself.
            # The executor rebuilds each group's losses from the FULL ledger
            # itself (WS4 fix-round #2) and parks any roster that is already
            # complete; BarrierIntakeCoordinator._flush_restored_collector_groups
            # closes those on the first intake pass after this restore
            # (META-31). The crash-between-collect_tokens-and-
            # complete_barrier residual completion (the coalesce/aggregation
            # residual seams' collector twin) is Phase 2 and is NOT wired here.
            collector_state_ids = self._barrier_restore_reads.get_open_node_state_ids(
                self._run_id,
                node_ids=[str(node_id) for node_id in self._collector_node_ids.values()],
                token_ids=[item.token_id for item in collector_items],
            )
            self._collector_executor.restore_from_journal(
                items=collector_items,
                state_ids=collector_state_ids,
                attempt_offsets=attempt_offsets,
                resume_checkpoint_id=restore.resume_checkpoint_id,
            )

        for plan in agg_plans:
            self._aggregation_executor.restore_from_journal(
                node_id=plan.node_id,
                items=plan.items,
                member_order=plan.member_order,
                batch_id=plan.batch_id,
                accepted_count_total=plan.accepted_count_total,
                completed_flush_count=plan.completed_flush_count,
                scalars=plan.scalars,
                attempt_offsets=attempt_offsets,
                resume_checkpoint_id=restore.resume_checkpoint_id,
                now=now,
            )

    def _commit_restored_row_union_outcome(self, outcome: RowUnionOutcome) -> None:
        """Commit a release or durable-loss failure reconstructed at resume."""
        if self._complete_row_union_fire is None or outcome.row_union_name is None or not outcome.consumed_tokens:
            raise OrchestrationInvariantError(
                "Row_union restore produced an outcome without a configured completion seam or consumed tokens."
            )
        row_union_name = RowUnionName(outcome.row_union_name)
        consumed_tokens = tuple(outcome.consumed_tokens)
        if outcome.failure_reason:
            if outcome.released_tokens or not outcome.outcomes_recorded:
                raise OrchestrationInvariantError("Row_union restore produced an invalid failure outcome.")
            self._complete_row_union_fire(
                row_union_name=row_union_name,
                consumed_tokens=consumed_tokens,
                scope_row_id=consumed_tokens[0].row_id,
            )
            if self._emit_token_completed is not None:
                for token in consumed_tokens:
                    self._emit_token_completed(
                        token,
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.UNROUTED,
                    )
            return

        if self._released_row_union_items is None or not outcome.released_tokens:
            raise OrchestrationInvariantError("Row_union restore produced an invalid release outcome.")
        released_items = self._released_row_union_items(
            row_union_name=row_union_name,
            released_tokens=outcome.released_tokens,
        )
        self._complete_row_union_fire(
            row_union_name=row_union_name,
            consumed_tokens=consumed_tokens,
            scope_row_id=outcome.released_tokens[0].row_id,
            released_items=released_items,
        )

    def _derive_restored_batch_id(
        self,
        node_id: NodeID,
        node_items: Sequence[TokenWorkItem],
        restore: BarrierJournalRestoreContext,
    ) -> str:
        """Resolve the in-progress batch id for one aggregation node's BLOCKED rows.

        Source of truth: each buffered token's BUFFERED token_outcome carries
        the batch_id it was accepted into (written by the fenced adoption
        verb since ADR-030 §E.2), read through the
        ``handle_incomplete_batches`` old->retry remap because the audit
        outcomes still reference the dead original batch when a crash
        interrupted a flush.

        Raises:
            AuditIntegrityError: If any token lacks a BUFFERED outcome with a
                batch_id, the group's tokens disagree on the (remapped)
                batch_id, the batch row is missing, or the batch belongs to a
                different aggregation node.
        """
        batch_id: str | None = None
        first_token_id: str | None = None
        for item in node_items:
            live_buffered = self._barrier_restore_reads.list_live_buffered_outcomes(TokenRef(token_id=item.token_id, run_id=self._run_id))
            if len(live_buffered) > 1:
                # ADR-030 §C.4 row 6a / §E.4: token_outcomes has no non-terminal
                # uniqueness; >1 live BUFFERED rows means a deposed leader's
                # unfenced intake wrote a second acceptance. Refuse loudly —
                # never the historical silent latest-wins.
                duplicates = "; ".join(
                    f"outcome_id={o.outcome_id!r} batch_id={o.batch_id!r} "
                    f"recorded_at={o.recorded_at.isoformat()} context={o.context_json!r}"
                    for o in live_buffered
                )
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at aggregation node {node_id!r} "
                    f"(run {self._run_id!r}, resume checkpoint {restore.resume_checkpoint_id!r}) has "
                    f"{len(live_buffered)} live BUFFERED token_outcomes — duplicate acceptances; a deposed "
                    "leader's unfenced intake wrote a second acceptance; refusing silent latest-wins. "
                    f"{duplicates}"
                )
            outcome = live_buffered[0] if live_buffered else None
            if outcome is None or outcome.batch_id is None:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at aggregation node {node_id!r} "
                    f"(run {self._run_id!r}, resume checkpoint {restore.resume_checkpoint_id!r}) has no "
                    f"matching BUFFERED token_outcome with a batch_id (got {outcome!r}) — the journal "
                    "and the audit trail disagree about this token being buffered."
                )
            resolved = restore.batch_id_remap.get(outcome.batch_id, outcome.batch_id)
            if batch_id is None:
                batch_id = resolved
                first_token_id = item.token_id
            elif resolved != batch_id:
                raise AuditIntegrityError(
                    f"BLOCKED journal rows at aggregation node {node_id!r} (run {self._run_id!r}, resume "
                    f"checkpoint {restore.resume_checkpoint_id!r}) split across batches: token "
                    f"{first_token_id!r} resolves batch_id={batch_id!r} but token {item.token_id!r} "
                    f"resolves batch_id={resolved!r}. One node has exactly one in-progress batch."
                )
        if batch_id is None:  # pragma: no cover - callers pass non-empty node_items
            raise AuditIntegrityError(f"_derive_restored_batch_id called with no journal rows for node {node_id!r}.")
        batch = self._execution.get_batch(batch_id)
        if batch is None:
            raise AuditIntegrityError(
                f"Restored batch_id {batch_id!r} for aggregation node {node_id!r} (run {self._run_id!r}) "
                "has no batches row — audit data corruption."
            )
        if str(batch.aggregation_node_id) != str(node_id):
            raise AuditIntegrityError(
                f"Restored batch_id {batch_id!r} belongs to aggregation node "
                f"{batch.aggregation_node_id!r}, but the journal BLOCKED rows carry barrier_key "
                f"{str(node_id)!r} (run {self._run_id!r}) — journal/audit disagreement."
            )
        return batch_id
