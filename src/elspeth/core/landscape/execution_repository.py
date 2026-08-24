"""Execution recording repository — compatibility facade.

The behaviour lives in the cohesive components under
:mod:`elspeth.core.landscape.execution` (filigree elspeth-c227effc89):
node states and routing events (:class:`NodeStateRepository`), the external
call audit trail with thread-safe call index allocation
(:class:`CallAuditRepository`), source/sink operation lifecycle
(:class:`OperationRepository`), aggregation batches
(:class:`BatchRepository`), and sink artifacts
(:class:`ArtifactRepository`). :class:`ExecutionRepository` composes them
behind the historical surface so call sites can migrate incrementally —
new code should prefer the component attributes (``.node_states``,
``.calls``, ``.operations``, ``.batches``, ``.artifacts``) over the flat
delegators.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, overload

from sqlalchemy import exists, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts import (
    AggregationMemberAction,
    AggregationResultMember,
    AggregationResultReceipt,
    Artifact,
    ArtifactPublicationEvidenceKind,
    Batch,
    BatchMember,
    BatchStatus,
    Call,
    CallStatus,
    CallType,
    CoalesceFailureReason,
    FrameKind,
    NodeState,
    NodeStateCompleted,
    NodeStateFailed,
    NodeStateOpen,
    NodeStatePending,
    NodeStateStatus,
    Operation,
    OperationType,
    OutputMode,
    RoutingEvent,
    RoutingMode,
    RoutingReason,
    RoutingSpec,
    RowUnionFailureReason,
    TerminalPath,
    TriggerType,
)
from elspeth.contracts.call_data import CallPayload
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.errors import AuditIntegrityError, ExecutionError, TransformErrorReason
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.checkpoint.serialization import checkpoint_dumps
from elspeth.core.landscape._database_ops import DatabaseOps
from elspeth.core.landscape._helpers import now
from elspeth.core.landscape.batch_lineage import batch_retry_lineage_ids
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapePostCommitError, LandscapeRecordError
from elspeth.core.landscape.execution import (
    ArtifactRepository,
    BatchRepository,
    CallAuditRepository,
    NodeStateRepository,
    OperationRepository,
    SinkEffectRepository,
    SourceCompletionReconciler,
)
from elspeth.core.landscape.model_loaders import (
    ArtifactLoader,
    BatchLoader,
    BatchMemberLoader,
    CallLoader,
    NodeStateLoader,
    OperationLoader,
    RoutingEventLoader,
    SinkEffectLoader,
    SinkEffectMemberLoader,
    SinkEffectStreamLoader,
)
from elspeth.core.landscape.row_data import CallDataResult
from elspeth.core.landscape.schema import (
    aggregation_result_members_table,
    aggregation_result_outputs_table,
    aggregation_results_table,
    batch_members_table,
    batches_table,
    group_records_table,
    node_states_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)

if TYPE_CHECKING:
    from elspeth.contracts.errors import TransformSuccessReason
    from elspeth.contracts.node_state_context import NodeStateContext
    from elspeth.contracts.payload_store import PayloadStore

__all__ = ["ExecutionRepository", "GroupRecord"]


@dataclass(frozen=True, slots=True)
class GroupRecord:
    """One ``group_records`` row (spec §4.3): the durable opener/roster-size
    witness minted for both FORK and EXPAND groups."""

    kind: FrameKind
    opener_token_id: str
    member_count: int


class ExecutionRepository:
    """Node state recording, external call tracking, and batch management.

    Compatibility facade over the execution components: every historical
    verb delegates to exactly one component. The components share the same
    :class:`LandscapeDB` and :class:`DatabaseOps` instances, so test seams
    that patch ``repo._db`` / ``repo._ops`` attributes remain effective.
    """

    def __init__(
        self,
        db: LandscapeDB,
        ops: DatabaseOps,
        *,
        node_state_loader: NodeStateLoader,
        routing_event_loader: RoutingEventLoader,
        call_loader: CallLoader,
        operation_loader: OperationLoader,
        batch_loader: BatchLoader,
        batch_member_loader: BatchMemberLoader,
        artifact_loader: ArtifactLoader,
        sink_effect_loader: SinkEffectLoader,
        sink_effect_member_loader: SinkEffectMemberLoader,
        sink_effect_stream_loader: SinkEffectStreamLoader,
        payload_store: PayloadStore | None = None,
    ) -> None:
        self._db = db
        self._ops = ops
        self._payload_store = payload_store
        self.node_states = NodeStateRepository(
            db,
            ops,
            node_state_loader=node_state_loader,
            routing_event_loader=routing_event_loader,
            payload_store=payload_store,
        )
        self.source_completion_recovery = SourceCompletionReconciler(db, node_states=self.node_states)
        self.calls = CallAuditRepository(db, ops, call_loader=call_loader, payload_store=payload_store)
        self.operations = OperationRepository(db, ops, operation_loader=operation_loader, payload_store=payload_store)
        self.batches = BatchRepository(db, ops, batch_loader=batch_loader, batch_member_loader=batch_member_loader)
        self.artifacts = ArtifactRepository(ops, artifact_loader=artifact_loader)
        self.sink_effects = SinkEffectRepository(
            db,
            ops,
            effect_loader=sink_effect_loader,
            member_loader=sink_effect_member_loader,
            stream_loader=sink_effect_stream_loader,
        )

    # ── Node state recording (NodeStateRepository) ─────────────────────

    def begin_node_state(
        self,
        token_id: str,
        node_id: str,
        run_id: str,
        step_index: int,
        input_data: Mapping[str, object],
        *,
        state_id: str | None = None,
        attempt: int = 0,
        quarantined: bool = False,
        resume_checkpoint_id: str | None = None,
    ) -> NodeStateOpen:
        """Begin recording a node state (token visiting a node)."""
        return self.node_states.begin_node_state(
            token_id,
            node_id,
            run_id,
            step_index,
            input_data,
            state_id=state_id,
            attempt=attempt,
            quarantined=quarantined,
            resume_checkpoint_id=resume_checkpoint_id,
        )

    def record_completed_node_state(
        self,
        token_id: str,
        node_id: str,
        run_id: str,
        step_index: int,
        input_data: Mapping[str, object],
        output_data: Mapping[str, object] | list[Mapping[str, object]],
        duration_ms: float,
        *,
        state_id: str | None = None,
        attempt: int = 0,
        quarantined: bool = False,
        success_reason: TransformSuccessReason | None = None,
        context_after: NodeStateContext | None = None,
    ) -> NodeStateCompleted:
        """Insert an immediately completed node state in one audit transaction."""
        return self.node_states.record_completed_node_state(
            token_id,
            node_id,
            run_id,
            step_index,
            input_data,
            output_data,
            duration_ms,
            state_id=state_id,
            attempt=attempt,
            quarantined=quarantined,
            success_reason=success_reason,
            context_after=context_after,
        )

    def record_completed_node_state_on(
        self,
        conn: Connection,
        token_id: str,
        node_id: str,
        run_id: str,
        step_index: int,
        input_data: Mapping[str, object],
        output_data: Mapping[str, object] | list[Mapping[str, object]],
        duration_ms: float,
        *,
        state_id: str | None = None,
        attempt: int = 0,
        quarantined: bool = False,
        success_reason: TransformSuccessReason | None = None,
        context_after: NodeStateContext | None = None,
    ) -> NodeStateCompleted:
        """Insert an immediately completed node state on a caller-owned transaction."""
        return self.node_states.record_completed_node_state_on(
            conn,
            token_id,
            node_id,
            run_id,
            step_index,
            input_data,
            output_data,
            duration_ms,
            state_id=state_id,
            attempt=attempt,
            quarantined=quarantined,
            success_reason=success_reason,
            context_after=context_after,
        )

    def reconcile_source_completions_from_scheduler(
        self,
        *,
        run_id: str,
        coordination_token: CoordinationToken,
        at: datetime,
    ) -> int:
        """Repair fully witnessed pre-fix TS-02 source-completion gaps."""
        return self.source_completion_recovery.reconcile(
            run_id=run_id,
            coordination_token=coordination_token,
            at=at,
        )

    def begin_node_states_many(
        self,
        entries: Sequence[tuple[str, str, str, int, Mapping[str, object]]],
    ) -> list[NodeStateOpen]:
        """Begin many node states in one audit transaction."""
        return self.node_states.begin_node_states_many(entries)

    @overload
    def complete_node_state(
        self,
        state_id: str,
        status: Literal[NodeStateStatus.PENDING],
        *,
        output_data: Mapping[str, object] | list[Mapping[str, object]] | None = None,
        duration_ms: float | None = None,
        error: ExecutionError | TransformErrorReason | CoalesceFailureReason | RowUnionFailureReason | None = None,
        context_after: NodeStateContext | None = None,
    ) -> NodeStatePending: ...

    @overload
    def complete_node_state(
        self,
        state_id: str,
        status: Literal[NodeStateStatus.COMPLETED],
        *,
        output_data: Mapping[str, object] | list[Mapping[str, object]] | None = None,
        duration_ms: float | None = None,
        error: ExecutionError | TransformErrorReason | CoalesceFailureReason | RowUnionFailureReason | None = None,
        success_reason: TransformSuccessReason | None = None,
        context_after: NodeStateContext | None = None,
    ) -> NodeStateCompleted: ...

    @overload
    def complete_node_state(
        self,
        state_id: str,
        status: Literal[NodeStateStatus.FAILED],
        *,
        output_data: Mapping[str, object] | list[Mapping[str, object]] | None = None,
        duration_ms: float | None = None,
        error: ExecutionError | TransformErrorReason | CoalesceFailureReason | RowUnionFailureReason | None = None,
        context_after: NodeStateContext | None = None,
    ) -> NodeStateFailed: ...

    def complete_node_state(
        self,
        state_id: str,
        status: NodeStateStatus,
        *,
        output_data: Mapping[str, object] | list[Mapping[str, object]] | None = None,
        duration_ms: float | None = None,
        error: ExecutionError | TransformErrorReason | CoalesceFailureReason | RowUnionFailureReason | None = None,
        success_reason: TransformSuccessReason | None = None,
        context_after: NodeStateContext | None = None,
    ) -> NodeStatePending | NodeStateCompleted | NodeStateFailed:
        """Complete a node state (PENDING, COMPLETED, or FAILED)."""
        return self.node_states.complete_node_state(
            state_id,
            status,
            output_data=output_data,
            duration_ms=duration_ms,
            error=error,
            success_reason=success_reason,
            context_after=context_after,
        )

    def complete_node_states_completed_many(
        self,
        completions: Sequence[tuple[str, Mapping[str, object], float]],
        *,
        conn: Connection | None = None,
    ) -> None:
        """Complete many node states as COMPLETED in one audit transaction."""
        return self.node_states.complete_node_states_completed_many(completions, conn=conn)

    def get_node_state(self, state_id: str) -> NodeState | None:
        """Get a node state by ID."""
        return self.node_states.get_node_state(state_id)

    def get_max_node_state_attempts(self, run_id: str, token_ids: Sequence[str], *, step_index: int | None = None) -> dict[str, int]:
        """Max ``node_states.attempt`` per token (F1 resume attempt-offset derivation)."""
        return self.node_states.get_max_node_state_attempts(run_id, token_ids, step_index=step_index)

    def get_open_node_state_ids(
        self,
        run_id: str,
        *,
        node_ids: Sequence[str],
        token_ids: Sequence[str],
    ) -> dict[str, str]:
        """Outstanding (OPEN) node_state hold ids per token at the given nodes."""
        return self.node_states.get_open_node_state_ids(run_id, node_ids=node_ids, token_ids=token_ids)

    def get_completed_row_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Get (node_id, row_id) pairs where a node_state has been completed."""
        return self.node_states.get_completed_row_ids_for_nodes(run_id, node_ids)

    def has_completed_row_for_node(self, *, run_id: str, node_id: str, row_id: str) -> bool:
        """Return whether one row completed at one node in one run."""
        return self.node_states.has_completed_row_for_node(run_id=run_id, node_id=node_id, row_id=row_id)

    def get_released_row_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Get (node_id, row_id) pairs where a node_state completed as COMPLETED."""
        return self.node_states.get_released_row_ids_for_nodes(run_id, node_ids)

    def has_released_row_for_node(self, *, run_id: str, node_id: str, row_id: str) -> bool:
        """Return whether one row completed as COMPLETED at one node in one run."""
        return self.node_states.has_released_row_for_node(run_id=run_id, node_id=node_id, row_id=row_id)

    def row_id_for_token(self, *, run_id: str, token_id: str) -> str | None:
        """Return the durable row_id for one token, or None if it never minted.

        Transitional resolution (spec §5/§6.2): mirrors
        :meth:`BarrierRestoreReadModel.row_id_for_token` for callers that
        construct the barrier-intake/recovery coordinators with this
        compatibility facade instead of the narrower read model.
        """
        query = select(tokens_table.c.row_id).where(
            tokens_table.c.token_id == token_id,
            tokens_table.c.run_id == run_id,
        )
        row = self._ops.execute_fetchone(query)
        return None if row is None else str(row.row_id)

    def resolve_group_member_token(self, *, run_id: str, kind: FrameKind, group_id: str, member_key: str) -> str:
        """Resolve the LIVE token at one frame — the token that currently
        carries this group member (WS3 Task 6 fix round 2, Ruling 42; final
        review F1).

        The honest identity for an ESCALATED group loss is the token that
        WAS the lost outer member — not whichever inner sibling's failure
        happened to notice the loss first. Candidates are the tokens whose
        OWN lineage path terminates at this frame: a descendant that forked
        again carries this SAME frame too, but at a depth strictly less than
        its own deepest frame, and the ``NOT EXISTS`` below excludes it.

        More than one token can legitimately terminate at a frame: every
        successful inner closer MINTS a successor whose lineage is the
        popped remaining path (`coalesce_tokens` / `collect_tokens` in
        data_flow/tokens.py), so after an inner merge inside branch ``b``
        both the original branch token and the merged token terminate at
        ``b``'s frame — and a sequential fork→merge→fork→merge chain in one
        branch stacks a successor per merge. The member is then carried by
        the LATEST successor, and that succession is a structural fact the
        closer itself recorded: the merged token's `token_parents` rows lead
        back through the consumed members to the token it superseded. A
        candidate that is an ANCESTOR of another candidate has therefore
        been consumed at this frame and is dropped; exactly one candidate
        must remain. Two candidates with no ancestry between them are
        genuinely ambiguous and still fail closed. Reads
        `token_lineage_frames` via `ix_token_lineage_frames_group` plus a
        bounded upward `token_parents` walk; mints no new schema.

        Raises `AuditIntegrityError` if zero or more than one live token
        remains — both are lineage corruption for a frame
        `_first_bound_frame` resolved against the group-binding registry,
        which requires the frame to have actually been minted.
        """
        deeper = token_lineage_frames_table.alias("deeper")
        outer = token_lineage_frames_table.alias("outer")
        query = select(outer.c.token_id).where(
            outer.c.run_id == run_id,
            outer.c.kind == kind.value,
            outer.c.group_id == group_id,
            outer.c.member_key == member_key,
            ~exists(
                select(1).where(
                    deeper.c.token_id == outer.c.token_id,
                    deeper.c.run_id == outer.c.run_id,
                    deeper.c.depth > outer.c.depth,
                )
            ),
        )
        terminating = sorted(str(row.token_id) for row in self._ops.execute_fetchall(query))
        superseded = self._ancestors_among(run_id=run_id, token_ids=terminating) if len(terminating) > 1 else set()
        live = [token_id for token_id in terminating if token_id not in superseded]
        if len(live) != 1:
            raise AuditIntegrityError(
                f"resolve_group_member_token: expected exactly one live token whose own lineage path "
                f"terminates at group_id={group_id!r} member_key={member_key!r} (run_id={run_id!r}), "
                f"found {len(live)}: {live!r} (terminating={terminating!r}, superseded={sorted(superseded)!r})"
            )
        return live[0]

    def _ancestors_among(self, *, run_id: str, token_ids: Sequence[str]) -> set[str]:
        """The subset of ``token_ids`` that is an ANCESTOR (via
        `token_parents`, transitively) of another member of ``token_ids``.
        One upward walk from the whole set; every hop is a fork or a closer
        mint, so the walk is bounded by lineage depth."""
        candidates = set(token_ids)
        visited: set[str] = set()
        ancestors: set[str] = set()
        frontier = set(candidates)
        while frontier:
            visited |= frontier
            query = select(token_parents_table.c.parent_token_id).where(
                token_parents_table.c.run_id == run_id,
                token_parents_table.c.token_id.in_(sorted(frontier)),
            )
            parents = {str(row.parent_token_id) for row in self._ops.execute_fetchall(query)}
            ancestors |= parents
            frontier = parents - visited
        return candidates & ancestors

    def get_group_record(self, *, run_id: str, group_id: str) -> GroupRecord | None:
        """Read one ``group_records`` row (spec §4.3: mints for BOTH FORK and
        EXPAND openers). ``None`` means the ledger references a group the
        audit trail never minted."""
        query = select(
            group_records_table.c.kind,
            group_records_table.c.opener_token_id,
            group_records_table.c.member_count,
        ).where(
            group_records_table.c.run_id == run_id,
            group_records_table.c.group_id == group_id,
        )
        row = self._ops.execute_fetchone(query)
        if row is None:
            return None
        return GroupRecord(kind=FrameKind(row.kind), opener_token_id=str(row.opener_token_id), member_count=int(row.member_count))

    def any_member_token_for_group(self, *, run_id: str, group_id: str) -> str | None:
        """An arbitrary token whose lineage passes through ``group_id``
        (escalation's enclosing-frame walk, spec §6.3): bound-region SESE
        nesting guarantees every member of a group shares the same
        enclosing prefix, so ONE witness token is sufficient — ``None``
        means the group minted no frames at all."""
        query = (
            select(token_lineage_frames_table.c.token_id)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.group_id == group_id,
            )
            .limit(1)
        )
        row = self._ops.execute_fetchone(query)
        return None if row is None else str(row.token_id)

    def member_keys_for_group(self, *, run_id: str, group_id: str) -> tuple[str, ...]:
        """DISTINCT ``member_key`` values minted for one group (EXPAND roster
        derivation, spec §5): the runtime-minted roster, unlike a FORK
        binding's static ``member_roster``. Sorted for deterministic
        iteration; callers cross-check the count against ``group_records.
        member_count``."""
        query = (
            select(token_lineage_frames_table.c.member_key)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.group_id == group_id,
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return tuple(sorted({str(row.member_key) for row in rows}))

    def record_routing_event(
        self,
        state_id: str,
        edge_id: str,
        mode: RoutingMode,
        reason: RoutingReason | None = None,
        *,
        event_id: str | None = None,
        routing_group_id: str | None = None,
        ordinal: int = 0,
        reason_ref: str | None = None,
    ) -> RoutingEvent:
        """Record one complete one-route decision for a state.

        Fork/multi-destination decisions must use ``record_routing_events``
        with every route in one call. ``routing_group_id`` and ``ordinal``
        are explicit/legacy retry identities, not permission to append routes
        through sequential calls.
        """
        return self.node_states.record_routing_event(
            state_id,
            edge_id,
            mode,
            reason,
            event_id=event_id,
            routing_group_id=routing_group_id,
            ordinal=ordinal,
            reason_ref=reason_ref,
        )

    def record_routing_events(
        self,
        state_id: str,
        routes: list[RoutingSpec],
        reason: RoutingReason | None = None,
    ) -> list[RoutingEvent]:
        """Atomically record one complete fork/multi-destination decision."""
        return self.node_states.record_routing_events(state_id, routes, reason)

    # ── Call recording (CallAuditRepository) ───────────────────────────

    def allocate_call_index(self, state_id: str) -> int:
        """Allocate next call index for a state_id (thread-safe)."""
        return self.calls.allocate_call_index(state_id)

    def record_call(
        self,
        state_id: str,
        call_index: int,
        call_type: CallType,
        status: CallStatus,
        request_data: CallPayload,
        response_data: CallPayload | None = None,
        error: CallPayload | None = None,
        latency_ms: float | None = None,
        *,
        request_ref: str | None = None,
        response_ref: str | None = None,
        resolved_prompt_template_hash: str | None = None,
    ) -> Call:
        """Record an external call for a node state."""
        return self.calls.record_call(
            state_id,
            call_index,
            call_type,
            status,
            request_data,
            response_data,
            error,
            latency_ms,
            request_ref=request_ref,
            response_ref=response_ref,
            resolved_prompt_template_hash=resolved_prompt_template_hash,
        )

    # === Operations (Source/Sink I/O) ===

    def begin_operation(
        self,
        run_id: str,
        node_id: str,
        operation_type: OperationType,
        *,
        input_data: Mapping[str, object] | None = None,
        sink_effect_id: str | None = None,
    ) -> Operation:
        """Begin an operation for source/sink I/O."""
        return self.operations.begin_operation(
            run_id,
            node_id,
            operation_type,
            input_data=input_data,
            sink_effect_id=sink_effect_id,
        )

    def complete_operation(
        self,
        operation_id: str,
        status: Literal["completed", "failed"],
        *,
        output_data: Mapping[str, object] | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Complete an operation."""
        return self.operations.complete_operation(
            operation_id,
            status,
            output_data=output_data,
            error=error,
            duration_ms=duration_ms,
        )

    def allocate_operation_call_index(self, operation_id: str) -> int:
        """Allocate next call index for an operation_id (thread-safe)."""
        return self.calls.allocate_operation_call_index(operation_id)

    def record_operation_call(
        self,
        operation_id: str,
        call_type: CallType,
        status: CallStatus,
        request_data: CallPayload,
        response_data: CallPayload | None = None,
        error: CallPayload | None = None,
        latency_ms: float | None = None,
        *,
        call_index: int | None = None,
        request_ref: str | None = None,
        response_ref: str | None = None,
        resolved_prompt_template_hash: str | None = None,
    ) -> Call:
        """Record an external call made during an operation."""
        return self.calls.record_operation_call(
            operation_id,
            call_type,
            status,
            request_data,
            response_data,
            error,
            latency_ms,
            call_index=call_index,
            request_ref=request_ref,
            response_ref=response_ref,
            resolved_prompt_template_hash=resolved_prompt_template_hash,
        )

    def get_operation(self, operation_id: str) -> Operation | None:
        """Get an operation by ID."""
        return self.operations.get_operation(operation_id)

    def get_operation_calls(self, operation_id: str) -> list[Call]:
        """Get external calls for an operation."""
        return self.calls.get_operation_calls(operation_id)

    def get_operations_for_run(self, run_id: str) -> list[Operation]:
        """Get all operations for a run."""
        return self.operations.get_operations_for_run(run_id)

    def get_all_operation_calls_for_run(self, run_id: str) -> list[Call]:
        """Get all operation-parented calls for a run (batch query)."""
        return self.calls.get_all_operation_calls_for_run(run_id)

    def find_call_by_request_hash(
        self,
        run_id: str,
        call_type: CallType,
        request_hash: str,
        *,
        sequence_index: int = 0,
    ) -> Call | None:
        """Find a call by its request hash within a run (replay lookup)."""
        return self.calls.find_call_by_request_hash(run_id, call_type, request_hash, sequence_index=sequence_index)

    def get_call_response_data(self, call_id: str) -> CallDataResult:
        """Retrieve the response data for a call with explicit state."""
        return self.calls.get_call_response_data(call_id)

    # ── Batch recording (BatchRepository) ──────────────────────────────

    def create_batch(
        self,
        run_id: str,
        aggregation_node_id: str,
        *,
        batch_id: str | None = None,
        attempt: int = 0,
    ) -> Batch:
        """Create a new batch for aggregation."""
        return self.batches.create_batch(run_id, aggregation_node_id, batch_id=batch_id, attempt=attempt)

    def add_batch_member(
        self,
        batch_id: str,
        token_id: str,
        ordinal: int,
        *,
        conn: Connection | None = None,
    ) -> BatchMember:
        """Add a token to a batch."""
        return self.batches.add_batch_member(batch_id, token_id, ordinal, conn=conn)

    def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        trigger_type: TriggerType | None = None,
        trigger_reason: str | None = None,
        state_id: str | None = None,
    ) -> None:
        """Update batch status."""
        return self.batches.update_batch_status(
            batch_id,
            status,
            trigger_type=trigger_type,
            trigger_reason=trigger_reason,
            state_id=state_id,
        )

    def complete_batch(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        trigger_type: TriggerType | None = None,
        trigger_reason: str | None = None,
        state_id: str | None = None,
    ) -> Batch:
        """Complete a batch."""
        return self.batches.complete_batch(
            batch_id,
            status,
            trigger_type=trigger_type,
            trigger_reason=trigger_reason,
            state_id=state_id,
        )

    def complete_aggregation_result(
        self,
        *,
        batch_id: str,
        run_id: str,
        aggregation_node_id: str,
        state_id: str,
        trigger_type: TriggerType,
        output_mode: OutputMode,
        output_rows: Sequence[PipelineRow],
        output_shape: str,
        output_hash: str,
        members: Sequence[AggregationResultMember],
        expansion_parent_token_id: str | None,
        duration_ms: float,
        success_reason: TransformSuccessReason | None,
        context_after: NodeStateContext,
    ) -> AggregationResultReceipt:
        """Atomically complete node, batch, and durable aggregation output."""
        if type(output_mode) is not OutputMode:
            raise AuditIntegrityError("aggregation result receipt requires a nominal OutputMode")
        rows = tuple(output_rows)
        receipt_members = tuple(members)
        if not receipt_members:
            raise AuditIntegrityError("aggregation result receipt requires ordered members")
        if any(item.member_ref.run_id != run_id for item in receipt_members):
            raise AuditIntegrityError("aggregation result receipt members cross run identity")
        member_token_ids = tuple(item.member_ref.token_id for item in receipt_members)
        if len(set(member_token_ids)) != len(member_token_ids):
            raise AuditIntegrityError("aggregation result receipt contains duplicate members")
        for ordinal, item in enumerate(receipt_members):
            expected_error_hash = sha256(f"quarantined_in_batch:{batch_id}:{ordinal}".encode()).hexdigest()[:16]
            if item.action is AggregationMemberAction.QUARANTINE:
                if item.error_hash != expected_error_hash:
                    raise AuditIntegrityError("aggregation result receipt contains a divergent quarantine action")
            elif item.error_hash is not None:
                raise AuditIntegrityError("non-quarantine aggregation result member action forbids error_hash")

        if output_mode is OutputMode.TRANSFORM:
            if rows:
                if output_shape not in {"single", "multi"}:
                    raise AuditIntegrityError("non-empty transform aggregation result requires single or multi shape")
                if output_shape == "single" and len(rows) != 1:
                    raise AuditIntegrityError("single aggregation result receipt requires exactly one output row")
                if any(
                    item.action not in {AggregationMemberAction.CONSUME_BATCH, AggregationMemberAction.QUARANTINE}
                    for item in receipt_members
                ):
                    raise AuditIntegrityError("non-empty transform aggregation result has an illegal member action")
                first_consumed_token_id = next(
                    (item.member_ref.token_id for item in receipt_members if item.action is AggregationMemberAction.CONSUME_BATCH),
                    None,
                )
                if expansion_parent_token_id != first_consumed_token_id or first_consumed_token_id is None:
                    raise AuditIntegrityError("aggregation result expansion parent is not its first consumed batch member")
            else:
                if output_shape != "empty" or expansion_parent_token_id is not None:
                    raise AuditIntegrityError("empty transform aggregation result requires empty shape and no expansion parent")
                if any(
                    item.action not in {AggregationMemberAction.DROP_FILTERED, AggregationMemberAction.QUARANTINE}
                    for item in receipt_members
                ):
                    raise AuditIntegrityError("empty transform aggregation result has an illegal member action")
        elif rows:
            if output_shape != "multi" or len(rows) != len(receipt_members) or expansion_parent_token_id is not None:
                raise AuditIntegrityError(
                    "non-empty passthrough aggregation result requires multi shape, one output per member, and no expansion parent"
                )
            if any(item.action is not AggregationMemberAction.CONTINUE_PASSTHROUGH for item in receipt_members):
                raise AuditIntegrityError("non-empty passthrough aggregation result has an illegal member action")
        else:
            if output_shape != "empty" or expansion_parent_token_id is not None:
                raise AuditIntegrityError("empty passthrough aggregation result requires empty shape and no expansion parent")
            if any(item.action is not AggregationMemberAction.DROP_FILTERED for item in receipt_members):
                raise AuditIntegrityError("empty passthrough aggregation result has an illegal member action")

        output_data: Mapping[str, object] | list[Mapping[str, object]]
        if output_shape == "single":
            output_data = rows[0].to_dict()
        else:
            output_data = [row.to_dict() for row in rows]
        if stable_hash(output_data) != output_hash:
            raise AuditIntegrityError("aggregation result receipt output hash disagrees with output rows")

        if rows and self._payload_store is None:
            raise AuditIntegrityError("aggregation result rows require a payload store")
        payload_bytes = tuple(
            checkpoint_dumps({"data": row.to_dict(), "contract": row.contract.to_checkpoint_format()}).encode("utf-8") for row in rows
        )
        expected_refs = tuple(sha256(payload).hexdigest() for payload in payload_bytes)
        stored_refs = tuple(self._payload_store.store(payload) for payload in payload_bytes) if self._payload_store is not None else ()
        if stored_refs != expected_refs:
            raise AuditIntegrityError("aggregation result payload store violated its SHA-256 content-address contract")

        expected_member_rows = tuple(
            (ordinal, item.member_ref.token_id, item.action.value, item.error_hash) for ordinal, item in enumerate(receipt_members)
        )
        expected_success_reason_json = canonical_json(success_reason) if success_reason is not None else None
        expected_context_after_json = canonical_json(context_after.to_dict())

        def existing_receipt_is_exact(conn: Connection, *, state: Any, batch: Any) -> bool:
            header = conn.execute(select(aggregation_results_table).where(aggregation_results_table.c.batch_id == batch_id)).one_or_none()
            if header is None:
                return False
            exact_header = (
                header.run_id == run_id
                and header.aggregation_state_id == state_id
                and header.output_mode == output_mode.value
                and header.output_shape == output_shape
                and header.output_hash == output_hash
                and header.expansion_parent_token_id == expansion_parent_token_id
            )
            exact_completion = (
                state.status == NodeStateStatus.COMPLETED.value
                and state.output_hash == output_hash
                and state.duration_ms == duration_ms
                and state.success_reason_json == expected_success_reason_json
                and state.context_after_json == expected_context_after_json
                and batch.status == BatchStatus.COMPLETED.value
                and batch.aggregation_state_id == state_id
                and batch.trigger_type == trigger_type.value
                and batch.trigger_reason is None
            )
            output_rows = tuple(
                (int(row.ordinal), str(row.token_data_ref))
                for row in conn.execute(
                    select(aggregation_result_outputs_table)
                    .where(aggregation_result_outputs_table.c.batch_id == batch_id)
                    .order_by(aggregation_result_outputs_table.c.ordinal)
                )
            )
            member_rows = tuple(
                (int(row.ordinal), str(row.token_id), str(row.action), row.error_hash)
                for row in conn.execute(
                    select(aggregation_result_members_table)
                    .where(aggregation_result_members_table.c.batch_id == batch_id)
                    .order_by(aggregation_result_members_table.c.ordinal)
                )
            )
            if not (
                exact_header and exact_completion and output_rows == tuple(enumerate(expected_refs)) and member_rows == expected_member_rows
            ):
                raise AuditIntegrityError(f"aggregation result receipt retry diverges for batch_id={batch_id}")
            return True

        receipt = AggregationResultReceipt(
            batch_id=batch_id,
            run_id=run_id,
            aggregation_state_id=state_id,
            output_mode=output_mode.value,
            output_shape=output_shape,
            output_hash=output_hash,
            output_refs=expected_refs,
            members=receipt_members,
            expansion_parent_token_id=expansion_parent_token_id,
        )
        write_body_completed = False
        try:
            with self._db.write_connection() as conn:
                token_rows = conn.execute(
                    select(tokens_table.c.token_id, tokens_table.c.run_id)
                    .where(tokens_table.c.token_id.in_(member_token_ids))
                    .order_by(tokens_table.c.token_id)
                    .with_for_update(of=tokens_table)
                ).all()
                if {(str(row.token_id), str(row.run_id)) for row in token_rows} != {(token_id, run_id) for token_id in member_token_ids}:
                    raise AuditIntegrityError("aggregation result receipt references a missing or foreign member token")
                state = conn.execute(
                    select(
                        node_states_table.c.run_id,
                        node_states_table.c.node_id,
                        node_states_table.c.status,
                        node_states_table.c.output_hash,
                        node_states_table.c.duration_ms,
                        node_states_table.c.success_reason_json,
                        node_states_table.c.context_after_json,
                    )
                    .where(node_states_table.c.state_id == state_id)
                    .with_for_update(of=node_states_table)
                ).one_or_none()
                if state is None or state.run_id != run_id or state.node_id != aggregation_node_id:
                    raise AuditIntegrityError("aggregation result receipt references a missing, foreign, or wrong-node state")
                batch = conn.execute(
                    select(
                        batches_table.c.run_id,
                        batches_table.c.aggregation_node_id,
                        batches_table.c.status,
                        batches_table.c.aggregation_state_id,
                        batches_table.c.expansion_group_id,
                        batches_table.c.retry_of_batch_id,
                        batches_table.c.trigger_type,
                        batches_table.c.trigger_reason,
                    )
                    .where(batches_table.c.batch_id == batch_id)
                    .with_for_update(of=batches_table)
                ).one_or_none()
                if batch is None or batch.run_id != run_id or batch.aggregation_node_id != aggregation_node_id:
                    raise AuditIntegrityError("aggregation result receipt references a missing, foreign, or wrong-node batch")
                member_ids = tuple(
                    str(row.token_id)
                    for row in conn.execute(
                        select(batch_members_table.c.token_id)
                        .where(batch_members_table.c.batch_id == batch_id)
                        .where(batch_members_table.c.run_id == run_id)
                        .order_by(batch_members_table.c.ordinal)
                    )
                )
                if member_ids != member_token_ids:
                    raise AuditIntegrityError("aggregation result receipt members do not match exact ordered batch membership")
                if not existing_receipt_is_exact(conn, state=state, batch=batch):
                    if (
                        state.status != NodeStateStatus.OPEN.value
                        or batch.status != BatchStatus.EXECUTING.value
                        or batch.aggregation_state_id not in (None, state_id)
                        or batch.expansion_group_id is not None
                    ):
                        raise AuditIntegrityError("aggregation result receipt requires an OPEN state and unclaimed EXECUTING batch")
                    # The members' live BUFFERED acceptances keep the ORIGINAL
                    # batch_id across crash-retry (retry batches copy members
                    # but never rewrite immutable acceptance history), so the
                    # liveness proof binds against the durable retry lineage.
                    lineage_batch_ids = batch_retry_lineage_ids(
                        lambda query: conn.execute(query).one_or_none(),
                        batch_id=batch_id,
                        run_id=run_id,
                        aggregation_node_id=aggregation_node_id,
                        retry_of_batch_id=batch.retry_of_batch_id,
                    )
                    live_rows = conn.execute(
                        select(token_outcomes_table.c.token_id)
                        .where(token_outcomes_table.c.run_id == run_id)
                        .where(token_outcomes_table.c.token_id.in_(member_token_ids))
                        .where(token_outcomes_table.c.completed == 0)
                        .where(token_outcomes_table.c.path == TerminalPath.BUFFERED.value)
                        .where(token_outcomes_table.c.batch_id.in_(lineage_batch_ids))
                    ).all()
                    if tuple(sorted(str(row.token_id) for row in live_rows)) != tuple(sorted(member_token_ids)):
                        raise AuditIntegrityError(
                            "aggregation result receipt requires one live BUFFERED outcome for every member within the batch retry lineage"
                        )
                    terminal_rows = conn.execute(
                        select(token_outcomes_table.c.token_id)
                        .where(token_outcomes_table.c.run_id == run_id)
                        .where(token_outcomes_table.c.token_id.in_(member_token_ids))
                        .where(token_outcomes_table.c.completed == 1)
                    ).all()
                    if terminal_rows:
                        terminal_token_ids = sorted(str(row.token_id) for row in terminal_rows)
                        raise AuditIntegrityError(
                            f"aggregation result receipt members already have terminal outcomes: {terminal_token_ids!r}"
                        )

                    self.node_states.complete_node_state(
                        state_id=state_id,
                        status=NodeStateStatus.COMPLETED,
                        output_data=output_data,
                        duration_ms=duration_ms,
                        success_reason=success_reason,
                        context_after=context_after,
                        conn=conn,
                    )
                    self.batches.complete_batch(
                        batch_id=batch_id,
                        status=BatchStatus.COMPLETED,
                        trigger_type=trigger_type,
                        state_id=state_id,
                        conn=conn,
                    )
                    inserted_batch_id = conn.execute(
                        aggregation_results_table.insert()
                        .values(
                            batch_id=batch_id,
                            run_id=run_id,
                            aggregation_state_id=state_id,
                            output_mode=output_mode.value,
                            output_shape=output_shape,
                            output_hash=output_hash,
                            expansion_parent_token_id=expansion_parent_token_id,
                            created_at=now(),
                        )
                        .returning(aggregation_results_table.c.batch_id)
                    ).scalar_one()
                    if inserted_batch_id != batch_id:
                        raise AuditIntegrityError("aggregation result receipt INSERT returned the wrong batch identity")
                    for ordinal, token_data_ref in enumerate(expected_refs):
                        inserted_ordinal = conn.execute(
                            aggregation_result_outputs_table.insert()
                            .values(
                                batch_id=batch_id,
                                run_id=run_id,
                                ordinal=ordinal,
                                token_data_ref=token_data_ref,
                            )
                            .returning(aggregation_result_outputs_table.c.ordinal)
                        ).scalar_one()
                        if inserted_ordinal != ordinal:
                            raise AuditIntegrityError("aggregation result output INSERT returned the wrong ordinal")
                    for ordinal, item in enumerate(receipt_members):
                        inserted_ordinal = conn.execute(
                            aggregation_result_members_table.insert()
                            .values(
                                batch_id=batch_id,
                                run_id=run_id,
                                ordinal=ordinal,
                                token_id=item.member_ref.token_id,
                                action=item.action.value,
                                error_hash=item.error_hash,
                            )
                            .returning(aggregation_result_members_table.c.ordinal)
                        ).scalar_one()
                        if inserted_ordinal != ordinal:
                            raise AuditIntegrityError("aggregation result member INSERT returned the wrong ordinal")
                write_body_completed = True
        except SQLAlchemyError as exc:
            try:
                with self._db.read_only_connection() as conn:
                    state = conn.execute(
                        select(
                            node_states_table.c.status,
                            node_states_table.c.output_hash,
                            node_states_table.c.duration_ms,
                            node_states_table.c.success_reason_json,
                            node_states_table.c.context_after_json,
                        ).where(node_states_table.c.state_id == state_id)
                    ).one_or_none()
                    batch = conn.execute(
                        select(
                            batches_table.c.status,
                            batches_table.c.aggregation_state_id,
                            batches_table.c.trigger_type,
                            batches_table.c.trigger_reason,
                        ).where(batches_table.c.batch_id == batch_id)
                    ).one_or_none()
                    if state is not None and batch is not None and existing_receipt_is_exact(conn, state=state, batch=batch):
                        return receipt
            except (SQLAlchemyError, AuditIntegrityError):
                pass
            error_type = LandscapePostCommitError if write_body_completed else LandscapeRecordError
            raise error_type(f"complete_aggregation_result failed for batch_id={batch_id}: {type(exc).__name__}: {exc}") from exc
        try:
            with self._db.read_only_connection() as conn:
                state = conn.execute(
                    select(
                        node_states_table.c.status,
                        node_states_table.c.output_hash,
                        node_states_table.c.duration_ms,
                        node_states_table.c.success_reason_json,
                        node_states_table.c.context_after_json,
                    ).where(node_states_table.c.state_id == state_id)
                ).one()
                batch = conn.execute(
                    select(
                        batches_table.c.status,
                        batches_table.c.aggregation_state_id,
                        batches_table.c.trigger_type,
                        batches_table.c.trigger_reason,
                    ).where(batches_table.c.batch_id == batch_id)
                ).one()
                if not existing_receipt_is_exact(conn, state=state, batch=batch):  # pragma: no cover - receipt must exist
                    raise AuditIntegrityError("aggregation result receipt disappeared after commit")
        except (SQLAlchemyError, AuditIntegrityError) as exc:
            raise LandscapePostCommitError(f"aggregation result receipt {batch_id!r} failed exact post-commit readback") from exc
        return receipt

    def get_batch(self, batch_id: str) -> Batch | None:
        """Get a batch by ID."""
        return self.batches.get_batch(batch_id)

    def get_batches(
        self,
        run_id: str,
        *,
        status: BatchStatus | None = None,
        node_id: str | None = None,
    ) -> list[Batch]:
        """Get batches for a run."""
        return self.batches.get_batches(run_id, status=status, node_id=node_id)

    def get_incomplete_batches(self, run_id: str) -> list[Batch]:
        """Get batches that need recovery (draft, executing, or failed)."""
        return self.batches.get_incomplete_batches(run_id)

    def get_batch_members(self, batch_id: str) -> list[BatchMember]:
        """Get all members of a batch."""
        return self.batches.get_batch_members(batch_id)

    def get_all_batch_members_for_run(self, run_id: str) -> list[BatchMember]:
        """Get all batch members for a run (batch query)."""
        return self.batches.get_all_batch_members_for_run(run_id)

    def retry_batch(self, batch_id: str) -> Batch:
        """Create a new batch attempt from a failed batch (idempotent)."""
        return self.batches.retry_batch(batch_id)

    # === Artifact Registration (ArtifactRepository) ===

    def register_artifact(
        self,
        run_id: str,
        sink_node_id: str,
        artifact_type: str,
        path: str,
        content_hash: str,
        size_bytes: int,
        *,
        state_id: str | None = None,
        sink_effect_id: str | None = None,
        artifact_id: str | None = None,
        idempotency_key: str | None = None,
        publication_performed: bool = True,
        publication_evidence_kind: ArtifactPublicationEvidenceKind | None = None,
        conn: Connection | None = None,
    ) -> Artifact:
        """Register an artifact produced by a sink."""
        return self.artifacts.register_artifact(
            run_id,
            sink_node_id,
            artifact_type,
            path,
            content_hash,
            size_bytes,
            state_id=state_id,
            sink_effect_id=sink_effect_id,
            artifact_id=artifact_id,
            idempotency_key=idempotency_key,
            publication_performed=publication_performed,
            publication_evidence_kind=publication_evidence_kind,
            conn=conn,
        )

    def get_artifacts(
        self,
        run_id: str,
        *,
        sink_node_id: str | None = None,
    ) -> list[Artifact]:
        """Get artifacts for a run."""
        return self.artifacts.get_artifacts(run_id, sink_node_id=sink_node_id)
