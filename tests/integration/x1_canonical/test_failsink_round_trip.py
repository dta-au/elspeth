"""Canonical values delivered through a linked failsink effect."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.fixtures.base_classes import as_sink
from tests.fixtures.landscape import leader_coordination_token
from tests.fixtures.sink_effects import DuplicateObservableTarget, PartitioningObservableSink
from tests.integration.pipeline.test_builtin_sink_effect_recovery import (
    _begin,
    _csv_sink,
    _json_sink,
    _pending_success,
    _register_sink,
    _tokens,
)

from elspeth.contracts import RoutingMode
from elspeth.contracts.audit import NodeStateOpen
from elspeth.contracts.diversion import RowDiversion
from elspeth.contracts.enums import NodeStateStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.canonical import normalize_for_canonical, stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, sink_effects_table
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.spans import SpanFactory


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf")], ids=["nan", "infinity"])
def test_noncanonical_diversion_refused_before_failsink_state(tmp_path: Path, nonfinite: float) -> None:
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        factory, run_id, source_id = _begin(db)
        failsink_id = _register_sink(factory, run_id, name="quarantine", plugin_name="json")
        (token,) = _tokens(factory, run_id=run_id, source_id=source_id, rows=[{"id": 1}])
        failsink = _json_sink(tmp_path / "failsink.jsonl")
        failsink.node_id = failsink_id
        state = NodeStateOpen(
            state_id="primary-state",
            token_id=token.token_id,
            node_id=source_id,
            step_index=0,
            attempt=0,
            status=NodeStateStatus.OPEN,
            input_hash="0" * 64,
            started_at=datetime.now(UTC),
        )
        executor = SinkExecutor(
            factory.execution,
            factory.data_flow,
            SpanFactory(),
            run_id,
            factory=factory,
            worker_id="worker-a",
            coordination_token=leader_coordination_token(factory, run_id),
        )
        ctx = PluginContext(run_id=run_id, config={}, landscape=factory.plugin_audit_writer(), node_id=failsink_id)
        with pytest.raises(AuditIntegrityError, match="non-canonical sink-boundary input") as exc_info:
            executor._handle_failsink_effect_diversions(
                failsink=as_sink(failsink),
                failsink_name="quarantine",
                failsink_effect_mode="write",
                failsink_edge_id="failsink-edge",
                primary_effect_id_by_token={},
                primary_divert_states=[(token, 0, state)],
                diversion_by_index={0: RowDiversion(row_index=0, reason="diverted", row_data={"id": 1, "value": nonfinite})},
                diversion_error_hashes={0: "0" * 16},
                diversion_reason_hashes={0: "0" * 64},
                sink_name="primary",
                step_in_pipeline=1,
                ctx=ctx,
                on_token_written=None,
            )
        assert isinstance(exc_info.value.__cause__, ValueError)
        with db.engine.connect() as conn:
            failsink_effects = conn.execute(
                select(func.count())
                .select_from(sink_effects_table)
                .where(sink_effects_table.c.run_id == run_id, sink_effects_table.c.role == "failsink")
            ).scalar_one()
            failsink_states = conn.execute(
                select(func.count())
                .select_from(node_states_table)
                .where(node_states_table.c.run_id == run_id, node_states_table.c.node_id == failsink_id)
            ).scalar_one()
        assert failsink_effects == 0
        assert failsink_states == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(
            {"id": 1, "divert": True, "when": datetime(2026, 1, 2, tzinfo=UTC), "amount": Decimal("12.50")},
            {"when": "2026-01-02T00:00:00+00:00", "amount": "12.50"},
            id="datetime-and-decimal",
        ),
        pytest.param(
            {"id": 1, "divert": True, "blob": b"abc", "nested": {"__elspeth_canonical_type__": "user-value"}},
            {"blob": "YWJj", "nested": {"__elspeth_canonical_type__": "user-value"}},
            id="bytes-and-reserved-key",
        ),
    ],
)
def test_raw_values_reach_failsink_in_json_form(tmp_path: Path, row: dict[str, object], expected: dict[str, object]) -> None:
    output = tmp_path / "failsink.jsonl"
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        factory, run_id, source_id = _begin(db)
        primary_id = _register_sink(factory, run_id, name="primary", plugin_name="partitioning")
        failsink_id = _register_sink(factory, run_id, name="quarantine", plugin_name="json")
        edge = factory.data_flow.register_edge(
            primary_id, failsink_id, "__failsink__", RoutingMode.DIVERT, coordination_token=leader_coordination_token(factory, run_id)
        )
        (token,) = _tokens(
            factory,
            run_id=run_id,
            source_id=source_id,
            rows=[row],
        )
        primary = PartitioningObservableSink(DuplicateObservableTarget(), name="primary")
        primary.node_id = primary_id
        failsink = _json_sink(output)
        failsink.node_id = failsink_id
        ctx = PluginContext(run_id=run_id, config={}, landscape=factory.plugin_audit_writer(), node_id=primary_id)

        SinkExecutor(
            factory.execution,
            factory.data_flow,
            SpanFactory(),
            run_id,
            factory=factory,
            worker_id="worker-a",
            coordination_token=leader_coordination_token(factory, run_id),
        ).write(
            as_sink(primary),
            [token],
            ctx,
            1,
            sink_name="primary",
            pending_outcome=_pending_success(),
            effect_mode="write",
            failsink=failsink,
            failsink_name="quarantine",
            failsink_effect_mode="write",
            failsink_edge_id=edge.edge_id,
            join_group_id_by_token={token.token_id: None},
        )

        rows = [json.loads(line) for line in output.read_text().splitlines()]
        assert len(rows) == 1
        for key, value in expected.items():
            assert rows[0][key] == value
        assert rows[0]["__diverted_from"] == "primary"
        assert rows[0]["__diversion_reason"].startswith("effect-diversion:")
    finally:
        db.close()


@pytest.mark.parametrize("format_name", ["jsonl", "csv"])
def test_json_only_failsink_keeps_base_output_form(tmp_path: Path, format_name: str) -> None:
    row: dict[str, object] = {"id": 1, "divert": True, "price": 1.0, "ratio": 2.5, "neg": -0.0}
    output = tmp_path / ("failsink.jsonl" if format_name == "jsonl" else "failsink.csv")
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        factory, run_id, source_id = _begin(db)
        primary_id = _register_sink(factory, run_id, name="primary", plugin_name="partitioning")
        failsink_id = _register_sink(factory, run_id, name="quarantine", plugin_name="json" if format_name == "jsonl" else "csv")
        edge = factory.data_flow.register_edge(
            primary_id, failsink_id, "__failsink__", RoutingMode.DIVERT, coordination_token=leader_coordination_token(factory, run_id)
        )
        (token,) = _tokens(factory, run_id=run_id, source_id=source_id, rows=[row])
        primary = PartitioningObservableSink(DuplicateObservableTarget(), name="primary")
        primary.node_id = primary_id
        failsink = _json_sink(output) if format_name == "jsonl" else _csv_sink(output)
        failsink.node_id = failsink_id
        ctx = PluginContext(run_id=run_id, config={}, landscape=factory.plugin_audit_writer(), node_id=primary_id)
        SinkExecutor(
            factory.execution,
            factory.data_flow,
            SpanFactory(),
            run_id,
            factory=factory,
            worker_id="worker-a",
            coordination_token=leader_coordination_token(factory, run_id),
        ).write(
            as_sink(primary),
            [token],
            ctx,
            1,
            sink_name="primary",
            pending_outcome=_pending_success(),
            effect_mode="write",
            failsink=as_sink(failsink),
            failsink_name="quarantine",
            failsink_effect_mode="write",
            failsink_edge_id=edge.edge_id,
            join_group_id_by_token={token.token_id: None},
        )
        if format_name == "jsonl":
            line = output.read_bytes().splitlines()[0]
            result = json.loads(line)
            expected = {
                **row,
                "__diversion_reason": result["__diversion_reason"],
                "__diverted_from": "primary",
                "__diversion_timestamp": result["__diversion_timestamp"],
            }
            assert line == json.dumps(expected).encode()
            assert stable_hash(row) == stable_hash({key: result[key] for key in row})
            assert stable_hash(normalize_for_canonical(row)) == stable_hash(row)
        else:
            with output.open(newline="") as stream:
                reader = csv.DictReader(stream)
                assert reader.fieldnames == [*row, "__diversion_reason", "__diverted_from", "__diversion_timestamp"]
                assert len(list(reader)) == 1
    finally:
        db.close()
