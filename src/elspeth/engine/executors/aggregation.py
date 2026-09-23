"""AggregationExecutor - manages batch lifecycle with audit recording."""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import (
    BatchTransformProtocol,
    ExecutionError,
    PipelineRow,
    TokenInfo,
    TransformResult,
)
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.barrier_scalars import AggregationNodeScalars
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import (
    BatchStatus,
    NodeStateStatus,
    OutputMode,
    RoutingMode,
    TriggerType,
)
from elspeth.contracts.errors import (
    AuditIntegrityError,
    OrchestrationInvariantError,
    PluginContractViolation,
    RunLeadershipLostError,
    RunMembershipLostError,
)
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.node_state_context import AggregationBatchContext, AggregationFlushContext
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.secret_scrub import scrub_transform_error_reason
from elspeth.contracts.types import NodeID, StepResolver
from elspeth.core.canonical import stable_hash
from elspeth.core.config import AggregationSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.engine.aggregation_result import aggregation_result_members, validated_quarantined_indices
from elspeth.engine.clock import DEFAULT_CLOCK
from elspeth.engine.executors.batch_contract_validation import validate_batch_inputs, validate_success_outputs
from elspeth.engine.executors.state_guard import NodeStateGuard
from elspeth.engine.journal_restore import AggregationJournalRestorer
from elspeth.engine.spans import SpanFactory
from elspeth.engine.triggers import TriggerEvaluator

if TYPE_CHECKING:
    from elspeth.engine.clock import Clock

slog = structlog.get_logger(__name__)


class AggregationResultError(Exception):
    """Marker for a handled aggregation TransformResult.error outcome."""


@dataclass(slots=True)
class _AggregationNodeState:
    """Per-node aggregation state.

    Groups settings, trigger evaluator, batch tracking, and buffered tokens
    that were previously scattered across six parallel dicts keyed by NodeID,
    plus a member_count that was in a separate dict keyed by batch_id.

    Mutable because the token buffer grows during processing and
    batch_id/member_count change across batch lifecycles.  Not frozen (unlike
    _BranchEntry in coalesce_executor.py) because the fields are updated
    in-place.

    ``tokens`` is the single in-memory source of truth for buffered rows: each
    ``TokenInfo.row_data`` is an immutable, deep-frozen ``PipelineRow``, so the
    row-dict view is derived on demand via :meth:`snapshot_rows` rather than
    stored a second time.
    """

    settings: AggregationSettings
    trigger: TriggerEvaluator
    batch_id: str | None = None
    member_count: int = 0
    tokens: list[TokenInfo] = field(default_factory=list)
    # Durable counters used to derive AggregationBatchContext pagination metadata.
    # NOT persisted in the checkpoint row (F1 design D3): they derive from audit
    # tables at restore time and arrive via restore_from_journal.
    accepted_count_total: int = 0
    completed_flush_count: int = 0

    def snapshot_rows(self) -> list[dict[str, Any]]:
        """Derive the row-dict view of the buffered tokens.

        ``PipelineRow`` is deep-frozen and ``to_dict()`` returns a fresh mutable
        deep copy, so this projection is pure and stable — it always mirrors
        ``tokens`` and can never desync from it. Callers that mutate the result
        get an independent copy; the buffered tokens stay authoritative.
        """
        return [token.row_data.to_dict() for token in self.tokens]


@dataclass(frozen=True, slots=True)
class _FlushInputSnapshot:
    """Immutable view of the rows consumed by one aggregation flush."""

    buffered_tokens: tuple[TokenInfo, ...]
    buffered_rows: tuple[Mapping[str, Any], ...]
    pipeline_rows: tuple[PipelineRow, ...]
    representative_token: TokenInfo

    def __post_init__(self) -> None:
        # The row dicts arrive as fresh to_dict() copies; the PipelineRows in
        # pipeline_rows were built from the originals before this snapshot is
        # constructed, so deep-freezing here detaches without aliasing them.
        freeze_fields(self, "buffered_rows")


@dataclass(frozen=True, slots=True)
class _FlushWindow:
    """Pagination metadata for one aggregation flush."""

    batch_size: int
    rows_seen_total: int
    row_start: int
    row_end: int
    flush_index: int
    is_end_of_source: bool

    def batch_context(self, *, batch_id: str, trigger_type: TriggerType) -> AggregationBatchContext:
        return AggregationBatchContext(
            trigger_type=trigger_type.value,
            batch_id=batch_id,
            batch_size=self.batch_size,
            flush_index=self.flush_index,
            rows_seen_total=self.rows_seen_total,
            row_start=self.row_start,
            row_end=self.row_end,
            is_end_of_source=self.is_end_of_source,
        )

    def flush_context(self, *, batch_id: str, trigger_type: TriggerType) -> AggregationFlushContext:
        return AggregationFlushContext(
            trigger_type=trigger_type.value,
            buffer_size=self.batch_size,
            batch_id=batch_id,
            flush_index=self.flush_index,
            rows_seen_total=self.rows_seen_total,
            row_start=self.row_start,
            row_end=self.row_end,
            is_end_of_source=self.is_end_of_source,
        )


class AggregationExecutor:
    """Executes aggregations with batch tracking and audit recording.

    Manages the lifecycle of batches:
    1. Create batch on first accept (if _batch_id is None)
    2. Track batch members as rows are accepted
    3. Transition batch through states: draft -> executing -> completed/failed
    4. Reset _batch_id after flush for next batch

    CRITICAL: Terminal state CONSUMED_IN_BATCH is DERIVED from batch_members table,
    NOT stored in node_states.status (which is always "completed" for successful accepts).

    Example:
        executor = AggregationExecutor(execution, span_factory, step_resolver, run_id, data_flow=data_flow)

        # Accept rows into batch
        batch_id, ordinal = executor.open_batch_membership(node_id, coordination_token=coordination_token)
        # The scheduler durably adopts the member before the memory accept.
        executor.accept_adopted_row(node_id, token)
        # Engine uses TriggerEvaluator to decide when to flush
    """

    def __init__(
        self,
        execution: ExecutionRepository,
        span_factory: SpanFactory,
        step_resolver: StepResolver,
        run_id: str,
        *,
        data_flow: DataFlowRepository,
        aggregation_settings: dict[NodeID, AggregationSettings] | None = None,
        error_edge_ids: Mapping[NodeID, str] | None = None,
        clock: "Clock | None" = None,
    ) -> None:
        """Initialize executor.

        Args:
            execution: Execution repository for audit trail
            span_factory: Span factory for tracing
            step_resolver: Resolves NodeID to 1-indexed audit step position
            run_id: Run identifier for batch creation
            data_flow: Data flow repository; records the per-member
                transform_errors rows of a failed flush.
            aggregation_settings: Map of node_id -> AggregationSettings for trigger evaluation
            error_edge_ids: Map of aggregation node_id -> DIVERT edge_id of its
                ``__error_<name>__`` edge. Built by the processor from the edge
                map; populated only for aggregations whose on_error names a sink.
            clock: Optional clock for time access. Defaults to system clock.
                   Inject MockClock for deterministic testing.
        """
        self._execution = execution
        self._data_flow = data_flow
        self._error_edge_ids: Mapping[NodeID, str] = error_edge_ids or {}
        self._spans = span_factory
        self._step_resolver = step_resolver
        self._run_id = run_id
        self._clock = clock if clock is not None else DEFAULT_CLOCK

        # Single consolidated dict replaces 6 parallel dicts:
        # _aggregation_settings, _trigger_evaluators, _batch_ids,
        # _member_counts, _buffers, _buffer_tokens
        self._nodes: dict[NodeID, _AggregationNodeState] = {}
        for node_id, settings in (aggregation_settings or {}).items():
            self._nodes[node_id] = _AggregationNodeState(
                settings=settings,
                trigger=TriggerEvaluator(settings.trigger, clock=self._clock),
            )

    def _get_node(self, node_id: NodeID, caller: str = "") -> _AggregationNodeState:
        """Look up a configured aggregation node, crash on unknown.

        This is the single validation point for all node access. Unknown
        node_id is always a bug — either in the caller or in checkpoint
        data (Tier 1 corruption).
        """
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise OrchestrationInvariantError(
                f"{caller or 'AggregationExecutor'} called for node '{node_id}' "
                f"which is not in aggregation_settings. "
                f"Configured nodes: {list(self._nodes.keys())}"
            ) from exc

    def open_batch_membership(self, node_id: NodeID, *, coordination_token: CoordinationToken) -> tuple[str, int]:
        """Return ``(batch_id, next_ordinal)`` for the node's in-progress batch.

        Creates the ``batches`` row on the FIRST member in a leader-fenced
        transaction before the separately fenced adoption verb. Does NOT mutate
        buffers or counters: the membership/BUFFERED writes belong to
        ``adopt_blocked_barrier_item`` and the memory mutation to
        ``accept_adopted_row`` after the adoption CAS succeeds.

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        node = self._get_node(node_id, "open_batch_membership")
        if node.batch_id is None:
            batch = self._execution.create_batch(
                coordination_token=coordination_token,
                aggregation_node_id=node_id,
            )
            node.batch_id = batch.batch_id
            node.member_count = 0
        batch_id = node.batch_id
        if batch_id is None:
            raise OrchestrationInvariantError(f"batch_id is None after creation for node {node_id}")
        return batch_id, node.member_count

    def accept_adopted_row(
        self,
        node_id: NodeID,
        token: TokenInfo,
        *,
        accept_time: float | None = None,
    ) -> None:
        """Feed one durably-adopted row into executor memory (ADR-030 §E.2).

        The durable writes
        (``batch_members`` + BUFFERED ``token_outcomes``) already committed
        inside ``adopt_blocked_barrier_item``'s fenced transaction — this
        method appends the buffer entry, advances the counters and anchors the
        trigger latches at ``accept_time`` (the row's ``barrier_blocked_at``
        on the monotonic scale — backdated accept timing, §H 476).

        The open-batch -> fenced-adopt -> accept ordering (and the rule that
        the idempotent adopted=False SKIP arm must not re-feed memory) is
        owned by ``BarrierIntakeCoordinator._adopt_aggregation_row`` — the
        sole production caller. The no-open-batch guard below is the
        residual defence for out-of-sequence callers.

        Raises:
            OrchestrationInvariantError: If node_id is not a configured
                aggregation or no batch is open.
        """
        node = self._get_node(node_id, "accept_adopted_row")
        if node.batch_id is None:
            raise OrchestrationInvariantError(
                f"accept_adopted_row called for node {node_id} with no open batch; open_batch_membership must run before the adoption verb."
            )
        # Buffer the row. tokens is the single source of truth; the row-dict
        # view is derived on demand (_AggregationNodeState.snapshot_rows).
        node.tokens.append(token)
        node.member_count += 1
        # Durable cumulative counter that drives AggregationBatchContext.rows_seen_total.
        # Incremented exactly once per accepted row.
        node.accepted_count_total += 1
        node.trigger.record_accept(accept_time)

    def get_buffered_rows(self, node_id: NodeID) -> list[dict[str, Any]]:
        """Get currently buffered rows (does not clear buffer).

        Args:
            node_id: Aggregation node ID

        Returns:
            List of buffered row dicts (empty if no rows buffered yet)

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        return self._get_node(node_id, "get_buffered_rows").snapshot_rows()

    def get_buffered_tokens(self, node_id: NodeID) -> list[TokenInfo]:
        """Get currently buffered tokens (does not clear buffer).

        Args:
            node_id: Aggregation node ID

        Returns:
            List of buffered TokenInfo objects (empty if no rows buffered yet)

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        return list(self._get_node(node_id, "get_buffered_tokens").tokens)

    def _get_buffered_data(self, node_id: NodeID) -> tuple[list[dict[str, Any]], list[TokenInfo]]:
        """Internal: Get buffered rows and tokens without clearing.

        IMPORTANT: This method does NOT record audit trail. Production code
        should use execute_flush() instead. This method is exposed for:
        - Testing buffer contents without triggering flush

        Args:
            node_id: Aggregation node ID

        Returns:
            Tuple of (buffered_rows, buffered_tokens)

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        node = self._get_node(node_id, "_get_buffered_data")
        return node.snapshot_rows(), list(node.tokens)

    @staticmethod
    def _validate_batch_inputs(transform: BatchTransformProtocol, rows: Sequence[PipelineRow]) -> None:
        """Validate reconstructed batch input rows before plugin execution.

        Thin seam over the shared check. The body moved to
        ``batch_contract_validation`` so ``CollectorExecutor`` — which runs the
        same batch-transform contract and had NO preflight at all
        (elspeth-c2fa61cf57) — calls the same code rather than a copy of it.
        """
        validate_batch_inputs(transform, rows, node_kind="Aggregation")

    @staticmethod
    def _validate_success_outputs(transform: BatchTransformProtocol, result: TransformResult) -> None:
        """Validate successful batch output rows before audit completion."""
        validate_success_outputs(transform, result, node_kind="Aggregation")

    def _snapshot_flush_inputs(self, *, node_id: NodeID, node: _AggregationNodeState) -> _FlushInputSnapshot:
        """Snapshot buffered tokens and reconstruct batch transform input rows."""
        buffered_tokens = tuple(node.tokens)
        if not buffered_tokens:
            raise OrchestrationInvariantError(f"Cannot flush empty buffer for node {node_id}")
        buffered_rows = tuple(token.row_data.to_dict() for token in buffered_tokens)

        pipeline_rows: list[PipelineRow] = []
        for row_dict, token in zip(buffered_rows, buffered_tokens, strict=True):
            contract = token.row_data.contract
            if contract is None:
                raise OrchestrationInvariantError(
                    f"Token {token.token_id} has no contract - cannot reconstruct PipelineRow. "
                    f"This indicates a bug in accept_adopted_row() or checkpoint restore."
                )
            pipeline_rows.append(PipelineRow(row_dict, contract))

        return _FlushInputSnapshot(
            buffered_tokens=buffered_tokens,
            buffered_rows=buffered_rows,
            pipeline_rows=tuple(pipeline_rows),
            representative_token=buffered_tokens[0],
        )

    @staticmethod
    def _build_flush_window(
        *,
        node: _AggregationNodeState,
        batch_size: int,
        trigger_type: TriggerType,
    ) -> _FlushWindow:
        """Build durable aggregation pagination metadata for one flush."""
        rows_seen_total = node.accepted_count_total
        row_start = rows_seen_total - batch_size + 1
        return _FlushWindow(
            batch_size=batch_size,
            rows_seen_total=rows_seen_total,
            row_start=row_start,
            row_end=rows_seen_total,
            flush_index=node.completed_flush_count + 1,
            is_end_of_source=trigger_type is TriggerType.END_OF_SOURCE,
        )

    @staticmethod
    def _prepare_plugin_context(
        *,
        ctx: PluginContext,
        guard: NodeStateGuard,
        node_id: NodeID,
        batch_token_ids: tuple[str, ...],
        batch_context: AggregationBatchContext,
    ) -> None:
        """Expose audit and aggregation context to the batch transform."""
        ctx.state_id = guard.state_id
        ctx.node_id = node_id
        ctx.batch_token_ids = batch_token_ids
        ctx.aggregation_batch = batch_context

    def _invoke_batch_transform(
        self,
        *,
        transform: BatchTransformProtocol,
        pipeline_rows: Sequence[PipelineRow],
        ctx: PluginContext,
        guard: NodeStateGuard,
    ) -> tuple[TransformResult, float]:
        """Run the batch transform and record direct plugin failures.

        A Tier-2 ``PluginContractViolation`` passes through with the state
        still open: ``execute_flush`` routes it as a failed batch and completes
        the state itself, with the routed reason.
        """
        start = time.perf_counter()
        try:
            result = transform.process(list(pipeline_rows), ctx)
            duration_ms = (time.perf_counter() - start) * 1000
        except (RunLeadershipLostError, RunMembershipLostError):
            raise
        except contract_errors.TIER_1_ERRORS:
            raise
        except PluginContractViolation:
            raise
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            guard.complete(
                NodeStateStatus.FAILED,
                duration_ms=duration_ms,
                error=ExecutionError(
                    exception=str(exc),
                    exception_type=type(exc).__name__,
                ),
            )
            raise
        return result, duration_ms

    @staticmethod
    def _populate_result_audit_fields(
        *,
        transform: BatchTransformProtocol,
        result: TransformResult,
        input_hash: str,
        duration_ms: float,
    ) -> None:
        """Populate TransformResult audit hashes and duration."""
        result.input_hash = input_hash
        try:
            if result.row is not None:
                result.output_hash = stable_hash(result.row)
            elif result.rows is not None:
                result.output_hash = stable_hash(result.rows)
            else:
                result.output_hash = None
        except (TypeError, ValueError) as exc:
            raise PluginContractViolation(
                f"Aggregation transform '{transform.name}' emitted non-canonical data: {exc}. "
                f"Ensure output contains only JSON-serializable types. "
                f"Use None instead of NaN for missing values."
            ) from exc
        result.duration_ms = duration_ms

    def _run_flush_transform(
        self,
        *,
        node: _AggregationNodeState,
        transform: BatchTransformProtocol,
        pipeline_rows: Sequence[PipelineRow],
        ctx: PluginContext,
        guard: NodeStateGuard,
        input_hash: str,
    ) -> tuple[TransformResult, float]:
        """Produce the flush's result: the plugin's own, or its contract violation as a failed batch.

        A Tier-2 ``PluginContractViolation`` raised here — the buffered-input
        preflight, the batch plugin itself, canonical hashing of its result, or
        the success result's output checks — fails the WHOLE batch as a
        returned error would (operator ruling 2026-09-23, elspeth-5887fb7928
        B2): ``_complete_error_flush`` then records it and the processor
        applies the aggregation's ``on_error`` to every buffered row. A
        wrongly-typed row under a typed aggregation schema is a fact about that
        row, not only about the configuration, so it must not end the run.

        Nothing is recorded before these checks, so a routed batch cannot also
        hold a terminal. The Tier-1 subclasses (``SinkTransactionalInvariantError``)
        still abort, and so does the processor's declaration cross-check, which
        runs after this and writes each member's terminal before it raises.
        """
        start = time.perf_counter()
        try:
            self._validate_batch_inputs(transform, pipeline_rows)
            result, duration_ms = self._invoke_batch_transform(
                transform=transform,
                pipeline_rows=pipeline_rows,
                ctx=ctx,
                guard=guard,
            )
            self._populate_result_audit_fields(
                transform=transform,
                result=result,
                input_hash=input_hash,
                duration_ms=duration_ms,
            )
            if result.status == "success":
                self._check_successful_result(node=node, transform=transform, result=result)
        except contract_errors.TIER_1_ERRORS:
            raise
        except PluginContractViolation as violation:
            failed_batch = TransformResult.error(violation.to_transform_error_reason(), retryable=False)
            failed_duration_ms = (time.perf_counter() - start) * 1000
            self._populate_result_audit_fields(
                transform=transform,
                result=failed_batch,
                input_hash=input_hash,
                duration_ms=failed_duration_ms,
            )
            return failed_batch, failed_duration_ms
        return result, duration_ms

    def _check_successful_result(
        self,
        *,
        node: _AggregationNodeState,
        transform: BatchTransformProtocol,
        result: TransformResult,
    ) -> None:
        """The plugin-contract checks a success result must pass before completion.

        Each raises a Tier-2 ``PluginContractViolation``, which ``execute_flush``
        routes as a failed batch — they run before anything is recorded.
        """
        self._validate_success_outputs(transform, result)

        if result.row is None and result.rows is None:
            raise PluginContractViolation(
                f"Aggregation transform '{transform.name}' returned success status but "
                f"neither row nor rows contains data. Batch-aware transforms must return "
                f"output via TransformResult.success(row) or TransformResult.success_multi(rows)."
            )

        output_count = 1 if result.row is not None else len(result.rows or ())
        expected_count = node.settings.expected_output_count
        if node.settings.output_mode is OutputMode.TRANSFORM and expected_count is not None and output_count != expected_count:
            raise PluginContractViolation(
                f"Aggregation {node.settings.name!r} produced {output_count} output row(s), but expected_output_count={expected_count}."
            )

    def _complete_successful_flush(
        self,
        *,
        node_id: NodeID,
        node: _AggregationNodeState,
        transform: BatchTransformProtocol,
        result: TransformResult,
        guard: NodeStateGuard,
        duration_ms: float,
        batch_id: str,
        trigger_type: TriggerType,
        window: _FlushWindow,
        buffered_tokens: Sequence[TokenInfo],
        coordination_token: CoordinationToken,
    ) -> None:
        """Record successful node-state and batch completion (``_check_successful_result`` passed)."""
        output_rows = (result.row,) if result.row is not None else tuple(result.rows or ())
        quarantined_indices = validated_quarantined_indices(
            result,
            buffered_token_count=len(buffered_tokens),
            aggregation_name=node.settings.name,
        )
        if node.settings.output_mode is OutputMode.TRANSFORM:
            non_quarantined_tokens = tuple(token for index, token in enumerate(buffered_tokens) if index not in quarantined_indices)
            if output_rows and not non_quarantined_tokens:
                raise OrchestrationInvariantError(
                    f"Aggregation {node.settings.name!r} emitted output but all buffered tokens were quarantined"
                )
            expansion_parent_token_id = non_quarantined_tokens[0].token_id if output_rows else None
        else:
            if quarantined_indices:
                raise OrchestrationInvariantError("passthrough aggregation cannot declare quarantined_indices")
            if result.rows is None:
                raise OrchestrationInvariantError(
                    f"Passthrough mode requires multi-row result, but transform {transform.name!r} returned single row. "
                    "Use TransformResult.success_multi() for passthrough."
                )
            if output_rows and len(output_rows) != len(buffered_tokens):
                raise OrchestrationInvariantError(
                    f"Passthrough mode requires same number of output rows as input rows. Transform {transform.name!r} "
                    f"returned {len(output_rows)} rows but received {len(buffered_tokens)} input rows."
                )
            expansion_parent_token_id = None
        if result.output_hash is None:
            raise OrchestrationInvariantError("successful aggregation result lacks output_hash")
        guard.complete_aggregation_result(
            batch_id=batch_id,
            coordination_token=coordination_token,
            aggregation_node_id=str(node_id),
            trigger_type=trigger_type,
            output_mode=node.settings.output_mode,
            output_rows=output_rows,
            output_shape="empty" if not output_rows else ("multi" if result.rows is not None else "single"),
            output_hash=result.output_hash,
            members=aggregation_result_members(
                buffered_tokens,
                run_id=self._run_id,
                batch_id=batch_id,
                output_mode=node.settings.output_mode,
                output_is_empty=not output_rows,
                quarantined_indices=quarantined_indices,
            ),
            expansion_parent_token_id=expansion_parent_token_id,
            duration_ms=duration_ms,
            success_reason=result.success_reason,
            context_after=window.flush_context(batch_id=batch_id, trigger_type=trigger_type),
        )
        node.completed_flush_count += 1

    def _complete_error_flush(
        self,
        *,
        node_id: NodeID,
        node: _AggregationNodeState,
        transform: BatchTransformProtocol,
        ctx: PluginContext,
        result: TransformResult,
        guard: NodeStateGuard,
        duration_ms: float,
        batch_id: str,
        trigger_type: TriggerType,
        buffered_tokens: Sequence[TokenInfo],
    ) -> None:
        """Record a transform-returned batch failure and apply the declared error route.

        The whole batch failed (the transform's verdict), so every buffered
        member shares the one batch reason. In order (record-before-complete,
        parity with ``TransformExecutor``'s error-result branch):

        1. require ``result.reason``, scrub it, and WRITE IT BACK onto
           ``result.reason`` — the processor builds each routed member's
           FailureInfo (and so its durable ``pending_error_message``) from it;
        2. one ``transform_errors`` row per buffered member, destination =
           ``on_error`` (a sink name, or ``"discard"``), in ONE leader-fenced
           write;
        3. for a named sink, ONE DIVERT ``routing_event`` on this flush's
           node_state along the ``__error_<name>__`` edge;
        4. the node_state FAILED with the scrubbed reason dict, then the batch
           FAILED.

        The DIVERT edge is resolved before any write, so a missing edge
        refuses without leaving half an envelope. A write that raises before
        step 4 leaves the guard to auto-fail the state and propagates.
        """
        if result.reason is None:
            raise OrchestrationInvariantError(
                f"Aggregation transform '{transform.name}' returned error but reason is None. "
                'Use TransformResult.error({"reason": "...", ...}) to create error results.'
            )
        scrubbed_reason = scrub_transform_error_reason(result.reason)
        result.reason = scrubbed_reason

        on_error = node.settings.on_error
        divert_edge_id: str | None = None
        if on_error != "discard":
            try:
                divert_edge_id = self._error_edge_ids[node_id]
            except KeyError as exc:
                raise OrchestrationInvariantError(
                    f"Aggregation '{node.settings.name}' has on_error={on_error!r} but no DIVERT edge "
                    f"registered. DAG construction should have created an __error_{node.settings.name}__ edge "
                    "in from_plugin_instances()."
                ) from exc

        coordination_token = ctx.require_coordination_token()
        self._data_flow.record_batch_transform_errors_leader(
            members=tuple((TokenRef(token_id=token.token_id, run_id=self._run_id), token.row_data) for token in buffered_tokens),
            transform_id=str(node_id),
            error_details=scrubbed_reason,
            destination=on_error,
            coordination_token=coordination_token,
        )
        if divert_edge_id is not None:
            self._execution.record_routing_event(
                member_token=ctx.require_member_token(),
                state_id=guard.state_id,
                edge_id=divert_edge_id,
                mode=RoutingMode.DIVERT,
                reason=scrubbed_reason,
            )
        guard.complete(
            NodeStateStatus.FAILED,
            duration_ms=duration_ms,
            error=scrubbed_reason,
        )
        self._execution.complete_batch(
            coordination_token=coordination_token,
            batch_id=batch_id,
            status=BatchStatus.FAILED,
            trigger_type=trigger_type,
            state_id=guard.state_id,
        )

    def _fail_unfinalized_batch(
        self,
        *,
        coordination_token: CoordinationToken,
        batch_id: str,
        trigger_type: TriggerType,
        state_id: str,
    ) -> None:
        """Mark a failed flush's batch as FAILED or raise audit-integrity error."""
        try:
            self._execution.complete_batch(
                coordination_token=coordination_token,
                batch_id=batch_id,
                status=BatchStatus.FAILED,
                trigger_type=trigger_type,
                state_id=state_id,
            )
        except (RunLeadershipLostError, RunMembershipLostError):
            raise
        except contract_errors.TIER_1_ERRORS:
            raise
        except (TypeError, AttributeError, KeyError, NameError):
            raise
        except Exception as cleanup_err:
            raise AuditIntegrityError(
                f"Failed to mark batch {batch_id} as FAILED during error cleanup — "
                f"batch would remain in non-terminal state (audit trail corruption). "
                f"Cleanup error: {cleanup_err}"
            ) from cleanup_err

    def _clear_flush_state(self, *, node_id: NodeID, node: _AggregationNodeState, ctx: PluginContext) -> None:
        """Clear in-memory and plugin-context flush state."""
        self._reset_batch_state(node_id)
        node.tokens.clear()
        node.trigger.reset()
        ctx.batch_token_ids = None
        ctx.aggregation_batch = None

    def execute_flush(
        self,
        node_id: NodeID,
        transform: BatchTransformProtocol,
        ctx: PluginContext,
        trigger_type: TriggerType,
        *,
        validate_success: Callable[[TransformResult, Sequence[TokenInfo], str], None] | None = None,
    ) -> tuple[TransformResult, list[TokenInfo], str]:
        """Execute a batch flush with full audit recording.

        This method:
        1. Transitions batch to "executing" with trigger reason
        2. Records node_state for the flush operation
        3. Executes the batch-aware transform
        4. Transitions batch to "completed" or "failed"; a returned error —
           or a Tier-2 ``PluginContractViolation`` raised before completion,
           which ``_run_flush_transform`` turns into one — first records one
           transform_errors row per member and, for a named on_error sink,
           one DIVERT routing_event on the flush state
           (``_complete_error_flush``)
        5. Resets batch_id for next batch

        The step position in the DAG is resolved internally via StepResolver
        using node_id, rather than being passed as a parameter.

        Args:
            node_id: Aggregation node ID
            transform: Batch-aware transform plugin (must implement BatchTransformProtocol)
            ctx: Plugin context
            trigger_type: What triggered the flush (COUNT, TIMEOUT, END_OF_SOURCE, etc.)

        Returns:
            Tuple of (TransformResult with audit fields, list of consumed tokens, batch_id)

        Raises:
            Exception: Any other exception from transform.process() (recorded
                FAILED first), a Tier-1 error, or a violation raised by
                ``validate_success`` (the processor's declaration cross-check,
                which records each member's terminal before raising). The batch
                is marked FAILED and the run aborts.
        """
        node = self._get_node(node_id, "execute_flush")
        batch_id = node.batch_id
        if batch_id is None:
            raise OrchestrationInvariantError(f"No batch exists for node {node_id} - cannot flush")

        snapshot = self._snapshot_flush_inputs(node_id=node_id, node=node)

        # Step 1: Transition batch to "executing"
        self._execution.update_batch_status(
            coordination_token=ctx.require_coordination_token(),
            batch_id=batch_id,
            status=BatchStatus.EXECUTING,
            trigger_type=trigger_type,
        )

        batch_input: dict[str, Any] = {"batch_rows": list(snapshot.buffered_rows)}
        input_hash = stable_hash(batch_input)
        step = self._step_resolver(node_id)

        # NodeStateGuard guarantees the node state reaches terminal status.
        # If any guarded flush step (validation, invocation, output hashing, or
        # batch completion) raises before the state is explicitly completed,
        # the guard auto-completes it as FAILED. Batch lifecycle cleanup is
        # handled separately below.
        # Attempt honors the token's resume offset: a journal-restored flush
        # re-run (the original flush crashed and wrote a FAILED node_state at
        # the prior attempt) must not collide with audited history (F1).
        batch_token_ids = tuple(token.token_id for token in snapshot.buffered_tokens)
        with (
            self._spans.aggregation_span(
                transform.name,
                node_id=node_id,
                batch_id=batch_id,
                token_ids=batch_token_ids,
                run_id=self._run_id,
            ) as aggregation_span,
            NodeStateGuard(
                self._execution,
                token_id=snapshot.representative_token.token_id,
                node_id=node_id,
                member_token=ctx.require_member_token(),
                step_index=step,
                input_data=batch_input,
                attempt=snapshot.representative_token.resume_attempt_offset,
                resume_checkpoint_id=snapshot.representative_token.resume_checkpoint_id,
                auto_fail_phase="aggregation_flush",
            ) as guard,
        ):
            window = self._build_flush_window(
                node=node,
                batch_size=len(snapshot.buffered_rows),
                trigger_type=trigger_type,
            )
            self._prepare_plugin_context(
                ctx=ctx,
                guard=guard,
                node_id=node_id,
                batch_token_ids=batch_token_ids,
                batch_context=window.batch_context(batch_id=batch_id, trigger_type=trigger_type),
            )

            # Track whether the batch was finalized (COMPLETED or FAILED).
            # Used by the outer except to decide whether to fail the batch.
            batch_finalized = False

            try:
                result, duration_ms = self._run_flush_transform(
                    node=node,
                    transform=transform,
                    pipeline_rows=snapshot.pipeline_rows,
                    ctx=ctx,
                    guard=guard,
                    input_hash=input_hash,
                )

                # Complete node state and batch
                if result.status == "success":
                    if validate_success is not None:
                        validate_success(result, snapshot.buffered_tokens, batch_id)
                    self._complete_successful_flush(
                        coordination_token=ctx.require_coordination_token(),
                        node_id=node_id,
                        node=node,
                        transform=transform,
                        result=result,
                        guard=guard,
                        duration_ms=duration_ms,
                        batch_id=batch_id,
                        trigger_type=trigger_type,
                        window=window,
                        buffered_tokens=snapshot.buffered_tokens,
                    )
                    batch_finalized = True
                else:
                    self._spans.mark_error(aggregation_span, AggregationResultError())
                    self._complete_error_flush(
                        node_id=node_id,
                        node=node,
                        transform=transform,
                        ctx=ctx,
                        result=result,
                        guard=guard,
                        duration_ms=duration_ms,
                        batch_id=batch_id,
                        trigger_type=trigger_type,
                        buffered_tokens=snapshot.buffered_tokens,
                    )
                    batch_finalized = True

            except (RunLeadershipLostError, RunMembershipLostError):
                raise  # Ownership loss leaves this attempt for the new leader.
            except contract_errors.TIER_1_ERRORS:
                raise  # Tier 1 errors must crash — skip batch cleanup
            except Exception:
                # Batch cleanup on ANY failure (guard handles node state).
                # Only attempt to fail the batch if it wasn't already finalized
                # (avoids double-write if complete_batch itself raised).
                if not batch_finalized:
                    self._fail_unfinalized_batch(
                        coordination_token=ctx.require_coordination_token(),
                        batch_id=batch_id,
                        trigger_type=trigger_type,
                        state_id=guard.state_id,
                    )
                self._clear_flush_state(node_id=node_id, node=node, ctx=ctx)
                raise

            # Success cleanup: save batch_id before reset (needed by caller for CONSUMED_IN_BATCH)
            flushed_batch_id = batch_id

            self._clear_flush_state(node_id=node_id, node=node, ctx=ctx)

        return result, list(snapshot.buffered_tokens), flushed_batch_id

    def _reset_batch_state(self, node_id: NodeID) -> None:
        """Reset batch tracking state for next batch.

        INTERNAL: Only called from execute_flush() which has already validated
        that batch_id exists. Uses _get_node() for consistent validation.

        Args:
            node_id: Aggregation node ID
        """
        node = self._get_node(node_id, "_reset_batch_state")
        if node.batch_id is None:
            raise OrchestrationInvariantError(f"_reset_batch_state invariant violation: batch_id is None for {node_id}")
        node.batch_id = None
        node.member_count = 0

    def get_buffer_count(self, node_id: NodeID) -> int:
        """Get the number of rows currently buffered for an aggregation.

        Args:
            node_id: Aggregation node ID

        Returns:
            Number of buffered rows (0 if no rows buffered yet)

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        return len(self._get_node(node_id, "get_buffer_count").tokens)

    def get_barrier_scalars(self) -> dict[NodeID, AggregationNodeScalars]:
        """Return the underivable trigger-latch scalars for the checkpoint row.

        F1 design D3: the checkpoint persists ONLY scalar barrier metadata —
        buffered tokens live in journal BLOCKED rows and counters derive from
        audit tables at restore time. The only underivable aggregation state
        is the pair of trigger fire-time latches, read live from each node's
        TriggerEvaluator.

        Emission choice: only nodes with at least one non-None fire offset are
        emitted. The checkpoint writer serializes None when no scalars exist
        (``BarrierScalars.has_state``), and restore treats a missing entry as
        ``(None, None)`` — so emitting unlatched/counter-only nodes would add
        bytes without information. This is design D3 applied to emission:
        everything restorable about a counter-only node derives from audit
        tables at restore time, so the checkpoint has nothing to say about it.

        Returns:
            Mapping of node_id -> AggregationNodeScalars for latched nodes only.
        """
        scalars: dict[NodeID, AggregationNodeScalars] = {}
        for node_id, node in self._nodes.items():
            count_fire_offset = node.trigger.get_count_fire_offset()
            condition_fire_offset = node.trigger.get_condition_fire_offset()
            if count_fire_offset is None and condition_fire_offset is None:
                continue
            scalars[node_id] = AggregationNodeScalars(
                count_fire_offset=count_fire_offset,
                condition_fire_offset=condition_fire_offset,
            )
        return scalars

    def restore_from_journal(
        self,
        *,
        node_id: NodeID,
        items: Sequence[TokenWorkItem],
        member_order: Sequence[str],
        batch_id: str | None,
        accepted_count_total: int,
        completed_flush_count: int,
        scalars: AggregationNodeScalars,
        attempt_offsets: Mapping[str, int],
        resume_checkpoint_id: str,
        now: datetime,
    ) -> None:
        """Rebuild one node's buffers from journal BLOCKED rows (F1 resume path).

        Replaces the checkpoint-blob restore: the journal (token_work_items
        BLOCKED rows) is authoritative for buffered token payloads; the caller
        (processor, Task 3.1) partitions journal items by barrier_key and
        derives batch_id / member_order / counters / attempt offsets from
        audit tables.

        Validation, journal-vs-batch_members reconciliation, token
        rehydration, and the trigger-latch staleness decision live in
        ``AggregationJournalRestorer`` (the restore/hydration boundary); this
        method resolves the node and applies the returned frozen state to its
        buffers and trigger evaluator.

        Args:
            node_id: Aggregation node being restored.
            items: BLOCKED journal rows for this node's barrier_key.
            member_order: Token ids in batch_members.ordinal order — the
                authoritative accept order for buffer reconstruction.
            batch_id: The in-progress batch id (None for a counter-only node).
            accepted_count_total: Audit-derived cumulative accept counter.
            completed_flush_count: Audit-derived completed-flush counter.
            scalars: Trigger fire-time latches from the checkpoint row.
                IGNORED when ``items`` is empty: latches are batch-scoped and
                zero buffered rows means there is no current batch — non-None
                latches here are stale (checkpoint older than the journal, a
                legitimate window under D3's staleness model), so they are
                dropped with a log line rather than rejected.
            attempt_offsets: Per-token resume attempt offset (max_attempt + 1).
            resume_checkpoint_id: Checkpoint id stamped on restored tokens
                (resume provenance).
            now: Current wall-clock time (tz-aware) — trigger age derives from
                ``now - min(barrier_blocked_at)``, not from an offset blob.

        Raises:
            AuditIntegrityError: On any journal/audit disagreement — NULL
                barrier_blocked_at, duplicate journal rows, membership
                mismatch, duplicate member_order entries, missing attempt
                offset, batch_id/items inconsistency, impossible counters.
            ValueError: If the restored trigger latch contradicts its trigger
                configuration or restored batch state.
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        node = self._get_node(node_id, "restore_from_journal")

        restored = AggregationJournalRestorer(run_id=self._run_id).restore(
            node_id=node_id,
            items=items,
            member_order=member_order,
            batch_id=batch_id,
            accepted_count_total=accepted_count_total,
            completed_flush_count=completed_flush_count,
            scalars=scalars,
            attempt_offsets=attempt_offsets,
            resume_checkpoint_id=resume_checkpoint_id,
            now=now,
        )

        restored_trigger = TriggerEvaluator(node.settings.trigger, clock=self._clock)
        latch = restored.trigger_latch
        if latch is not None:
            restored_trigger.restore_from_checkpoint(
                batch_count=latch.batch_count,
                elapsed_age_seconds=latch.elapsed_age_seconds,
                count_fire_offset=latch.count_fire_offset,
                condition_fire_offset=latch.condition_fire_offset,
            )

        # Apply only after both the journal state and trigger state validate.
        # A failed restore must leave every existing node field untouched.
        node.tokens = list(restored.tokens)
        node.batch_id = restored.batch_id
        node.member_count = len(restored.tokens)
        node.accepted_count_total = restored.accepted_count_total
        node.completed_flush_count = restored.completed_flush_count
        node.trigger = restored_trigger

        slog.info(
            "aggregation_journal_restored",
            node_id=str(node_id),
            token_count=len(restored.tokens),
            batch_id=restored.batch_id,
            accepted_count_total=restored.accepted_count_total,
            completed_flush_count=restored.completed_flush_count,
            elapsed_age_seconds=restored.elapsed_age_seconds,
        )

    def get_batch_id(self, node_id: NodeID) -> str | None:
        """Get current batch ID for an aggregation node.

        Args:
            node_id: Aggregation node ID

        Returns:
            Batch ID if a batch is in progress, None otherwise

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        return self._get_node(node_id, "get_batch_id").batch_id

    def should_flush(self, node_id: NodeID) -> bool:
        """Check if the aggregation should flush based on trigger config.

        Args:
            node_id: Aggregation node ID

        Returns:
            True if trigger condition is met, False otherwise

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        return self._get_node(node_id, "should_flush").trigger.should_trigger()

    def get_trigger_type(self, node_id: NodeID) -> "TriggerType | None":
        """Get the TriggerType for the trigger that fired.

        Args:
            node_id: Aggregation node ID

        Returns:
            TriggerType enum if a trigger fired, None otherwise

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        return self._get_node(node_id, "get_trigger_type").trigger.get_trigger_type()

    def check_flush_status(self, node_id: NodeID) -> tuple[bool, "TriggerType | None"]:
        """Check flush status and get trigger type in a single operation.

        This is an optimized method that combines should_flush() and get_trigger_type()
        with a single dict lookup instead of two. Used in the hot path where
        timeout checks happen before every row is processed.

        Args:
            node_id: Aggregation node ID

        Returns:
            Tuple of (should_flush, trigger_type):
            - should_flush: True if trigger condition is met
            - trigger_type: The type of trigger that fired, or None

        Raises:
            OrchestrationInvariantError: If node_id is not a configured aggregation.
        """
        node = self._get_node(node_id, "check_flush_status")
        should_flush = node.trigger.should_trigger()
        trigger_type = node.trigger.get_trigger_type() if should_flush else None
        return (should_flush, trigger_type)
