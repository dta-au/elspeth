"""Production-path sink boundary enforcement integration coverage.

Sibling of ``test_source_boundary_orchestrator``. A source that fails its
boundary check fails the ``source_load`` operation that wraps source
consumption, so the run keeps an operation-level pointer at the node that
raised. A sink boundary failure happens *before* any sink effect is reserved,
so the reservation-owned ``sink_write`` operation
(``SinkEffectReservation._insert_or_compare_operation``) never exists — and
without the record added here the run would carry no failed operation at all
(elspeth-207c9fbb0b).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.contracts import NodeType, PendingOutcome, PluginSchema, RoutingMode, RunStatus, TerminalOutcome, TerminalPath, TokenInfo
from elspeth.contracts.enums import NodeStateStatus
from elspeth.contracts.errors import PluginContractViolation, SinkTransactionalInvariantError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, operations_table, runs_table
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.spans import SpanFactory
from elspeth.web.execution.diagnostics import load_run_diagnostics_from_db
from tests.fixtures.base_classes import as_sink, as_source, create_observed_contract
from tests.fixtures.landscape import make_factory, register_test_node
from tests.fixtures.pipeline import build_linear_pipeline
from tests.fixtures.sink_effects import DuplicateObservableTarget, PartitioningObservableSink
from tests.fixtures.stores import MockPayloadStore


class _RequiresMissingField(PluginSchema):
    """Sink schema demanding a field the pipeline rows never carry."""

    field_the_row_never_has: str


def _run_failing_sink_pipeline(db: LandscapeDB) -> str:
    """Run a pipeline whose sink rejects every row at its input boundary."""
    source, _transforms, sinks, graph = build_linear_pipeline([{"value": 1}, {"value": 2}], transforms=[])
    sinks["default"].input_schema = _RequiresMissingField

    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"default": as_sink(sinks["default"])},
    )

    with pytest.raises(PluginContractViolation, match="input validation failed"):
        Orchestrator(db).run(config, graph=graph, payload_store=MockPayloadStore())

    with db.connection() as conn:
        run_rows = conn.execute(select(runs_table)).fetchall()
    assert len(run_rows) == 1
    assert run_rows[0].status == RunStatus.FAILED
    return str(run_rows[0].run_id)


def test_sink_boundary_failure_records_a_failed_sink_write_operation() -> None:
    """A sink boundary failure must leave a FAILED sink_write operation row.

    Without it a run whose only failures are sink node_states has no failed
    operation, and every operation-level consumer of the audit trail loses the
    per-node pointer to the sink that actually raised.
    """
    db = LandscapeDB.in_memory()
    run_id = _run_failing_sink_pipeline(db)

    with db.connection() as conn:
        operations = conn.execute(select(operations_table).where(operations_table.c.run_id == run_id)).fetchall()
        states = conn.execute(select(node_states_table).where(node_states_table.c.run_id == run_id)).fetchall()

    sink_node_ids = {row.node_id for row in states if row.status == NodeStateStatus.FAILED}
    assert len(sink_node_ids) == 1, "expected exactly one failing sink node"
    sink_node_id = sink_node_ids.pop()

    failed_writes = [row for row in operations if row.operation_type == "sink_write" and row.status == "failed"]
    assert len(failed_writes) == 1, (
        f"expected exactly one failed sink_write operation, got {[(r.operation_type, r.status) for r in operations]}"
    )

    failed_write = failed_writes[0]
    assert failed_write.node_id == sink_node_id
    assert "input validation failed" in (failed_write.error_message or "")
    # The effect was never reserved, so this operation owns no effect identity.
    # The schema's ck_operations_sink_effect_type check permits exactly this.
    assert failed_write.sink_effect_id is None


def test_sink_boundary_failure_does_not_double_record_the_write_operation() -> None:
    """The boundary record must not duplicate the reservation-owned operation.

    ``SinkEffectReservation`` already inserts one ``sink_write`` operation per
    reserved effect. The boundary record is reached only when validation raises
    *before* reservation, so the two paths are mutually exclusive and a failing
    sink must still produce exactly one sink_write row.
    """
    db = LandscapeDB.in_memory()
    run_id = _run_failing_sink_pipeline(db)

    with db.connection() as conn:
        operations = conn.execute(select(operations_table).where(operations_table.c.run_id == run_id)).fetchall()

    sink_writes = [row for row in operations if row.operation_type == "sink_write"]
    assert len(sink_writes) == 1, f"expected exactly one sink_write operation, got {len(sink_writes)}"
    assert sink_writes[0].status == "failed"


class _RejectingFailsink(PartitioningObservableSink):
    """Failsink whose transactional backstop rejects every enriched row."""

    declared_required_fields = frozenset({"must_exist"})


def _effect_tokens(factory, *, run_id: str, source_id: str, rows: list[dict[str, object]]) -> list[TokenInfo]:  # type: ignore[no-untyped-def]
    tokens: list[TokenInfo] = []
    for index, row_data in enumerate(rows):
        row = factory.data_flow.create_row(
            run_id=run_id,
            source_node_id=source_id,
            row_index=index,
            data=row_data,
            source_row_index=index,
            ingest_sequence=index,
        )
        durable = factory.data_flow.create_token(row.row_id)
        tokens.append(
            TokenInfo(
                row_id=row.row_id,
                token_id=durable.token_id,
                row_data=PipelineRow(row_data, create_observed_contract(row_data)),
            )
        )
    return tokens


def _run_failing_failsink(db: LandscapeDB) -> tuple[str, str]:
    """Divert a token to a failsink that rejects it at its input boundary.

    Returns ``(run_id, failsink_node_id)``. The primary effect finalizes
    normally, so its own sink_write operation is 'completed' — only the
    failsink's write fails.
    """
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
    primary_id = register_test_node(factory.data_flow, run.run_id, "primary", node_type=NodeType.SINK, plugin_name="partitioning")
    failsink_id = register_test_node(factory.data_flow, run.run_id, "failsink", node_type=NodeType.SINK, plugin_name="rejecting")
    edge = factory.data_flow.register_edge(run.run_id, primary_id, failsink_id, "__failsink__", RoutingMode.DIVERT)
    accepted, diverted = _effect_tokens(
        factory,
        run_id=run.run_id,
        source_id=source_id,
        rows=[{"value": 1}, {"value": 2, "divert": True}],
    )
    primary = PartitioningObservableSink(DuplicateObservableTarget(), name="primary")
    primary.node_id = primary_id
    failsink = _RejectingFailsink(DuplicateObservableTarget(), name="failsink", divert_rows=False)
    failsink.node_id = failsink_id
    ctx = PluginContext(run_id=run.run_id, config={}, landscape=factory.plugin_audit_writer(), node_id=primary_id)

    with pytest.raises(SinkTransactionalInvariantError, match="must_exist"):
        SinkExecutor(
            factory.execution,
            factory.data_flow,
            SpanFactory(),
            run.run_id,
            factory=factory,
            worker_id="worker-a",
        ).write(
            primary,  # type: ignore[arg-type]
            [accepted, diverted],
            ctx,
            1,
            sink_name="output",
            pending_outcome=PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW),
            effect_mode="write",
            failsink=failsink,  # type: ignore[arg-type]
            failsink_name="failsink",
            failsink_effect_mode="write",
            failsink_edge_id=edge.edge_id,
            join_group_id_by_token={t.token_id: None for t in [accepted, diverted]},
        )
    return str(run.run_id), str(failsink_id)


def test_failsink_boundary_failure_records_a_failed_sink_write_operation(tmp_path: Path) -> None:
    """The failsink boundary must mirror the primary path's operation record.

    When the linked failsink rejects the enriched rows, the primary effect has
    already finalized — so its sink_write operation reads 'completed' and the
    run would otherwise carry no failed operation at all, exactly the
    elspeth-207c9fbb0b symptom reached by a different trigger.
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'failsink_boundary.db'}")
    try:
        run_id, failsink_id = _run_failing_failsink(db)

        with db.connection() as conn:
            operations = conn.execute(select(operations_table).where(operations_table.c.run_id == run_id)).fetchall()

        failed_writes = [row for row in operations if row.operation_type == "sink_write" and row.status == "failed"]
        assert len(failed_writes) == 1, f"expected one failed sink_write, got {[(r.node_id, r.status) for r in operations]}"
        assert failed_writes[0].node_id == failsink_id
        assert failed_writes[0].sink_effect_id is None
        assert "must_exist" in (failed_writes[0].error_message or "")
    finally:
        db.close()


# ── UI-level pins ────────────────────────────────────────────────────────────
# The two tests above stop at the operations table. The ticket's actual symptom
# is what the run drawer renders, so the projection is pinned here as well: an
# operations row that exists but projects to the wrong node is worse than the
# original failure_detail=None, because it names a bystander confidently.


def test_primary_boundary_failure_projects_failure_detail_at_the_sink() -> None:
    """The failing sink must be what the run diagnostics actually surface."""
    db = LandscapeDB.in_memory()
    run_id = _run_failing_sink_pipeline(db)

    with db.connection() as conn:
        states = conn.execute(select(node_states_table).where(node_states_table.c.run_id == run_id)).fetchall()
    sink_node_id = {row.node_id for row in states if row.status == NodeStateStatus.FAILED}.pop()

    diagnostics = load_run_diagnostics_from_db(db, run_id="ui", landscape_run_id=run_id, run_status="failed")

    assert diagnostics.failure_detail is not None, "run drawer would degrade to the stored run error"
    assert diagnostics.failure_detail.node_id == sink_node_id
    assert diagnostics.failure_detail.operation_type == "sink_write"
    assert "input validation failed" in diagnostics.failure_detail.error_message


def test_failsink_boundary_failure_projects_failure_detail_at_the_failsink(tmp_path: Path) -> None:
    """The failsink that raised must win over the primary divert anchors.

    One boundary_error terminalizes the failsink states AND the primary divert
    anchors with identical text (elspeth-2a75af7f8f), so correlating on message
    alone leaves several equally-matching states and recency picks the primary —
    naming a node that did not raise. Pinned here because this fix is the first
    consumer of that pre-existing ambiguity.
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'failsink_ui.db'}")
    try:
        run_id, failsink_id = _run_failing_failsink(db)
        diagnostics = load_run_diagnostics_from_db(db, run_id="ui", landscape_run_id=run_id, run_status="failed")

        assert diagnostics.failure_detail is not None
        assert diagnostics.failure_detail.operation_type == "sink_write"
        assert diagnostics.failure_detail.node_id == failsink_id, (
            f"failure_detail names {diagnostics.failure_detail.node_id!r}, but the error text "
            f"says the failsink raised: {diagnostics.failure_detail.error_message[:80]!r}"
        )
    finally:
        db.close()
