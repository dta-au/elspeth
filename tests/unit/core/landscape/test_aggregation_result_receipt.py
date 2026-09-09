"""Atomic persistence tests for durable aggregation result receipts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from elspeth.contracts import AggregationResultMember, BatchStatus, NodeStateStatus, NodeType, OutputMode, TriggerType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import AggregationMemberAction, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.node_state_context import AggregationFlushContext
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.data_flow.outcomes import record_buffered_outcome_guarded
from elspeth.core.landscape.schema import (
    aggregation_result_members_table,
    aggregation_result_outputs_table,
    aggregation_results_table,
)
from tests.fixtures.landscape import make_recorder_with_run, register_test_node


def test_completion_failure_rolls_back_node_batch_and_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The completion boundary cannot expose a terminal state without its receipt."""
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    register_test_node(
        setup.data_flow,
        setup.run_id,
        "agg-1",
        node_type=NodeType.AGGREGATION,
        plugin_name="aggregator",
    )
    for ordinal in range(2):
        setup.data_flow.create_row(
            setup.run_id,
            setup.source_node_id,
            ordinal,
            {"value": ordinal + 1},
            row_id=f"row-{ordinal}",
            source_row_index=ordinal,
            ingest_sequence=ordinal,
        )
        setup.data_flow.create_token(f"row-{ordinal}", token_id=f"tok-{ordinal}")

    execution = setup.execution
    state = execution.begin_node_state("tok-0", "agg-1", setup.run_id, 0, {"value": 1}, state_id="state-1")
    execution.create_batch(setup.run_id, "agg-1", batch_id="batch-1")
    execution.add_batch_member("batch-1", "tok-0", ordinal=0)
    execution.add_batch_member("batch-1", "tok-1", ordinal=1)
    with setup.db.write_connection() as conn:
        for ordinal in range(2):
            record_buffered_outcome_guarded(
                conn,
                run_id=setup.run_id,
                token_id=f"tok-{ordinal}",
                batch_id="batch-1",
                recorded_at=datetime.now(UTC),
            )
    execution.update_batch_status("batch-1", BatchStatus.EXECUTING, state_id=state.state_id)

    real_complete_batch = execution.batches.complete_batch

    def fail_after_batch_completion(*args: object, **kwargs: object) -> object:
        real_complete_batch(*args, **kwargs)
        raise RuntimeError("injected failure after batch completion")

    monkeypatch.setattr(execution.batches, "complete_batch", fail_after_batch_completion)
    output = PipelineRow({"total": 3}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    members = tuple(
        AggregationResultMember(
            member_ref=TokenRef(token_id=f"tok-{ordinal}", run_id=setup.run_id),
            action=AggregationMemberAction.CONSUME_BATCH,
            error_hash=None,
        )
        for ordinal in range(2)
    )

    def complete(row: PipelineRow = output, *, duration_ms: float = 1.0) -> object:
        return execution.complete_aggregation_result(
            batch_id="batch-1",
            run_id=setup.run_id,
            aggregation_node_id="agg-1",
            state_id=state.state_id,
            trigger_type=TriggerType.END_OF_SOURCE,
            output_mode=OutputMode.TRANSFORM,
            output_rows=(row,),
            output_shape="single",
            output_hash=stable_hash(row.to_dict()),
            members=members,
            expansion_parent_token_id="tok-0",
            duration_ms=duration_ms,
            success_reason=None,
            context_after=AggregationFlushContext(
                trigger_type=TriggerType.END_OF_SOURCE.value,
                buffer_size=2,
                batch_id="batch-1",
                flush_index=1,
                rows_seen_total=2,
                row_start=1,
                row_end=2,
                is_end_of_source=True,
            ),
        )

    with pytest.raises(RuntimeError, match="injected failure after batch completion"):
        complete()

    assert execution.get_node_state(state.state_id).status is NodeStateStatus.OPEN
    assert execution.get_batch("batch-1").status is BatchStatus.EXECUTING
    with setup.db.read_only_connection() as conn:
        for table in (aggregation_results_table, aggregation_result_outputs_table, aggregation_result_members_table):
            assert conn.execute(select(func.count()).select_from(table)).scalar_one() == 0

    monkeypatch.setattr(execution.batches, "complete_batch", real_complete_batch)
    first = complete()
    second = complete()
    assert first == second
    with pytest.raises(AuditIntegrityError, match="retry diverges"):
        complete(PipelineRow({"total": 4}, output.contract))
    with pytest.raises(AuditIntegrityError, match="retry diverges"):
        complete(duration_ms=2.0)
    assert execution.get_node_state(state.state_id).status is NodeStateStatus.COMPLETED
    assert execution.get_batch("batch-1").status is BatchStatus.COMPLETED
    with setup.db.read_only_connection() as conn:
        assert conn.execute(select(func.count()).select_from(aggregation_results_table)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(aggregation_result_outputs_table)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(aggregation_result_members_table)).scalar_one() == 2


def test_completion_accepts_original_batch_acceptances_on_a_retry_batch() -> None:
    """A resumed flush completes a retry batch against ORIGINAL-batch BUFFERED acceptances.

    Crash-retry (handle_incomplete_batches -> retry_batch) copies members to a
    new batch linked via retry_of_batch_id, but the acceptance-time BUFFERED
    token_outcomes are immutable history and still name the original batch.
    The receipt writer must bind liveness against the durable retry lineage.
    """
    from elspeth.contracts.errors import ExecutionError

    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    register_test_node(
        setup.data_flow,
        setup.run_id,
        "agg-1",
        node_type=NodeType.AGGREGATION,
        plugin_name="aggregator",
    )
    for ordinal in range(2):
        setup.data_flow.create_row(
            setup.run_id,
            setup.source_node_id,
            ordinal,
            {"value": ordinal + 1},
            row_id=f"row-{ordinal}",
            source_row_index=ordinal,
            ingest_sequence=ordinal,
        )
        setup.data_flow.create_token(f"row-{ordinal}", token_id=f"tok-{ordinal}")

    execution = setup.execution
    first_state = execution.begin_node_state("tok-0", "agg-1", setup.run_id, 0, {"value": 1}, state_id="state-1")
    execution.create_batch(setup.run_id, "agg-1", batch_id="batch-1")
    execution.add_batch_member("batch-1", "tok-0", ordinal=0)
    execution.add_batch_member("batch-1", "tok-1", ordinal=1)
    with setup.db.write_connection() as conn:
        for ordinal in range(2):
            record_buffered_outcome_guarded(
                conn,
                run_id=setup.run_id,
                token_id=f"tok-{ordinal}",
                batch_id="batch-1",
                recorded_at=datetime.now(UTC),
            )
    execution.update_batch_status("batch-1", BatchStatus.EXECUTING, state_id=first_state.state_id)

    # First flush attempt dies: FAILED node state + FAILED batch, then the
    # resume-side repair mints the retry batch with copied members.
    execution.complete_node_state(
        state_id=first_state.state_id,
        status=NodeStateStatus.FAILED,
        duration_ms=1.0,
        error=ExecutionError(exception="injected flush crash", exception_type="RuntimeError"),
    )
    execution.complete_batch(
        "batch-1",
        BatchStatus.FAILED,
        trigger_type=TriggerType.END_OF_SOURCE,
        state_id=first_state.state_id,
    )
    retry = execution.retry_batch("batch-1")
    assert retry.retry_of_batch_id == "batch-1"

    resume_state = execution.begin_node_state("tok-0", "agg-1", setup.run_id, 0, {"value": 1}, state_id="state-2", attempt=1)
    execution.update_batch_status(retry.batch_id, BatchStatus.EXECUTING, state_id=resume_state.state_id)

    output = PipelineRow({"total": 3}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    receipt = execution.complete_aggregation_result(
        batch_id=retry.batch_id,
        run_id=setup.run_id,
        aggregation_node_id="agg-1",
        state_id=resume_state.state_id,
        trigger_type=TriggerType.END_OF_SOURCE,
        output_mode=OutputMode.TRANSFORM,
        output_rows=(output,),
        output_shape="single",
        output_hash=stable_hash(output.to_dict()),
        members=tuple(
            AggregationResultMember(
                member_ref=TokenRef(token_id=f"tok-{ordinal}", run_id=setup.run_id),
                action=AggregationMemberAction.CONSUME_BATCH,
                error_hash=None,
            )
            for ordinal in range(2)
        ),
        expansion_parent_token_id="tok-0",
        duration_ms=1.0,
        success_reason=None,
        context_after=AggregationFlushContext(
            trigger_type=TriggerType.END_OF_SOURCE.value,
            buffer_size=2,
            batch_id=retry.batch_id,
            flush_index=1,
            rows_seen_total=2,
            row_start=1,
            row_end=2,
            is_end_of_source=True,
        ),
    )

    assert receipt.batch_id == retry.batch_id
    assert execution.get_node_state(resume_state.state_id).status is NodeStateStatus.COMPLETED
    assert execution.get_batch(retry.batch_id).status is BatchStatus.COMPLETED
    # The failed original keeps its immutable identity.
    assert execution.get_batch("batch-1").status is BatchStatus.FAILED
    with setup.db.read_only_connection() as conn:
        assert conn.execute(select(func.count()).select_from(aggregation_results_table)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(aggregation_result_members_table)).scalar_one() == 2


def test_completion_refuses_buffered_acceptance_outside_the_retry_lineage() -> None:
    """A member's live BUFFERED acceptance in an unrelated batch is not liveness."""
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    register_test_node(
        setup.data_flow,
        setup.run_id,
        "agg-1",
        node_type=NodeType.AGGREGATION,
        plugin_name="aggregator",
    )
    setup.data_flow.create_row(
        setup.run_id,
        setup.source_node_id,
        0,
        {"value": 1},
        row_id="row-0",
        source_row_index=0,
        ingest_sequence=0,
    )
    setup.data_flow.create_token("row-0", token_id="tok-0")

    execution = setup.execution
    state = execution.begin_node_state("tok-0", "agg-1", setup.run_id, 0, {"value": 1}, state_id="state-1")
    execution.create_batch(setup.run_id, "agg-1", batch_id="batch-a")
    execution.create_batch(setup.run_id, "agg-1", batch_id="batch-b")
    execution.add_batch_member("batch-a", "tok-0", ordinal=0)
    with setup.db.write_connection() as conn:
        record_buffered_outcome_guarded(
            conn,
            run_id=setup.run_id,
            token_id="tok-0",
            batch_id="batch-b",
            recorded_at=datetime.now(UTC),
        )
    execution.update_batch_status("batch-a", BatchStatus.EXECUTING, state_id=state.state_id)

    output = PipelineRow({"total": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    with pytest.raises(AuditIntegrityError, match="within the batch retry lineage"):
        execution.complete_aggregation_result(
            batch_id="batch-a",
            run_id=setup.run_id,
            aggregation_node_id="agg-1",
            state_id=state.state_id,
            trigger_type=TriggerType.END_OF_SOURCE,
            output_mode=OutputMode.TRANSFORM,
            output_rows=(output,),
            output_shape="single",
            output_hash=stable_hash(output.to_dict()),
            members=(
                AggregationResultMember(
                    member_ref=TokenRef(token_id="tok-0", run_id=setup.run_id),
                    action=AggregationMemberAction.CONSUME_BATCH,
                    error_hash=None,
                ),
            ),
            expansion_parent_token_id="tok-0",
            duration_ms=1.0,
            success_reason=None,
            context_after=AggregationFlushContext(
                trigger_type=TriggerType.END_OF_SOURCE.value,
                buffer_size=1,
                batch_id="batch-a",
                flush_index=1,
                rows_seen_total=1,
                row_start=1,
                row_end=1,
                is_end_of_source=True,
            ),
        )

    assert execution.get_node_state(state.state_id).status is NodeStateStatus.OPEN
    assert execution.get_batch("batch-a").status is BatchStatus.EXECUTING
    with setup.db.read_only_connection() as conn:
        assert conn.execute(select(func.count()).select_from(aggregation_results_table)).scalar_one() == 0


def test_completion_rejects_member_with_buffered_history_and_terminal_outcome() -> None:
    """A retained BUFFERED row cannot hide an already-terminal batch member."""
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    register_test_node(
        setup.data_flow,
        setup.run_id,
        "agg-1",
        node_type=NodeType.AGGREGATION,
        plugin_name="aggregator",
    )
    setup.data_flow.create_row(
        setup.run_id,
        setup.source_node_id,
        0,
        {"value": 1},
        row_id="row-0",
        source_row_index=0,
        ingest_sequence=0,
    )
    setup.data_flow.create_token("row-0", token_id="tok-0")

    execution = setup.execution
    state = execution.begin_node_state("tok-0", "agg-1", setup.run_id, 0, {"value": 1}, state_id="state-1")
    execution.create_batch(setup.run_id, "agg-1", batch_id="batch-1")
    execution.add_batch_member("batch-1", "tok-0", ordinal=0)
    ref = TokenRef(token_id="tok-0", run_id=setup.run_id)
    setup.data_flow.record_token_outcome(ref, None, TerminalPath.BUFFERED, batch_id="batch-1")
    setup.data_flow.record_token_outcome(
        ref,
        TerminalOutcome.TRANSIENT,
        TerminalPath.BATCH_CONSUMED,
        batch_id="batch-1",
    )
    execution.update_batch_status("batch-1", BatchStatus.EXECUTING, state_id=state.state_id)

    output = PipelineRow({"total": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    with pytest.raises(AuditIntegrityError, match="already have terminal outcomes"):
        execution.complete_aggregation_result(
            batch_id="batch-1",
            run_id=setup.run_id,
            aggregation_node_id="agg-1",
            state_id=state.state_id,
            trigger_type=TriggerType.END_OF_SOURCE,
            output_mode=OutputMode.TRANSFORM,
            output_rows=(output,),
            output_shape="single",
            output_hash=stable_hash(output.to_dict()),
            members=(
                AggregationResultMember(
                    member_ref=ref,
                    action=AggregationMemberAction.CONSUME_BATCH,
                    error_hash=None,
                ),
            ),
            expansion_parent_token_id="tok-0",
            duration_ms=1.0,
            success_reason=None,
            context_after=AggregationFlushContext(
                trigger_type=TriggerType.END_OF_SOURCE.value,
                buffer_size=1,
                batch_id="batch-1",
                flush_index=1,
                rows_seen_total=1,
                row_start=1,
                row_end=1,
                is_end_of_source=True,
            ),
        )

    assert execution.get_node_state(state.state_id).status is NodeStateStatus.OPEN
    assert execution.get_batch("batch-1").status is BatchStatus.EXECUTING
    with setup.db.read_only_connection() as conn:
        assert conn.execute(select(func.count()).select_from(aggregation_results_table)).scalar_one() == 0
