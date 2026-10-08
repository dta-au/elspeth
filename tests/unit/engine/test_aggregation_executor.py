"""Aggregation executor buffering, flush, journal restore, and terminality tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.contracts import (
    TokenInfo,
    TransformResult,
)
from elspeth.contracts.barrier_scalars import AggregationNodeScalars
from elspeth.contracts.data import PluginSchema as _PermissiveSchema
from elspeth.contracts.enums import (
    AggregationMemberAction,
    BatchStatus,
    FrameKind,
    NodeStateStatus,
    OutputMode,
    TriggerType,
)
from elspeth.contracts.errors import (
    AuditIntegrityError,
    OrchestrationInvariantError,
    PluginContractViolation,
    SinkTransactionalInvariantError,
)
from elspeth.contracts.events import EngineSpanCompleted, EngineSpanName, EngineSpanStatus
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import AggregationSettings, TriggerConfig
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.engine.clock import MockClock
from elspeth.engine.executors import (
    AggregationExecutor,
)
from elspeth.engine.spans import SpanFactory
from elspeth.testing import make_row
from tests.fixtures.audit_hashing import assert_stable_hash
from tests.fixtures.executors import (
    _AGGREGATION_LEADER,
    _CallRecorder,
    _make_contract,
    _make_factory,
    _make_span_factory,
    _make_token,
    _single_complete_node_state_kwargs,
)
from tests.fixtures.factories import make_context
from tests.unit.engine.conftest import make_test_step_resolver as _make_step_resolver

# Reference instant for journal-restore tests (tz-aware, like barrier_blocked_at).
_JOURNAL_T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _journal_payload(data: dict[str, Any], contract: SchemaContract | None = None) -> str:
    """Build a REAL journal row payload via the serializer mark_blocked rows carry.

    Round-trip fidelity through serialize_row_payload/deserialize_row_payload is
    the property restore_from_journal depends on — fixtures must not shortcut it.
    """
    if contract is None:
        contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    return TokenSchedulerRepository.serialize_row_payload(PipelineRow(data, contract))


def _blocked_item(
    *,
    token_id: str,
    row_id: str,
    payload: str,
    blocked_at: datetime | None,
    node_id: str = "agg_1",
    ingest_sequence: int = 0,
    attempt: int = 0,
    branch_name: str | None = None,
    fork_group_id: str = "fg-executors-test",
    join_group_id: str | None = None,
) -> TokenWorkItem:
    """Build a BLOCKED journal row as list_blocked_barrier_items returns them."""
    lineage_path = () if branch_name is None else (LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch_name),)
    return TokenWorkItem(
        work_item_id=f"wi-{token_id}",
        run_id="test-run",
        token_id=token_id,
        row_id=row_id,
        node_id=node_id,
        step_index=0,
        ingest_sequence=ingest_sequence,
        row_payload_json=payload,
        status=TokenWorkStatus.BLOCKED,
        attempt=attempt,
        available_at=_JOURNAL_T0,
        created_at=_JOURNAL_T0,
        updated_at=_JOURNAL_T0,
        barrier_key=f"aggregation:{node_id}",
        lineage_path=lineage_path,
        join_group_id=join_group_id,
        barrier_blocked_at=blocked_at,
    )


class _AggregationTransformDouble:
    def __init__(self, *, name: str = "agg_transform") -> None:
        self.name = name
        self.input_schema = _PermissiveSchema
        self.output_schema = _PermissiveSchema
        # `BatchTransformProtocol` requires it; the flush postflight's ADR-050
        # value check reads it. None declares no output contract to enforce.
        self._output_schema_config: SchemaConfig | None = None
        self.process = _CallRecorder()

    @property
    def declared_read_fields(self) -> frozenset[str]:
        # BatchTransformProtocol spelling surface: this fake declares its required input columns only.
        return self.schema_required_input_fields()

    def schema_required_input_fields(self) -> frozenset[str]:
        # Declares no required column, so the flush preflight's presence check passes every row.
        return frozenset()


def _make_aggregation_transform(name: str = "agg_transform") -> _AggregationTransformDouble:
    return _AggregationTransformDouble(name=name)


# =============================================================================
# TestAggregationExecutor
# =============================================================================


def _accept_adopted_aggregation_row(executor: AggregationExecutor, node_id: NodeID, token: TokenInfo) -> tuple[str, int]:
    """Feed the executor side of adoption; scheduler persistence has its own tests."""
    membership = executor.open_batch_membership(node_id, coordination_token=_AGGREGATION_LEADER)
    executor.accept_adopted_row(node_id, token)
    return membership


class TestAggregationExecutor:
    """Tests for AggregationExecutor covering buffering, flush, and triggers."""

    def _make_agg_executor(
        self,
        factory: MagicMock | None = None,
        node_id: str = "agg_1",
        count: int = 3,
        clock: MockClock | None = None,
        span_factory: SpanFactory | None = None,
        on_error: str = "discard",
        error_edge_ids: Mapping[NodeID, str] | None = None,
        expected_output_count: int | None = None,
    ) -> tuple[AggregationExecutor, MagicMock, NodeID]:
        """Create an AggregationExecutor with a single configured node."""
        if factory is None:
            factory = _make_factory()
        if span_factory is None:
            span_factory = _make_span_factory()
        nid = NodeID(node_id)
        settings = AggregationSettings(
            name="test_agg",
            plugin="batch_stats",
            input="default",
            on_error=on_error,
            trigger=TriggerConfig(count=count),
            expected_output_count=expected_output_count,
        )
        executor = AggregationExecutor(
            factory.execution,
            span_factory,
            _make_step_resolver(),
            run_id="test-run",
            aggregation_settings={nid: settings},
            error_edge_ids=error_edge_ids,
            clock=clock,
        )
        return executor, factory, nid

    # --- buffer_row ---

    def test_buffer_row_unconfigured_node_raises(self) -> None:
        """buffer_row for unconfigured node raises OrchestrationInvariantError."""
        executor, _, _ = self._make_agg_executor()
        token = _make_token()

        with pytest.raises(OrchestrationInvariantError, match="not in aggregation_settings"):
            _accept_adopted_aggregation_row(executor, NodeID("unknown"), token)

    def test_buffer_row_first_row_creates_batch(self) -> None:
        """First buffered row creates a new batch via execution repo."""
        executor, factory, nid = self._make_agg_executor()
        token = _make_token()

        _accept_adopted_aggregation_row(executor, nid, token)

        factory.execution.create_batch.assert_called_once_with(
            coordination_token=_AGGREGATION_LEADER,
            aggregation_node_id=nid,
        )

    def test_buffer_row_subsequent_rows_reuse_batch(self) -> None:
        """Second row does not create a new batch."""
        executor, factory, nid = self._make_agg_executor()

        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t2"))

        # Only one batch created
        assert factory.execution.create_batch.call_count == 1

    def test_adopted_rows_advance_batch_member_ordinal(self) -> None:
        """Each accepted adoption advances the next ordinal in the same batch."""
        executor, _, nid = self._make_agg_executor()

        first_batch, first_ordinal = _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        second_batch, second_ordinal = _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t2"))

        assert first_batch == second_batch
        assert (first_ordinal, second_ordinal) == (0, 1)

    # --- get_buffered_rows / get_buffered_tokens ---

    @pytest.mark.parametrize("membership_lost", [False, True])
    @pytest.mark.parametrize("during_cleanup", [False, True])
    def test_flush_authority_refusal_preserves_open_attempt(self, membership_lost: bool, during_cleanup: bool) -> None:
        from elspeth.contracts.errors import RunLeadershipLostError, RunMembershipLostError

        executor, factory, nid = self._make_agg_executor(count=1)
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        failure = (
            RunMembershipLostError(run_id="test-run", worker_id="mock-worker", verb="complete_batch")
            if membership_lost
            else RunLeadershipLostError(run_id="test-run", worker_id="mock-worker", leader_epoch=1, verb="complete_batch")
        )
        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.success(
            make_row({"value": "aggregated"}, contract=contract),
            success_reason={"action": "aggregated"},
        )
        if during_cleanup:
            factory.execution.complete_aggregation_result.side_effect = RuntimeError("receipt failed before authority expired")
            factory.execution.complete_batch.side_effect = failure
        else:
            factory.execution.complete_aggregation_result.side_effect = failure

        with pytest.raises(type(failure)) as caught:
            executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        assert caught.value is failure
        factory.execution.complete_node_state.assert_not_called()
        assert factory.execution.complete_batch.call_count == int(during_cleanup)
        assert executor.get_buffer_count(nid) == 1

    def test_get_buffered_rows_returns_data(self) -> None:
        """get_buffered_rows returns the buffered row dicts."""
        executor, _, nid = self._make_agg_executor()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1"))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2"))

        rows = executor.get_buffered_rows(nid)
        assert len(rows) == 2
        assert rows[0]["value"] == "a"
        assert rows[1]["value"] == "b"

    def test_get_buffered_rows_unconfigured_raises(self) -> None:
        executor, _, _ = self._make_agg_executor()
        with pytest.raises(OrchestrationInvariantError):
            executor.get_buffered_rows(NodeID("unknown"))

    def test_get_buffered_tokens_returns_tokens(self) -> None:
        """get_buffered_tokens returns TokenInfo objects."""
        executor, _, nid = self._make_agg_executor()
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))

        tokens = executor.get_buffered_tokens(nid)
        assert len(tokens) == 1
        assert tokens[0].token_id == "t1"

    def test_get_buffered_tokens_unconfigured_raises(self) -> None:
        executor, _, _ = self._make_agg_executor()
        with pytest.raises(OrchestrationInvariantError):
            executor.get_buffered_tokens(NodeID("unknown"))

    def test_empty_buffer_returns_empty_list(self) -> None:
        """Configured node with no rows returns empty list."""
        executor, _, nid = self._make_agg_executor()
        assert executor.get_buffered_rows(nid) == []
        assert executor.get_buffered_tokens(nid) == []

    # --- get_buffer_count ---

    def test_get_buffer_count_correct(self) -> None:
        executor, _, nid = self._make_agg_executor()
        assert executor.get_buffer_count(nid) == 0
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        assert executor.get_buffer_count(nid) == 1
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t2"))
        assert executor.get_buffer_count(nid) == 2

    def test_get_buffer_count_unconfigured_raises(self) -> None:
        executor, _, _ = self._make_agg_executor()
        with pytest.raises(OrchestrationInvariantError):
            executor.get_buffer_count(NodeID("unknown"))

    # --- should_flush ---

    def test_should_flush_delegates_to_trigger_evaluator(self) -> None:
        """should_flush delegates to trigger evaluator (count=3, need 3 rows)."""
        executor, _, nid = self._make_agg_executor(count=3)
        assert executor.should_flush(nid) is False
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t2"))
        assert executor.should_flush(nid) is False
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t3"))
        assert executor.should_flush(nid) is True

    def test_should_flush_unconfigured_raises(self) -> None:
        executor, _, _ = self._make_agg_executor()
        with pytest.raises(OrchestrationInvariantError):
            executor.should_flush(NodeID("unknown"))

    # --- get_trigger_type ---

    def test_get_trigger_type_returns_none_before_fire(self) -> None:
        executor, _, nid = self._make_agg_executor(count=10)
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        assert executor.get_trigger_type(nid) is None

    def test_get_trigger_type_returns_count_after_fire(self) -> None:
        executor, _, nid = self._make_agg_executor(count=2)
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t2"))
        # should_flush() must be called first to evaluate triggers
        # (sets _last_triggered on the evaluator)
        assert executor.should_flush(nid) is True
        assert executor.get_trigger_type(nid) == TriggerType.COUNT

    def test_get_trigger_type_unconfigured_raises(self) -> None:
        executor, _, _ = self._make_agg_executor()
        with pytest.raises(OrchestrationInvariantError):
            executor.get_trigger_type(NodeID("unknown"))

    # --- execute_flush ---

    def test_execute_flush_no_batch_raises_orchestration_invariant_error(self) -> None:
        """Flushing without a batch raises OrchestrationInvariantError."""
        executor, _, nid = self._make_agg_executor()
        transform = _make_aggregation_transform()
        ctx = make_context()

        with pytest.raises(OrchestrationInvariantError, match="No batch exists"):
            executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

    def test_execute_flush_empty_buffer_raises_before_execution(self) -> None:
        """An opened batch cannot execute before any adopted row reaches its buffer."""
        executor, factory, nid = self._make_agg_executor()
        executor.open_batch_membership(nid, coordination_token=_AGGREGATION_LEADER)
        transform = _make_aggregation_transform()

        with pytest.raises(OrchestrationInvariantError, match="Cannot flush empty buffer for node agg_1"):
            executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        factory.execution.update_batch_status.assert_not_called()
        transform.process.assert_not_called()

    def test_execute_flush_success_completes_batch_and_state(self) -> None:
        """Successful flush commits node, batch, and result receipt in ONE atomic call.

        Every successful aggregation completion owns a durable result receipt:
        the node state, batch completion, ordered output payloads, and exact
        member actions commit together via complete_aggregation_result — never
        through the legacy complete_batch/complete_node_state pair.
        """
        executor, factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()

        # Buffer two rows
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))

        # Mock batch transform
        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.success(
            make_row({"value": "aggregated"}, contract=contract),
            success_reason={"action": "aggregated"},
        )
        ctx = make_context()

        result, tokens, batch_id = executor.execute_flush(
            nid,
            transform,
            ctx,
            TriggerType.COUNT,
        )

        assert result.status == "success"
        assert len(tokens) == 2
        assert batch_id == "batch_001"

        factory.execution.complete_aggregation_result.assert_called_once()
        receipt_kwargs = factory.execution.complete_aggregation_result.call_args.kwargs
        assert receipt_kwargs["batch_id"] == "batch_001"
        assert receipt_kwargs["coordination_token"] is ctx.require_coordination_token()
        assert receipt_kwargs["aggregation_node_id"] == "agg_1"
        assert receipt_kwargs["state_id"] == "state_001"
        assert receipt_kwargs["trigger_type"] == TriggerType.COUNT
        assert receipt_kwargs["output_mode"] is OutputMode.TRANSFORM
        assert receipt_kwargs["output_shape"] == "single"
        assert receipt_kwargs["output_rows"] == (result.row,)
        assert_stable_hash(receipt_kwargs["output_hash"], result.row)
        assert receipt_kwargs["expansion_parent_token_id"] == "t1"
        assert receipt_kwargs["success_reason"] == {"action": "aggregated"}
        assert [(member.member_ref.token_id, member.action, member.error_hash) for member in receipt_kwargs["members"]] == [
            ("t1", AggregationMemberAction.CONSUME_BATCH, None),
            ("t2", AggregationMemberAction.CONSUME_BATCH, None),
        ]
        # No separate legacy completion writes on success — replaying them
        # would break the single-transaction receipt guarantee.
        factory.execution.complete_batch.assert_not_called()
        factory.execution.complete_node_state.assert_not_called()

    def test_execute_flush_success_passes_aggregation_flush_context(self) -> None:
        """Successful flush passes AggregationFlushContext as context_after."""
        from elspeth.contracts.node_state_context import AggregationFlushContext

        executor, factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()

        # Buffer two rows
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))

        # Mock batch transform
        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.success(
            make_row({"value": "aggregated"}, contract=contract),
            success_reason={"action": "aggregated"},
        )
        ctx = make_context()

        executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        # The flush context rides the atomic result receipt; the success path
        # never issues a separate complete_node_state write.
        factory.execution.complete_aggregation_result.assert_called_once()
        factory.execution.complete_node_state.assert_not_called()

        context_after = factory.execution.complete_aggregation_result.call_args.kwargs["context_after"]
        assert isinstance(context_after, AggregationFlushContext)
        assert context_after.trigger_type == "count"
        assert context_after.buffer_size == 2
        assert context_after.batch_id == "batch_001"
        assert context_after.flush_index == 1
        assert context_after.rows_seen_total == 2
        assert context_after.row_start == 1
        assert context_after.row_end == 2
        assert context_after.is_end_of_source is False

    # --- Tier-2 contract violations fail the batch (operator ruling 2026-09-23, B2) ---

    @staticmethod
    def _verdict_kwargs(factory: MagicMock) -> dict[str, Any]:
        """The one FAILED-verdict write of a failed flush, and proof it was the only failure write.

        ``complete_aggregation_failure`` records the transform_errors rows, the
        DIVERT, the FAILED state and the FAILED batch in ONE transaction, so no
        separate node_state, batch, routing or transform_errors write may exist.
        """
        factory.execution.complete_aggregation_failure.assert_called_once()
        factory.execution.record_routing_event.assert_not_called()
        assert not [c for c in factory.execution.complete_node_state.call_args_list if c.kwargs.get("status") == NodeStateStatus.FAILED]
        assert not [c for c in factory.execution.complete_batch.call_args_list if c.kwargs.get("status") == BatchStatus.FAILED]
        return dict(factory.execution.complete_aggregation_failure.call_args.kwargs)

    @classmethod
    def _assert_contract_violation_routed(cls, factory: MagicMock, result: TransformResult, *, error_prefix: str) -> None:
        """A violation raised before completion is a failed batch, recorded once.

        The executor turns it into an error result (``reason="contract_violation"``,
        not retryable) and ``_complete_error_flush`` records it exactly as a
        returned error: ONE verdict write carrying the reason dict.
        """
        assert result.status == "error"
        assert result.retryable is False
        assert result.reason is not None
        assert result.reason["reason"] == "contract_violation"
        assert result.reason["error"].startswith(error_prefix)
        factory.execution.complete_aggregation_result.assert_not_called()
        assert cls._verdict_kwargs(factory)["reason"] == result.reason

    def test_a_buffered_row_failing_the_input_contract_fails_the_batch_before_process(self) -> None:
        """Schema-invalid buffered rows never reach the plugin; the batch fails, the run goes on."""
        from elspeth.contracts import PluginSchema

        class StrictInputSchema(PluginSchema):
            count: int

        executor, factory, nid = self._make_agg_executor(count=1)
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"count": "42"}, token_id="t1", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        transform.input_schema = StrictInputSchema
        transform.process.return_value = TransformResult.success(
            make_row({"value": "unused"}, contract=contract),
            success_reason={"action": "unused"},
        )

        result, tokens, _batch_id = executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        transform.process.assert_not_called()
        assert [token.token_id for token in tokens] == ["t1"]
        self._assert_contract_violation_routed(
            factory, result, error_prefix="Aggregation transform 'agg_transform' input validation failed for buffered row 0: "
        )
        assert executor.get_buffer_count(nid) == 0

    def test_the_routed_input_violation_names_the_row_and_type_never_the_value(self) -> None:
        from elspeth.contracts import PluginSchema

        class StrictInputSchema(PluginSchema):
            count: int

        sentinel = "SENTINEL-value-7f3a91"
        executor, factory, nid = self._make_agg_executor(count=3, on_error="quarantine", error_edge_ids={NodeID("agg_1"): "edge_err_1"})
        contract = _make_contract()
        for index, value in enumerate((1, sentinel, 3)):
            _accept_adopted_aggregation_row(executor, nid, _make_token(data={"count": value}, token_id=f"t{index}", contract=contract))
        transform = _make_aggregation_transform("agg_transform")
        transform.input_schema = StrictInputSchema

        result, _tokens, _batch_id = executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        self._assert_contract_violation_routed(
            factory, result, error_prefix="Aggregation transform 'agg_transform' input validation failed for buffered row 1: "
        )
        assert result.reason is not None
        assert "count: " in result.reason["error"]
        assert "[int_type]" in result.reason["error"]
        assert sentinel not in repr(result.reason)
        verdict = self._verdict_kwargs(factory)
        assert verdict["divert_edge_id"] == "edge_err_1"
        assert verdict["destination"] == "quarantine"
        assert verdict["reason"] == result.reason

    @pytest.mark.parametrize("declared", [True, False], ids=["declared-field", "undeclared-field"])
    def test_the_routed_non_canonical_output_violation_names_no_emitted_value(self, declared: bool) -> None:
        """B2 routes this violation, so its text reaches every audit write of the failed batch.

        ``rfc8785`` renders an out-of-range integer in its own error text;
        the reason must carry the exception type, row and declared field only.
        """
        from elspeth.contracts import PluginSchema

        class DeclaredOutput(PluginSchema):
            total: int

        bigint = 2**60 + 12345
        executor, factory, nid = self._make_agg_executor(count=1, on_error="quarantine", error_edge_ids={NodeID("agg_1"): "edge_err_1"})
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": 1}, token_id="t1", contract=contract))
        transform = _make_aggregation_transform("agg_transform")
        if declared:
            transform.output_schema = DeclaredOutput
        transform.process.return_value = TransformResult.success(
            make_row({"total": bigint}, contract=contract), success_reason={"action": "aggregated"}
        )

        result, _tokens, _batch_id = executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        located = "emitted row 0 field 'total'" if declared else "emitted row 0, in a field its output schema does not declare"
        self._assert_contract_violation_routed(
            factory, result, error_prefix=f"Aggregation transform 'agg_transform' emitted non-canonical data at {located} ("
        )
        written = (result.reason, self._verdict_kwargs(factory)["reason"])
        assert str(bigint) not in repr(written)

    def test_a_contract_violation_raised_by_the_batch_plugin_fails_the_batch_once(self) -> None:
        """The plugin's own Tier-2 violation leaves the state open for the route to complete.

        ``_invoke_batch_transform`` must not complete FAILED first: a second
        completion of the same state would be refused.
        """
        executor, factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))
        transform = _make_aggregation_transform("agg_transform")
        transform.process.side_effect = PluginContractViolation("output field 'x' would overwrite an input field in row 1")

        result, _tokens, _batch_id = executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        self._assert_contract_violation_routed(factory, result, error_prefix="output field 'x' would overwrite")

    def test_a_tier_1_contract_violation_from_the_batch_plugin_still_aborts(self) -> None:
        """``SinkTransactionalInvariantError`` is a PluginContractViolation AND Tier 1.

        Registration in ``TIER_1_ERRORS`` is what opts it out of routing
        (ADR-008), so the Tier-1 re-raise must come before the PCV arm.
        """
        executor, factory, nid = self._make_agg_executor(count=2, on_error="quarantine", error_edge_ids={NodeID("agg_1"): "edge_err_1"})
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))
        transform = _make_aggregation_transform("agg_transform")
        transform.process.side_effect = SinkTransactionalInvariantError("commit boundary diverged")

        with pytest.raises(SinkTransactionalInvariantError, match="commit boundary diverged"):
            executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        factory.execution.complete_aggregation_failure.assert_not_called()
        factory.execution.record_routing_event.assert_not_called()
        failed = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        assert failed["error"].exception_type == "SinkTransactionalInvariantError"

    def test_a_violation_from_the_cross_check_callback_still_aborts(self) -> None:
        """The processor's declaration cross-check writes each member's terminal
        before it raises, so routing its violation would be a second terminal."""
        executor, factory, nid = self._make_agg_executor(count=1, on_error="quarantine", error_edge_ids={NodeID("agg_1"): "edge_err_1"})
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.success(
            make_row({"value": "aggregated"}, contract=contract),
            success_reason={"action": "aggregated"},
        )

        def cross_check(result: TransformResult, tokens: Sequence[TokenInfo], batch_id: str) -> None:
            raise PluginContractViolation("cross-check recorded every member and raised")

        with pytest.raises(PluginContractViolation, match="cross-check recorded"):
            executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT, validate_success=cross_check)

        factory.execution.complete_aggregation_failure.assert_not_called()
        factory.execution.record_routing_event.assert_not_called()
        failed_batches = [c for c in factory.execution.complete_batch.call_args_list if c.kwargs.get("status") == BatchStatus.FAILED]
        assert len(failed_batches) == 1

    def test_a_success_result_failing_the_output_contract_fails_the_batch(self) -> None:
        """Schema-invalid batch output fails the batch before any completion."""
        from elspeth.contracts import PluginSchema

        class StrictOutputSchema(PluginSchema):
            count: int

        executor, factory, nid = self._make_agg_executor(count=1)
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        transform.output_schema = StrictOutputSchema
        transform.process.return_value = TransformResult.success(
            make_row({"count": "42"}, contract=contract),
            success_reason={"action": "aggregated"},
        )

        result, _tokens, _batch_id = executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        self._assert_contract_violation_routed(
            factory, result, error_prefix="Aggregation transform 'agg_transform' output validation failed for emitted row 0: "
        )
        assert all(c.kwargs.get("status") != NodeStateStatus.COMPLETED for c in factory.execution.complete_node_state.call_args_list)

    @pytest.mark.parametrize(("expected", "emitted", "fails"), [(5, 1, True), (1, 2, True), (2, 2, False)])
    def test_an_expected_output_count_mismatch_fails_the_batch(self, expected: int, emitted: int, fails: bool) -> None:
        """B2 moved the count check into the routable region: a mismatch is a failed batch.

        Without it the flush would record the batch COMPLETED and the
        processor's own count check would then abort the run.
        """
        executor, factory, nid = self._make_agg_executor(count=1, expected_output_count=expected)
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        transform = _make_aggregation_transform("agg_transform")
        rows = [make_row({"value": index}, contract=contract) for index in range(emitted)]
        transform.process.return_value = (
            TransformResult.success(rows[0], success_reason={"action": "aggregated"})
            if emitted == 1
            else TransformResult.success_multi(rows, success_reason={"action": "aggregated"})
        )

        result, _tokens, _batch_id = executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        if not fails:
            assert result.status == "success", "control: a matching count completes the batch"
            factory.execution.complete_aggregation_failure.assert_not_called()
            return
        self._assert_contract_violation_routed(
            factory,
            result,
            error_prefix=f"Aggregation 'test_agg' produced {emitted} output row(s), but expected_output_count={expected}.",
        )

    def test_post_invocation_output_validation_failure_marks_aggregation_span_error(self) -> None:
        """The aggregation span covers output validation; a failed batch marks it ERROR."""
        from elspeth.contracts import PluginSchema

        class StrictOutputSchema(PluginSchema):
            count: int

        events: list[EngineSpanCompleted] = []
        spans = SpanFactory(telemetry_emit=events.append)
        executor, _factory, nid = self._make_agg_executor(count=1, span_factory=spans)
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1"))
        transform = _make_aggregation_transform("agg_transform")
        transform.output_schema = StrictOutputSchema
        transform.process.return_value = TransformResult.success(
            make_row({"count": "not-an-int"}, contract=_make_contract()),
            success_reason={"action": "aggregated"},
        )

        with spans.trace_scope("test-run", datetime.now(UTC)):
            result, _tokens, _batch_id = executor.execute_flush(nid, transform, make_context(run_id="test-run"), TriggerType.COUNT)

        assert result.status == "error"
        assert len(events) == 1
        assert events[0].name is EngineSpanName.AGGREGATION
        assert events[0].status is EngineSpanStatus.ERROR
        assert events[0].exception_type == "AggregationResultError"

    def test_execute_flush_error_result_marks_batch_failed(self) -> None:
        """Error result from transform records the batch's FAILED verdict, once."""
        executor, factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.error(
            reason={"reason": "test_error"},
        )
        ctx = make_context()

        result, _tokens, batch_id = executor.execute_flush(
            nid,
            transform,
            ctx,
            TriggerType.COUNT,
        )

        assert result.status == "error"
        verdict = self._verdict_kwargs(factory)
        assert verdict["batch_id"] == batch_id
        assert verdict["trigger_type"] is TriggerType.COUNT

    def test_handled_aggregation_error_result_marks_aggregation_span_error(self) -> None:
        """A TransformResult.error marks the flush span even though routing returns."""
        events: list[EngineSpanCompleted] = []
        spans = SpanFactory(telemetry_emit=events.append)
        executor, _factory, nid = self._make_agg_executor(count=1, span_factory=spans)
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1"))
        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.error(reason={"reason": "rejected"})

        with spans.trace_scope("test-run", datetime.now(UTC)):
            result, _tokens, _batch_id = executor.execute_flush(
                nid,
                transform,
                make_context(run_id="test-run"),
                TriggerType.COUNT,
            )

        assert result.status == "error"
        assert len(events) == 1
        assert events[0].status is EngineSpanStatus.ERROR
        assert events[0].exception_type == "AggregationResultError"

    # --- failed-flush error route (elspeth-d2e3f29d10, B5) ---

    _SECRET_SHAPED = "sk-" + "A" * 40

    def _flush_error_result(
        self, executor: AggregationExecutor, nid: NodeID, *, reason: dict[str, Any] | None = None
    ) -> tuple[TransformResult, list[TokenInfo]]:
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", row_id="r1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", row_id="r2", contract=contract))
        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.error(
            reason=reason
            if reason is not None
            else {"reason": "invalid_input", "field": "value", "error": f"bad batch; leaked {self._SECRET_SHAPED}"},
        )
        result, tokens, _batch_id = executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)
        return result, tokens

    def test_named_on_error_records_the_verdict_with_its_divert_on_the_flush_state_in_one_write(self) -> None:
        """A named aggregation on_error's DIVERT rides the batch's ONE verdict
        write, bound to the flush node_state, with the transform_errors rows,
        the FAILED state and the FAILED batch (C4: no crash can split them)."""
        executor, factory, nid = self._make_agg_executor(count=2, on_error="quarantine", error_edge_ids={NodeID("agg_1"): "edge_err_1"})

        result, _tokens = self._flush_error_result(executor, nid)

        verdict = self._verdict_kwargs(factory)
        assert verdict["state_id"] == "state_001"
        assert verdict["divert_edge_id"] == "edge_err_1"
        assert verdict["destination"] == "quarantine"
        assert verdict["reason"] == result.reason
        assert verdict["trigger_type"] is TriggerType.COUNT
        assert verdict["duration_ms"] >= 0

    def test_failed_flush_reason_is_scrubbed_and_written_back(self) -> None:
        """The scrubbed reason replaces result.reason (the processor builds the
        routed FailureInfo, and so pending_error_message, from it) and is the
        dict stored on the FAILED node_state — never a Python repr."""
        executor, factory, nid = self._make_agg_executor(count=2, on_error="quarantine", error_edge_ids={NodeID("agg_1"): "edge_err_1"})

        result, _tokens = self._flush_error_result(executor, nid)

        assert result.reason is not None
        assert self._SECRET_SHAPED not in repr(result.reason)
        assert result.reason["reason"] == "invalid_input"
        assert result.reason["field"] == "value"
        assert self._verdict_kwargs(factory)["reason"] == result.reason

    @pytest.mark.parametrize("on_error", ["quarantine", "discard"])
    def test_failed_flush_records_one_transform_error_per_buffered_token(self, on_error: str) -> None:
        """Every buffered member is in the verdict with its own row and the
        scrubbed batch reason, in ONE leader-fenced write, for BOTH
        dispositions (B5). destination is the on_error value."""
        edge_ids = {NodeID("agg_1"): "edge_err_1"} if on_error != "discard" else None
        executor, factory, nid = self._make_agg_executor(count=2, on_error=on_error, error_edge_ids=edge_ids)

        result, tokens = self._flush_error_result(executor, nid)

        recorded = self._verdict_kwargs(factory)
        assert [(ref.token_id, ref.run_id) for ref, _row in recorded["members"]] == [("t1", "test-run"), ("t2", "test-run")]
        assert [row for _ref, row in recorded["members"]] == [token.row_data for token in tokens]
        assert recorded["aggregation_node_id"] == "agg_1"
        assert recorded["reason"] == result.reason
        assert recorded["destination"] == on_error
        assert recorded["coordination_token"] == _AGGREGATION_LEADER

    def test_discard_on_error_records_no_routing_event(self) -> None:
        executor, factory, nid = self._make_agg_executor(count=2, on_error="discard")

        self._flush_error_result(executor, nid)

        assert self._verdict_kwargs(factory)["divert_edge_id"] is None

    def test_named_on_error_without_a_divert_edge_fails_closed_before_any_write(self) -> None:
        """No __error_<name>__ edge for a named sink is a builder/orchestration
        bug: refuse before writing half an envelope."""
        executor, factory, nid = self._make_agg_executor(count=2, on_error="quarantine", error_edge_ids={})

        with pytest.raises(OrchestrationInvariantError, match="DIVERT edge"):
            self._flush_error_result(executor, nid)

        factory.execution.complete_aggregation_failure.assert_not_called()
        factory.execution.record_routing_event.assert_not_called()

    def test_failed_flush_with_no_reason_is_an_invariant_violation(self) -> None:
        """An error result without a reason cannot be routed or recorded
        honestly; the executor refuses rather than fabricating one."""
        executor, factory, nid = self._make_agg_executor(count=2, on_error="discard")
        contract = _make_contract()
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))
        result = TransformResult.error(reason={"reason": "invalid_input"})
        object.__setattr__(result, "reason", None)
        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = result

        with pytest.raises(OrchestrationInvariantError, match="reason is None"):
            executor.execute_flush(nid, transform, make_context(), TriggerType.COUNT)

        factory.execution.complete_aggregation_failure.assert_not_called()

    def test_execute_flush_exception_marks_batch_failed_and_reraises(self) -> None:
        """Exception from transform marks batch as FAILED and re-raises."""
        executor, factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        transform.process.side_effect = RuntimeError("transform crash")
        ctx = make_context()

        with pytest.raises(RuntimeError, match="transform crash"):
            executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        # Verify batch marked failed
        failed_calls = [c for c in factory.execution.complete_batch.call_args_list if c[1].get("status") == BatchStatus.FAILED]
        assert len(failed_calls) == 1

    def test_execute_flush_scrubs_plugin_exception_message_in_failed_state(self) -> None:
        """Secret-bearing plugin exception text must not be persisted in audit errors."""
        executor, factory, nid = self._make_agg_executor(count=1)
        contract = _make_contract()
        secret = "sk-" + ("a" * 32)

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        transform.process.side_effect = RuntimeError(f"Authorization: Bearer {secret}")
        ctx = make_context()

        with pytest.raises(RuntimeError) as exc_info:
            executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        assert secret in str(exc_info.value)
        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        error = failed_kwargs["error"]
        assert error.exception == "<redacted-secret>"
        assert secret not in str(error.to_dict())

    def test_execute_flush_exception_clears_batch_token_ids(self) -> None:
        """Exception path clears ctx.batch_token_ids to prevent stale leakage.

        Regression: PluginContext is reused across calls. If execute_flush()
        raises without clearing batch_token_ids, subsequent transforms
        (e.g. OpenRouterBatchLLMTransform) see stale IDs and misattribute
        telemetry to tokens from the failed batch.
        """
        executor, _factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"v": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"v": "b"}, token_id="t2", contract=contract))

        transform = _make_aggregation_transform("agg")
        transform.process.side_effect = RuntimeError("boom")
        ctx = make_context()

        with pytest.raises(RuntimeError, match="boom"):
            executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        # batch_token_ids must be None after error, not stale ["t1", "t2"]
        assert ctx.batch_token_ids is None

    def test_execute_flush_exposes_aggregation_batch_context_to_transform(self) -> None:
        """ctx.aggregation_batch is populated for batch-aware transforms during flush.

        The batch context carries durable pagination metadata (flush_index,
        row_start/end, rows_seen_total, is_end_of_source) that batch-aware
        transforms (e.g. ReportAssembler) need in order to emit truthful
        pagination columns. Counters are owned by AggregationExecutor (not the
        transform) so they survive checkpoint round-trip.

        After the flush returns, ctx.aggregation_batch must be cleared so that
        subsequent transforms outside the aggregation don't see stale state.
        """
        from elspeth.contracts.node_state_context import AggregationBatchContext
        from elspeth.contracts.plugin_context import PluginContext

        captured: list[AggregationBatchContext] = []

        class _CapturingBatchTransform:
            """Tiny batch-aware transform that snapshots ctx.aggregation_batch."""

            @property
            def declared_read_fields(self) -> frozenset[str]:
                # BatchTransformProtocol spelling surface: this fake declares its required input columns only.
                return self.schema_required_input_fields()

            name = "capturing_agg"
            input_schema = _PermissiveSchema
            output_schema = _PermissiveSchema
            _output_schema_config: SchemaConfig | None = None

            def schema_required_input_fields(self) -> frozenset[str]:
                return frozenset()

            def process(self, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
                # ctx.aggregation_batch must be set by AggregationExecutor before process()
                if ctx.aggregation_batch is None:
                    raise AssertionError("ctx.aggregation_batch was None during transform.process()")
                captured.append(ctx.aggregation_batch)
                return TransformResult.success(
                    make_row({"agg": "ok"}, contract=rows[0].contract),
                    success_reason={"action": "agg"},
                )

        executor, _factory, nid = self._make_agg_executor(count=3)
        contract = _make_contract()

        ctx = make_context()

        # === First flush: 3 rows ===
        for i, label in enumerate(("a", "b", "c"), start=1):
            _accept_adopted_aggregation_row(executor, nid, _make_token(data={"v": label}, token_id=f"t{i}", contract=contract))

        transform = _CapturingBatchTransform()
        _result, _tokens, flushed_batch_id = executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        # ctx must be cleared after success
        assert ctx.aggregation_batch is None

        assert len(captured) == 1
        first = captured[0]
        assert first.trigger_type == "count"
        assert first.batch_id == flushed_batch_id
        assert first.batch_size == 3
        assert first.flush_index == 1
        assert first.rows_seen_total == 3
        assert first.row_start == 1
        assert first.row_end == 3
        assert first.is_end_of_source is False

        # === Second flush: another 3 rows ===
        for i, label in enumerate(("d", "e", "f"), start=4):
            _accept_adopted_aggregation_row(executor, nid, _make_token(data={"v": label}, token_id=f"t{i}", contract=contract))

        executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        assert ctx.aggregation_batch is None
        assert len(captured) == 2
        second = captured[1]
        assert second.flush_index == 2
        assert second.rows_seen_total == 6
        assert second.row_start == 4
        assert second.row_end == 6

    def test_execute_flush_clears_aggregation_batch_context_on_failure(self) -> None:
        """On a failed flush, ctx.aggregation_batch is cleared in the failure cleanup path.

        Mirrors test_execute_flush_exception_clears_batch_token_ids — exception
        path must not leave aggregation_batch dangling on the shared ctx.
        """
        executor, _factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"v": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"v": "b"}, token_id="t2", contract=contract))

        transform = _make_aggregation_transform("agg")
        transform.process.side_effect = RuntimeError("boom")
        ctx = make_context()

        with pytest.raises(RuntimeError, match="boom"):
            executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        assert ctx.aggregation_batch is None

    def test_aggregation_batch_context_resumes_pagination_after_journal_restore(self) -> None:
        """After restore, the next flush emits flush_index = M+1 and rows_seen_total = N + new_rows.

        Headline guarantee of the durable-counter design: pagination metadata
        derived from AggregationExecutor counters must survive resume. Post-F1
        the counters derive from audit tables (Task 3.1) and arrive via
        restore_from_journal. This exercises flush→counter-only-restore→flush:

        1. Executor A flushes a batch of 3 rows successfully (accepted_count_total=3,
           completed_flush_count=1, buffer empty).
        2. Fresh executor B restores the audit-derived counters via
           restore_from_journal with no journal items (counter-only node).
        3. Buffer 2 new rows and flush. AggregationBatchContext seen by the
           transform must report flush_index=2, rows_seen_total=5, row_start=4,
           row_end=5, batch_size=2.
        """
        from elspeth.contracts.node_state_context import AggregationBatchContext
        from elspeth.contracts.plugin_context import PluginContext

        # === Phase 1: Executor A flushes 3 rows successfully ===
        executor_a, factory, nid = self._make_agg_executor(count=3)
        contract = _make_contract()
        ctx = make_context()

        for i, label in enumerate(("a", "b", "c"), start=1):
            _accept_adopted_aggregation_row(executor_a, nid, _make_token(data={"v": label}, token_id=f"t{i}", contract=contract))

        transform_a = _make_aggregation_transform("agg")
        transform_a.process.return_value = TransformResult.success(
            make_row({"v": "agg1"}, contract=contract),
            success_reason={"action": "agg"},
        )
        executor_a.execute_flush(nid, transform_a, ctx, TriggerType.COUNT)

        # === Phase 2: Fresh executor B restores audit-derived counters (no journal items) ===
        executor_b = AggregationExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            run_id="test-run",
            aggregation_settings={
                nid: AggregationSettings(
                    name="test_agg",
                    plugin="batch_stats",
                    input="default",
                    on_error="discard",
                    trigger=TriggerConfig(count=2),
                )
            },
        )
        executor_b.restore_from_journal(
            node_id=nid,
            items=[],
            member_order=[],
            batch_id=None,
            accepted_count_total=3,
            completed_flush_count=1,
            scalars=AggregationNodeScalars(None, None),
            attempt_offsets={},
            resume_checkpoint_id="cp-0",
            now=_JOURNAL_T0,
        )
        assert executor_b.get_buffer_count(nid) == 0  # No buffered tokens restored

        # === Phase 4: Buffer 2 new rows on B, flush, capture ctx.aggregation_batch ===
        captured: list[AggregationBatchContext] = []

        class _CapturingBatchTransform:
            name = "capturing_agg"

            @property
            def declared_read_fields(self) -> frozenset[str]:
                # BatchTransformProtocol spelling surface: this fake declares its required input columns only.
                return self.schema_required_input_fields()

            input_schema = _PermissiveSchema
            output_schema = _PermissiveSchema
            _output_schema_config: SchemaConfig | None = None

            def schema_required_input_fields(self) -> frozenset[str]:
                return frozenset()

            def process(self, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
                if ctx.aggregation_batch is None:
                    raise AssertionError("ctx.aggregation_batch was None during transform.process()")
                captured.append(ctx.aggregation_batch)
                return TransformResult.success(
                    make_row({"v": "agg2"}, contract=rows[0].contract),
                    success_reason={"action": "agg"},
                )

        for i, label in enumerate(("d", "e"), start=4):
            _accept_adopted_aggregation_row(executor_b, nid, _make_token(data={"v": label}, token_id=f"t{i}", contract=contract))

        executor_b.execute_flush(nid, _CapturingBatchTransform(), ctx, TriggerType.COUNT)

        # === Phase 5: Pagination continues correctly ===
        assert len(captured) == 1
        post = captured[0]
        assert post.flush_index == 2, "completed_flush_count restored as 1 → next flush is the 2nd"
        assert post.rows_seen_total == 5, "accepted_count_total restored as 3 + 2 new rows = 5"
        assert post.row_start == 4
        assert post.row_end == 5
        assert post.batch_size == 2

    def test_execute_flush_resets_batch_state(self) -> None:
        """After flush, batch state is reset (new batch on next row)."""
        executor, _factory, nid = self._make_agg_executor(count=1)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1", contract=contract))

        transform = _make_aggregation_transform("agg")
        transform.process.return_value = TransformResult.success(
            make_row({"value": "agg"}, contract=contract),
            success_reason={"action": "agg"},
        )
        ctx = make_context()

        executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        # Batch ID should be None after flush
        assert executor.get_batch_id(nid) is None
        # Buffer should be empty
        assert executor.get_buffer_count(nid) == 0

    # --- get_batch_id ---

    def test_get_batch_id_none_before_rows(self) -> None:
        executor, _, nid = self._make_agg_executor()
        assert executor.get_batch_id(nid) is None

    def test_get_batch_id_set_after_first_row(self) -> None:
        executor, _, nid = self._make_agg_executor()
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        assert executor.get_batch_id(nid) == "batch_001"

    # --- check_flush_status ---

    def test_check_flush_status_combined_operation(self) -> None:
        """check_flush_status returns (bool, TriggerType|None) tuple."""
        executor, _, nid = self._make_agg_executor(count=2)

        should_flush, trigger = executor.check_flush_status(nid)
        assert should_flush is False
        assert trigger is None

        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t2"))

        should_flush, trigger = executor.check_flush_status(nid)
        assert should_flush is True
        assert trigger == TriggerType.COUNT

    def test_check_flush_status_unconfigured_raises(self) -> None:
        executor, _, _ = self._make_agg_executor()
        with pytest.raises(OrchestrationInvariantError):
            executor.check_flush_status(NodeID("unknown"))

    # --- _get_node (unified validation) ---

    def test_get_node_unknown_raises_orchestration_invariant(self) -> None:
        """Unknown node_id always raises OrchestrationInvariantError with diagnostics."""
        executor, _, _ = self._make_agg_executor()
        unknown = NodeID("not_configured")
        with pytest.raises(OrchestrationInvariantError, match="not_configured"):
            executor.get_batch_id(unknown)
        with pytest.raises(OrchestrationInvariantError, match="not_configured"):
            executor.restore_from_journal(
                node_id=unknown,
                items=[],
                member_order=[],
                batch_id=None,
                accepted_count_total=0,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    # --- PipelineRow handling (merged from test_executor_pipeline_row.py) ---

    def test_buffer_row_stores_dict_not_pipeline_row(self) -> None:
        """AggregationExecutor should store dicts in buffer, not PipelineRow.

        Internal buffer must be JSON-serializable for checkpoints.
        """
        executor, _, nid = self._make_agg_executor(count=10)
        contract = _make_contract()
        token = _make_token(data={"value": "test"}, contract=contract)

        _accept_adopted_aggregation_row(executor, nid, token)

        buffered = executor.get_buffered_rows(nid)
        assert len(buffered) == 1
        assert isinstance(buffered[0], dict)
        assert type(buffered[0]) is not PipelineRow  # type: ignore[comparison-overlap, unreachable]
        assert buffered[0] == {"value": "test"}

    def test_buffer_row_extracts_dict_from_pipeline_row(self) -> None:
        """buffer_row should extract dict from PipelineRow, preserving all fields."""
        executor, _, nid = self._make_agg_executor(count=10)
        contract = _make_contract()
        row_data = {"value": "test", "extra": "field", "number": 42}
        token = _make_token(data=row_data, contract=contract)

        _accept_adopted_aggregation_row(executor, nid, token)

        buffered = executor.get_buffered_rows(nid)
        assert buffered[0] == {"value": "test", "extra": "field", "number": 42}

    def test_buffer_tokens_preserves_pipeline_row(self) -> None:
        """TokenInfo in buffer_tokens should keep PipelineRow as row_data."""
        executor, _, nid = self._make_agg_executor(count=10)
        contract = _make_contract()
        token = _make_token(data={"value": "test"}, contract=contract)

        _accept_adopted_aggregation_row(executor, nid, token)

        buffered_tokens = executor.get_buffered_tokens(nid)
        assert len(buffered_tokens) == 1
        assert isinstance(buffered_tokens[0].row_data, PipelineRow)
        assert buffered_tokens[0].row_data.contract is contract

    # --- restore_from_journal (F1: buffers rebuild from BLOCKED journal rows) ---

    def test_restore_from_journal_rebuilds_buffer_and_attempt_offset(self) -> None:
        """Journal items rebuild tokens/buffers in member_order with typed-value fidelity.

        Payloads carry datetime AND Decimal values — proves the journal payload
        round-trip (serialize_row_payload → deserialize_row_payload) preserves
        type fidelity into the restored buffer.
        """
        clock = MockClock(start=100.0)
        executor, _, nid = self._make_agg_executor(node_id="agg-1", count=10, clock=clock)
        t0 = _JOURNAL_T0
        items = [
            _blocked_item(
                token_id="t1",
                row_id="r1",
                node_id="agg-1",
                payload=_journal_payload({"v": 1, "at": datetime(2026, 6, 1, tzinfo=UTC), "amount": Decimal("1.10")}),
                blocked_at=t0,
            ),
            _blocked_item(
                token_id="t2",
                row_id="r2",
                node_id="agg-1",
                payload=_journal_payload({"v": 2, "at": datetime(2026, 6, 2, tzinfo=UTC), "amount": Decimal("2.25")}),
                blocked_at=t0 + timedelta(seconds=5),
                ingest_sequence=1,
            ),
        ]

        executor.restore_from_journal(
            node_id=nid,
            items=items,
            member_order=["t2", "t1"],  # batch_members.ordinal order (Task 3.1 derives)
            batch_id="batch-9",
            accepted_count_total=2,
            completed_flush_count=0,
            scalars=AggregationNodeScalars(None, None),
            attempt_offsets={"t1": 1, "t2": 1},  # max_attempt+1 discipline
            resume_checkpoint_id="cp-0",
            now=t0 + timedelta(seconds=60),
        )

        tokens = executor.get_buffered_tokens(nid)
        rows = executor.get_buffered_rows(nid)
        # Buffer order comes from member_order (authoritative accept order), not item order
        assert [t.token_id for t in tokens] == ["t2", "t1"]
        assert rows[0]["at"] == datetime(2026, 6, 2, tzinfo=UTC)
        assert isinstance(rows[0]["at"], datetime)
        assert rows[0]["amount"] == Decimal("2.25")
        assert isinstance(rows[0]["amount"], Decimal)
        assert rows[1]["at"] == datetime(2026, 6, 1, tzinfo=UTC)
        assert all(isinstance(t.row_data, PipelineRow) for t in tokens)
        assert executor.get_batch_id(nid) == "batch-9"
        node = executor._nodes[nid]
        assert node.accepted_count_total == 2
        assert node.completed_flush_count == 0
        assert node.member_count == 2
        assert all(t.resume_attempt_offset == 1 and t.resume_checkpoint_id == "cp-0" for t in tokens)
        # Trigger age restored from absolute blocked_at (oldest item), not an offset blob
        assert node.trigger.get_age_seconds() == pytest.approx(60.0)

    def test_restore_from_journal_counter_only_node(self) -> None:
        """Empty journal restores only the audit-derived counters (post-flush node)."""
        executor, _, nid = self._make_agg_executor(node_id="agg-1", clock=MockClock(start=10.0))

        executor.restore_from_journal(
            node_id=nid,
            items=[],
            member_order=[],
            batch_id=None,
            accepted_count_total=5,
            completed_flush_count=2,
            scalars=AggregationNodeScalars(None, None),
            attempt_offsets={},
            resume_checkpoint_id="cp-0",
            now=_JOURNAL_T0,
        )

        node = executor._nodes[nid]
        assert node.completed_flush_count == 2
        assert node.accepted_count_total == 5
        assert node.batch_id is None
        assert node.tokens == []
        assert executor.get_buffered_rows(nid) == []
        assert node.member_count == 0
        assert node.trigger.get_age_seconds() == 0.0

    def test_restore_from_journal_empty_items_drops_stale_scalars(self) -> None:
        """Latched scalars with an empty buffer are STALE — dropped, never restored.

        Trigger latches are batch-scoped; zero buffered items means no current
        batch. The window is real (crash after a flush terminalized the BLOCKED
        rows but before the next checkpoint), so the stale latches must be
        ignored rather than rejected. Restoring them at batch_count=0 would
        plant a phantom first-accept anchor at restore time (record_accept only
        sets first_accept_time when None) → wrong timeout age + pre-fired
        count latch in the NEXT genuine batch.
        """
        clock = MockClock(start=0.0)
        executor, _, nid = self._make_agg_executor(node_id="agg-1", count=2, clock=clock)

        executor.restore_from_journal(
            node_id=nid,
            items=[],
            member_order=[],
            batch_id=None,
            accepted_count_total=3,
            completed_flush_count=1,
            scalars=AggregationNodeScalars(count_fire_offset=1.5, condition_fire_offset=2.5),
            attempt_offsets={},
            resume_checkpoint_id="cp-0",
            now=_JOURNAL_T0,
        )

        node = executor._nodes[nid]
        # Trigger is fully unlatched — no phantom anchor, no pre-fired latches
        assert node.trigger.get_count_fire_offset() is None
        assert node.trigger.get_condition_fire_offset() is None
        assert node.trigger.get_age_seconds() == 0.0
        assert executor.get_barrier_scalars() == {}
        # Counters still restored
        assert node.accepted_count_total == 3
        assert node.completed_flush_count == 1

        # Time passes before the next genuine batch starts — age must anchor
        # to the first accept, NOT the restore instant.
        clock.advance(30.0)
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t9"))
        assert node.trigger.get_age_seconds() == 0.0
        assert executor.should_flush(nid) is False  # count=2, one row, no stale latch

    def test_restore_from_journal_null_blocked_at_is_corruption(self) -> None:
        """Post-epoch-20 every BLOCKED row was stamped; NULL barrier_blocked_at = corruption."""
        executor, _, nid = self._make_agg_executor(node_id="agg-1")

        with pytest.raises(AuditIntegrityError, match="barrier_blocked_at"):
            executor.restore_from_journal(
                node_id=nid,
                items=[
                    _blocked_item(
                        token_id="t1",
                        row_id="r1",
                        node_id="agg-1",
                        payload=_journal_payload({"v": 1}),
                        blocked_at=None,
                    )
                ],
                member_order=["t1"],
                batch_id="b",
                accepted_count_total=1,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={"t1": 1},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    def test_restore_from_journal_membership_mismatch_is_corruption(self) -> None:
        """Journal items and batch_members (member_order) must agree as SETS.

        Order is sourced FROM batch_members, so ordered comparison would be
        circular — but a token present in one and missing from the other means
        journal and audit disagree about batch membership: corruption.
        """
        executor, _, nid = self._make_agg_executor(node_id="agg-1")
        items = [
            _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
        ]

        # member_order references a token with no BLOCKED journal row
        with pytest.raises(AuditIntegrityError, match="batch membership"):
            executor.restore_from_journal(
                node_id=nid,
                items=items,
                member_order=["t1", "t-ghost"],
                batch_id="b",
                accepted_count_total=2,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={"t1": 1, "t-ghost": 1},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

        # Journal row not present in batch_members
        with pytest.raises(AuditIntegrityError, match="batch membership"):
            executor.restore_from_journal(
                node_id=nid,
                items=items,
                member_order=[],
                batch_id="b",
                accepted_count_total=1,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={"t1": 1},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    def test_restore_from_journal_duplicate_journal_rows_are_corruption(self) -> None:
        """Two BLOCKED journal rows for the same token at one barrier = corruption."""
        executor, _, nid = self._make_agg_executor(node_id="agg-1")
        items = [
            _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
            _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
        ]

        with pytest.raises(AuditIntegrityError, match="Duplicate BLOCKED journal rows"):
            executor.restore_from_journal(
                node_id=nid,
                items=items,
                member_order=["t1"],
                batch_id="b",
                accepted_count_total=2,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={"t1": 1},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    def test_restore_from_journal_duplicate_member_order_is_corruption(self) -> None:
        """A duplicated token id in batch_members order = audit trail corruption."""
        executor, _, nid = self._make_agg_executor(node_id="agg-1")
        items = [
            _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
        ]

        with pytest.raises(AuditIntegrityError, match=r"Duplicate token ids in batch_members.*\['t1'\]"):
            executor.restore_from_journal(
                node_id=nid,
                items=items,
                member_order=["t1", "t1"],
                batch_id="b",
                accepted_count_total=2,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={"t1": 1},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    def test_restore_from_journal_missing_attempt_offset_is_corruption(self) -> None:
        """Every journal item must have an audit-derived attempt offset."""
        executor, _, nid = self._make_agg_executor(node_id="agg-1")

        with pytest.raises(AuditIntegrityError, match="attempt_offsets"):
            executor.restore_from_journal(
                node_id=nid,
                items=[
                    _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
                ],
                member_order=["t1"],
                batch_id="b",
                accepted_count_total=1,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    def test_restore_from_journal_batch_id_items_consistency(self) -> None:
        """Items without batch_id (or batch_id without items) is corruption."""
        executor, _, nid = self._make_agg_executor(node_id="agg-1")
        items = [
            _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
        ]

        with pytest.raises(AuditIntegrityError, match="batch_id"):
            executor.restore_from_journal(
                node_id=nid,
                items=items,
                member_order=["t1"],
                batch_id=None,
                accepted_count_total=1,
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={"t1": 1},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

        with pytest.raises(AuditIntegrityError, match="batch_id"):
            executor.restore_from_journal(
                node_id=nid,
                items=[],
                member_order=[],
                batch_id="batch-orphan",
                accepted_count_total=1,
                completed_flush_count=1,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    def test_restore_from_journal_impossible_counters_are_corruption(self) -> None:
        """accepted_count_total must cover every buffered row; counters non-negative."""
        executor, _, nid = self._make_agg_executor(node_id="agg-1")
        items = [
            _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
            _blocked_item(token_id="t2", row_id="r2", node_id="agg-1", payload=_journal_payload({"v": 2}), blocked_at=_JOURNAL_T0),
        ]

        with pytest.raises(AuditIntegrityError, match="impossible"):
            executor.restore_from_journal(
                node_id=nid,
                items=items,
                member_order=["t1", "t2"],
                batch_id="b",
                accepted_count_total=1,  # < 2 buffered rows — impossible
                completed_flush_count=0,
                scalars=AggregationNodeScalars(None, None),
                attempt_offsets={"t1": 1, "t2": 1},
                resume_checkpoint_id="cp-0",
                now=_JOURNAL_T0,
            )

    def test_restore_from_journal_restores_trigger_fire_offsets(self) -> None:
        """Scalar trigger latches pass through to the TriggerEvaluator and read back."""
        clock = MockClock(start=50.0)
        executor, _, nid = self._make_agg_executor(node_id="agg-1", count=1, clock=clock)

        executor.restore_from_journal(
            node_id=nid,
            items=[
                _blocked_item(token_id="t1", row_id="r1", node_id="agg-1", payload=_journal_payload({"v": 1}), blocked_at=_JOURNAL_T0),
            ],
            member_order=["t1"],
            batch_id="b",
            accepted_count_total=1,
            completed_flush_count=0,
            scalars=AggregationNodeScalars(count_fire_offset=1.5, condition_fire_offset=None),
            attempt_offsets={"t1": 1},
            resume_checkpoint_id="cp-0",
            now=_JOURNAL_T0 + timedelta(seconds=60),
        )

        node = executor._nodes[nid]
        assert node.trigger.get_count_fire_offset() == pytest.approx(1.5)
        assert node.trigger.get_condition_fire_offset() is None

    # --- get_barrier_scalars (F1: checkpoint row carries only trigger latches) ---

    def test_get_barrier_scalars_skips_unlatched_nodes(self) -> None:
        """Nodes whose triggers have not fired contribute no scalars.

        Emission choice (Task 2.1): only nodes with at least one non-None fire
        offset are emitted — the checkpoint writer serializes None when nothing
        latched, and restore treats a missing entry as (None, None). This keeps
        checkpoints minimal and matches the Task 1.2 transitional projection.
        """
        executor, _, nid = self._make_agg_executor(count=10)
        assert executor.get_barrier_scalars() == {}
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        assert executor.get_barrier_scalars() == {}

    def test_get_barrier_scalars_reads_live_trigger_latches(self) -> None:
        """A latched count trigger surfaces as AggregationNodeScalars for its node."""
        clock = MockClock(start=0.0)
        executor, _, nid = self._make_agg_executor(count=2, clock=clock)
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t1"))
        clock.advance(1.5)
        _accept_adopted_aggregation_row(executor, nid, _make_token(token_id="t2"))
        # should_flush() evaluates triggers, latching the count fire time
        assert executor.should_flush(nid) is True

        scalars = executor.get_barrier_scalars()
        assert set(scalars) == {nid}
        assert scalars[nid].count_fire_offset == pytest.approx(1.5)
        assert scalars[nid].condition_fire_offset is None


# =============================================================================
# Regression: B2 — AggregationExecutor post-processing terminality
# =============================================================================


class TestAggregationExecutorTerminality:
    """Regression tests for B2: AggregationExecutor post-processing failures.

    Before the NodeStateGuard fix, output hashing failure in execute_flush()
    left the node_state OPEN and the batch in EXECUTING state permanently.

    These tests verify that post-processing failures are caught by the guard,
    the batch is marked FAILED, and buffers are cleared for recovery.
    """

    @staticmethod
    def _make_agg_executor(
        count: int = 2,
    ) -> tuple[AggregationExecutor, MagicMock, NodeID]:
        """Create executor + mock factory + node_id."""
        nid = NodeID("agg_1")
        factory = _make_factory()
        settings = AggregationSettings(
            name="test_agg",
            plugin="batch_stats",
            input="default",
            on_error="discard",
            trigger=TriggerConfig(count=count),
        )
        executor = AggregationExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            run_id="test-run",
            aggregation_settings={nid: settings},
        )
        return executor, factory, nid

    def test_output_hash_failure_marks_state_and_batch_failed(self) -> None:
        """Output hash failure → state FAILED AND batch FAILED, buffers cleared.

        Regression: B2 — stable_hash raises for non-canonical data after
        transform.process() succeeds, but this was outside the old try/except.
        The non-canonical-output violation is Tier 2, so since operator ruling
        2026-09-23 (elspeth-5887fb7928) it fails the batch as a routed error
        rather than aborting the run.
        """
        executor, factory, nid = self._make_agg_executor(count=2)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))
        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "b"}, token_id="t2", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.success(
            make_row({"value": "aggregated"}, contract=contract),
            success_reason={"action": "aggregated"},
        )
        ctx = make_context()

        # Make output hash fail (simulating NaN in transform output)
        import elspeth.engine.executors.aggregation as agg_mod

        original_hash = agg_mod.stable_hash  # type: ignore[attr-defined]
        call_count = 0

        def failing_hash(data: Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return original_hash(data)  # input hash succeeds
            raise ValueError("NaN detected in output")

        agg_mod.stable_hash = failing_hash  # type: ignore[attr-defined, assignment]
        try:
            result, _tokens, _batch_id = executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)
        finally:
            agg_mod.stable_hash = original_hash  # type: ignore[attr-defined]

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "contract_violation"
        assert "non-canonical data" in result.reason["error"]

        # State and batch FAILED together: the one verdict write carries both.
        factory.execution.complete_aggregation_failure.assert_called_once()
        verdict = factory.execution.complete_aggregation_failure.call_args.kwargs
        assert verdict["state_id"] == "state_001"
        assert verdict["reason"] == result.reason
        assert verdict["batch_id"] == "batch_001"
        assert verdict["trigger_type"] == TriggerType.COUNT
        factory.execution.complete_batch.assert_not_called()

        # Buffers cleared for recovery
        assert executor.get_buffer_count(nid) == 0

    def test_batch_complete_failure_raises_audit_integrity_error(self) -> None:
        """If complete_batch itself fails, AuditIntegrityError crashes the run.

        Edge case: guard completes the node state, but complete_batch
        raises (e.g., DB write failure). Leaving the batch in non-terminal
        state would corrupt the audit trail, so we crash immediately.
        """
        executor, factory, nid = self._make_agg_executor(count=1)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        # Transform crashes — inner except completes guard, then outer except tries cleanup
        transform.process.side_effect = RuntimeError("plugin crash")
        ctx = make_context()

        # Make complete_batch fail (DB is down during cleanup)
        factory.execution.complete_batch.side_effect = RuntimeError("DB down")

        with pytest.raises(AuditIntegrityError, match="non-terminal state"):
            executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

    def test_successful_flush_still_completes_normally(self) -> None:
        """Sanity: guard does not interfere with normal flush success path."""
        executor, factory, nid = self._make_agg_executor(count=1)
        contract = _make_contract()

        _accept_adopted_aggregation_row(executor, nid, _make_token(data={"value": "a"}, token_id="t1", contract=contract))

        transform = _make_aggregation_transform("agg_transform")
        transform.process.return_value = TransformResult.success(
            make_row({"value": "aggregated"}, contract=contract),
            success_reason={"action": "aggregated"},
        )
        ctx = make_context()

        result, tokens, _batch_id = executor.execute_flush(nid, transform, ctx, TriggerType.COUNT)

        assert result.status == "success"
        assert len(tokens) == 1

        # Guard stands down after the atomic result receipt: exactly one
        # completion write, and no legacy batch/node-state writes (a guard
        # auto-FAIL after the receipt would show up here as complete_node_state).
        factory.execution.complete_aggregation_result.assert_called_once()
        receipt_kwargs = factory.execution.complete_aggregation_result.call_args.kwargs
        assert receipt_kwargs["batch_id"] == "batch_001"
        assert receipt_kwargs["state_id"] == "state_001"
        factory.execution.complete_batch.assert_not_called()
        factory.execution.complete_node_state.assert_not_called()
