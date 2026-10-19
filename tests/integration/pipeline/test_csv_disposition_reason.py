"""Readable retention distinguishes intentional gate routing from failures."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from elspeth.contracts import RoutingMode, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import NodeStateCompleted
from elspeth.contracts.errors import SinkEffectCapabilityError
from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.config import GateSettings
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.query_repository import QueryRepository
from elspeth.engine.executors.sink_disposition_reason import resolve_sink_disposition_reason
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.transforms.json_explode import JSONExplode
from tests.fixtures.base_classes import as_sink, as_source, as_transform, inject_write_failure
from tests.fixtures.landscape import make_factory, make_landscape_db
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink
from tests.integration.pipeline.orchestrator.test_quarantine_routing import QuarantineSource


def _configuration(target: Path, *, error_only: bool = False, mixed: bool = False) -> tuple[PipelineConfig, CollectSink]:
    valid_rows = [{"id": "first", "accepted": False}, {"id": "second", "accepted": False}, {"id": "third", "accepted": True}]
    if mixed:
        valid_rows = [dict(row, items=[1]) for row in valid_rows]
        valid_rows.insert(0, {"id": "transform", "accepted": True, "items": "invalid"})
    source = QuarantineSource(
        valid_rows=valid_rows,
        quarantine_rows=[({"id": "source"}, "The required amount is missing.")] if mixed else [],
        quarantine_destination="retained",
        on_validation_failure="retained",
    )
    retention = inject_write_failure(
        CSVSink(
            {
                "path": str(target),
                "schema": {"mode": "observed"},
                "require_all_rows": True,
                "include_audit_context": True,
                "include_error_reason" if error_only else "include_disposition_reason": True,
            }
        ),
        "discard",
    )
    ordinary = CollectSink("default")
    transform = JSONExplode({"schema": {"mode": "observed"}, "array_field": "items", "output_field": "member"})
    transform.on_error = "retained"
    return (
        PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)] if mixed else [],
            gates=[
                GateSettings(
                    name="quality_check", input="checked", condition="row['accepted']", routes={"true": "default", "false": "retained"}
                )
            ],
            sinks={"default": as_sink(ordinary), "retained": as_sink(retention)},
        ),
        ordinary,
    )


def test_boolean_gate_retention_has_verified_disposition_without_an_error(tmp_path: Path, payload_store) -> None:
    target = tmp_path / "retained.csv"
    config, ordinary = _configuration(target)
    db = make_landscape_db()
    try:
        result = Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert result.status is RunStatus.COMPLETED
        assert result.rows_failed == 0
        assert [row["id"] for row in ordinary.results] == ["third"]
        with target.open(newline="") as stream:
            records = list(csv.DictReader(stream))
        assert [json.loads(record["row_json"])["id"] for record in records] == ["first", "second"]
        assert len({record["token_id"] for record in records}) == 2
        assert len({record["row_id"] for record in records}) == 2
        factory = make_factory(db, payload_store=payload_store)
        effects = factory.execution.sink_effects.get_effects_for_run(result.run_id)
        assert effects
        for record in records:
            assert record["outcome"] == TerminalOutcome.SUCCESS.value
            assert record["path"] == TerminalPath.GATE_ROUTED.value
            assert record["error_hash"] == ""
            assert record["disposition_kind"] == "gate_route"
            assert record["disposition_reason"] == "Gate quality_check selected false and routed this row to retained."
            outcomes = factory.query.get_token_outcomes_for_tokens(result.run_id, (record["token_id"],))
            completed = [outcome for outcome in outcomes if outcome.completed]
            assert len(completed) == 1
            assert completed[0].outcome is TerminalOutcome.SUCCESS
            assert completed[0].error_hash is None
            # A new repository factory reconstructs the real proof after the
            # scheduler has terminalized and purged its live row payload.
            reason = resolve_sink_disposition_reason(
                factory,
                run_id=result.run_id,
                token_id=record["token_id"],
                row_id=record["row_id"],
                sink_node_id=effects[0].sink_node_id,
                sink_name="retained",
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.GATE_ROUTED,
                error_hash=None,
                row=json.loads(record["row_json"]),
            )
            assert reason.text == record["disposition_reason"]
    finally:
        db.close()


def test_error_only_csv_preserves_refusal_of_successful_gate_route(tmp_path: Path, payload_store) -> None:
    target = tmp_path / "retained.csv"
    config, _ordinary = _configuration(target, error_only=True)
    db = make_landscape_db()
    try:
        with pytest.raises(SinkEffectCapabilityError, match="exact durable source or transform failure"):
            Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert not target.exists()
    finally:
        db.close()


def test_disposition_mode_refuses_a_string_route_with_boolean_spelling(tmp_path: Path, payload_store) -> None:
    target = tmp_path / "retained.csv"
    config, _ordinary = _configuration(target)
    config = replace(
        config,
        gates=[GateSettings(name="quality_check", input="checked", condition="'false'", routes={"true": "default", "false": "retained"})],
    )
    db = make_landscape_db()
    try:
        with pytest.raises(LandscapeRecordError, match="Boolean route configuration"):
            Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert not target.exists()
    finally:
        db.close()


@pytest.mark.parametrize("corruption", [None, "sink", "witness"])
def test_mixed_disposition_predecessors_retain_each_original_cause(tmp_path: Path, payload_store, monkeypatch, corruption) -> None:
    target = tmp_path / "retained.csv"
    config, ordinary = _configuration(target, mixed=True)
    query = QueryRepository.get_sink_error_handoffs
    predecessor_bytes = []

    def handoffs(self, **kwargs):
        items = query(self, **kwargs)
        if items and items[0].status is TokenWorkStatus.TERMINAL:
            predecessor_bytes.append(target.read_bytes())
            if corruption == "sink":
                return (replace(items[0], pending_sink_name="another_sink"),)
            if corruption == "witness":
                return (replace(items[0], work_item_id="f" * 64),)
        return items

    monkeypatch.setattr(QueryRepository, "get_sink_error_handoffs", handoffs)
    db = make_landscape_db()
    try:
        if corruption is not None:
            with pytest.raises(LandscapeRecordError):
                Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
            assert predecessor_bytes
            assert target.read_bytes() == predecessor_bytes[0]
            return
        result = Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert result.status is RunStatus.COMPLETED_WITH_FAILURES
        assert [row["id"] for row in ordinary.results] == ["third"]
        assert predecessor_bytes
        with target.open(newline="") as stream:
            records = list(csv.DictReader(stream))
        by_id = {json.loads(record["row_json"])["id"]: record for record in records}
        assert len(records) == len(by_id) == 4
        assert by_id["first"]["disposition_kind"] == by_id["second"]["disposition_kind"] == "gate_route"
        assert by_id["source"]["disposition_reason"] == "The required amount is missing."
        assert by_id["source"]["disposition_kind"] == by_id["transform"]["disposition_kind"] == "error"
        assert "wrong_type" in by_id["transform"]["disposition_reason"]
        assert by_id["first"]["error_hash"] == by_id["second"]["error_hash"] == ""
    finally:
        db.close()


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "hash", "context", "event-mode", "reason-ref"])
def test_gate_disposition_refuses_incomplete_or_contradictory_evidence(tmp_path: Path, payload_store, monkeypatch, corruption) -> None:
    target = tmp_path / "retained.csv"
    config, _ordinary = _configuration(target)
    states_query = QueryRepository.get_node_states_for_token
    events_query = QueryRepository.get_routing_events
    controlled = []

    def states(self, token_id):
        rows = states_query(self, token_id)
        gates = [state for state in rows if type(state) is NodeStateCompleted and state.context_after_json is not None]
        assert len(gates) == 1
        gate = gates[0]
        controlled.append(gate.state_id)
        if corruption == "missing":
            return [state for state in rows if state.state_id != gate.state_id]
        if corruption == "duplicate":
            return [*rows, gate]
        if corruption == "hash":
            return [replace(state, output_hash="f" * 64) if state.state_id == gate.state_id else state for state in rows]
        if corruption == "context":
            context = json.loads(gate.context_after_json)
            assert context["result"] == "False"
            context["result"] = "false"  # A string route is not proof of a Boolean result.
            return [replace(state, context_after_json=json.dumps(context)) if state.state_id == gate.state_id else state for state in rows]
        return rows

    def events(self, state_id):
        rows = events_query(self, state_id)
        assert len(rows) == 1
        if corruption == "event-mode":
            return [replace(rows[0], mode=RoutingMode.COPY)]
        if corruption == "reason-ref":
            return [replace(rows[0], reason_ref=None)]
        return rows

    monkeypatch.setattr(QueryRepository, "get_node_states_for_token", states)
    monkeypatch.setattr(QueryRepository, "get_routing_events", events)
    db = make_landscape_db()
    try:
        with pytest.raises(LandscapeRecordError, match="Readable CSV gate retention"):
            Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert controlled
        assert not target.exists()
    finally:
        db.close()


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "sink", "error", "row-payload"])
def test_gate_disposition_refuses_divergent_handoff(tmp_path: Path, payload_store, monkeypatch, corruption: str) -> None:
    target = tmp_path / "retained.csv"
    config, _ordinary = _configuration(target)
    query = QueryRepository.get_sink_error_handoffs
    controlled = []

    def handoffs(self, **kwargs):
        items = query(self, **kwargs)
        assert len(items) == 1
        item = items[0]
        controlled.append(item.work_item_id)
        if corruption == "missing":
            return ()
        if corruption == "duplicate":
            return (*items, *items)
        if corruption == "sink":
            return (replace(item, pending_sink_name="foreign"),)
        if corruption == "error":
            return (replace(item, pending_error_message="Invented error."),)
        assert '"first"' in item.row_payload_json
        return (replace(item, row_payload_json=item.row_payload_json.replace('"first"', '"foreign"')),)

    monkeypatch.setattr(QueryRepository, "get_sink_error_handoffs", handoffs)
    db = make_landscape_db()
    try:
        with pytest.raises(LandscapeRecordError, match="Readable CSV gate retention"):
            Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert controlled
        assert not target.exists()
    finally:
        db.close()


@pytest.mark.parametrize("value", ["true", 1, None])
def test_disposition_reason_requires_actual_boolean(tmp_path: Path, value: object) -> None:
    with pytest.raises(PluginConfigError, match="include_disposition_reason"):
        CSVSink({"path": str(tmp_path / "retained.csv"), "schema": {"mode": "observed"}, "include_disposition_reason": value})


def test_disposition_reason_requires_audit_context_and_excludes_error_only_mode(tmp_path: Path) -> None:
    config = {"path": str(tmp_path / "retained.csv"), "schema": {"mode": "observed"}, "include_disposition_reason": True}
    with pytest.raises(PluginConfigError, match="requires include_audit_context"):
        CSVSink(config)
    with pytest.raises(PluginConfigError, match="mutually exclusive"):
        CSVSink({**config, "include_audit_context": True, "require_all_rows": True, "include_error_reason": True})


@pytest.mark.parametrize("corruption", ["missing", "changed"])
def test_gate_disposition_requires_the_actual_reason_payload(tmp_path: Path, payload_store, monkeypatch, corruption: str) -> None:
    target = tmp_path / "retained.csv"
    config, _ordinary = _configuration(target)
    events_query = QueryRepository.get_routing_events
    retrieve = type(payload_store).retrieve
    selected_refs = set()
    controlled = []

    def events(self, state_id):
        events = events_query(self, state_id)
        assert len(events) == 1 and events[0].reason_ref is not None
        selected_refs.add(events[0].reason_ref)
        return events

    def payload(self, content_hash):
        actual = retrieve(self, content_hash)
        if content_hash in selected_refs:
            assert json.loads(actual)["result"] == "false"
            controlled.append(content_hash)
            if corruption == "missing":
                raise PayloadNotFoundError(content_hash)
            return b'{"condition":"a different condition","result":"false"}'
        return actual

    monkeypatch.setattr(QueryRepository, "get_routing_events", events)
    monkeypatch.setattr(type(payload_store), "retrieve", payload)
    db = make_landscape_db()
    try:
        with pytest.raises((LandscapeRecordError, PayloadNotFoundError)):
            Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert controlled
        assert not target.exists()
    finally:
        db.close()
