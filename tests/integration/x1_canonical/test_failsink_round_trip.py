"""Canonical values delivered through a linked failsink effect."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
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
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.canonical import normalize_for_canonical, stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.spans import SpanFactory


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
