"""Required CSV retention exposes recorded causes without changing source rows."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import SinkEffectCapabilityError
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.secret_scrub import REDACTED_SECRET_TEXT
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.query_repository import QueryRepository
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.executors.sink_error_reason import resolve_sink_error_reason
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.transforms.json_explode import JSONExplode
from tests.fixtures.base_classes import as_sink, as_source, as_transform, inject_write_failure
from tests.fixtures.landscape import make_factory, make_landscape_db
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink
from tests.integration.pipeline.orchestrator.test_quarantine_routing import QuarantineSource


def _retention(target: Path) -> CSVSink:
    return inject_write_failure(
        CSVSink(
            {
                "path": str(target),
                "schema": {"mode": "observed"},
                "require_all_rows": True,
                "include_audit_context": True,
                "include_error_reason": True,
            }
        ),
        "discard",
    )


def test_source_quarantine_retention_writes_actual_readable_cause(tmp_path: Path, payload_store, monkeypatch) -> None:
    reason = "Required amount must be an integer."
    original = {"application_id": "case-1", "amount": "invalid"}
    target = tmp_path / "failed_rows.csv"
    source = QuarantineSource(
        valid_rows=[],
        quarantine_rows=[(original, reason)],
        quarantine_destination="failed_rows",
        on_validation_failure="failed_rows",
    )
    retention = _retention(target)
    ordinary = CollectSink("default")
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"default": as_sink(ordinary), "failed_rows": as_sink(retention)},
    )
    db = make_landscape_db()
    try:
        result = Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert result.status is RunStatus.COMPLETED_WITH_FAILURES
        assert result.rows_quarantined == 1
        assert ordinary.results == []
        with target.open(newline="") as stream:
            records = list(csv.DictReader(stream))
        assert len(records) == 1
        record = records[0]
        assert record["run_id"] == result.run_id
        assert record["row_id"] and record["token_id"]
        assert record["path"] == "quarantined_at_source"
        assert record["error_hash"] == compute_error_hash(reason)
        assert record["error_reason"] == reason
        assert json.loads(record["row_json"]) == original
        factory = make_factory(db, payload_store=payload_store)
        items = factory.query.get_sink_error_handoffs(run_id=result.run_id, token_id=record["token_id"], row_id=record["row_id"])
        assert len(items) == 1
        effects = factory.execution.sink_effects.get_effects_for_run(result.run_id)
        assert len(effects) == 1
        witnesses = []
        for tail in ("first-tail", "different-tail"):
            full_message = "\u00e9" * 3000 + tail
            selected = replace(items[0], pending_error_message=full_message, pending_error_hash=compute_error_hash(full_message))
            monkeypatch.setattr(QueryRepository, "get_sink_error_handoffs", lambda *args, selected=selected, **kwargs: (selected,))
            witness = resolve_sink_error_reason(
                factory,
                run_id=result.run_id,
                token_id=record["token_id"],
                row_id=record["row_id"],
                sink_node_id=effects[0].sink_node_id,
                sink_name="failed_rows",
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.QUARANTINED_AT_SOURCE,
                error_hash=selected.pending_error_hash,
            )
            assert len(witness.text) == 2048 and witness.text.endswith(" [truncated]")
            assert tail not in witness.text
            witnesses.append(witness)
        assert witnesses[0].text == witnesses[1].text
        assert witnesses[0].witness_hash != witnesses[1].witness_hash  # The full undisplayed tail remains bound.
    finally:
        db.close()


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "message", "sink", "row", "token", "run", "hash", "path"])
def test_required_reason_refuses_corrupt_handoff_before_publication(tmp_path: Path, payload_store, monkeypatch, corruption: str) -> None:
    target = tmp_path / "failed_rows.csv"
    source = QuarantineSource(
        valid_rows=[],
        quarantine_rows=[({"amount": "invalid"}, "Amount must be an integer.")],
        quarantine_destination="failed_rows",
        on_validation_failure="failed_rows",
    )
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"default": as_sink(CollectSink("default")), "failed_rows": as_sink(_retention(target))},
    )
    original = QueryRepository.get_sink_error_handoffs

    def corrupt(self, **kwargs):
        items = original(self, **kwargs)
        assert len(items) == 1  # The real query supplies the positive control.
        if corruption == "missing":
            return ()
        if corruption == "duplicate":
            return (*items, *items)
        changes = {
            "message": {"pending_error_message": "A different cause."},
            "sink": {"pending_sink_name": "another_sink"},
            "row": {"row_id": "another_row"},
            "token": {"token_id": "another_token"},
            "run": {"run_id": "another_run"},
            "hash": {"pending_error_hash": "f" * 16},
            "path": {"pending_path": "on_error_routed"},
        }
        return (replace(items[0], **changes[corruption]),)

    monkeypatch.setattr(QueryRepository, "get_sink_error_handoffs", corrupt)
    db = make_landscape_db()
    try:
        with pytest.raises(LandscapeRecordError, match="Readable CSV error retention"):
            Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []
    finally:
        db.close()


@pytest.mark.parametrize("value", ["true", 1, None])
def test_readable_reason_configuration_requires_strict_boolean(tmp_path: Path, value: object) -> None:
    with pytest.raises(PluginConfigError, match="include_error_reason"):
        CSVSink({"path": str(tmp_path / "failed.csv"), "schema": {"mode": "observed"}, "include_error_reason": value})


def test_readable_reason_configuration_requires_audit_context(tmp_path: Path) -> None:
    with pytest.raises(PluginConfigError, match="include_error_reason requires include_audit_context"):
        CSVSink({"path": str(tmp_path / "failed.csv"), "schema": {"mode": "observed"}, "include_error_reason": True})


@pytest.mark.parametrize("reason", [REDACTED_SECRET_TEXT, "{'_redaction_status': 'credential_scrub_failure'}"])
def test_required_reason_refuses_already_redacted_cause(tmp_path: Path, payload_store, reason: str) -> None:
    target = tmp_path / "failed_rows.csv"
    source = QuarantineSource(
        valid_rows=[],
        quarantine_rows=[({"amount": "invalid"}, reason)],
        quarantine_destination="failed_rows",
        on_validation_failure="failed_rows",
    )
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"default": as_sink(CollectSink("default")), "failed_rows": as_sink(_retention(target))},
    )
    db = make_landscape_db()
    try:
        with pytest.raises(SinkEffectCapabilityError, match="unavailable under credential redaction"):
            Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert not target.exists()
    finally:
        db.close()


@pytest.mark.parametrize("corruption", [None, "message", "sink", "witness-id"])
def test_source_and_transform_causes_survive_cumulative_csv_rendering(tmp_path: Path, payload_store, monkeypatch, corruption) -> None:
    target = tmp_path / "failed_rows.csv"
    quarantine_reason = "Required case amount is unavailable."
    source = QuarantineSource(
        valid_rows=[{"id": "transform-failure", "items": "not-a-list"}],
        quarantine_rows=[({"id": "source-failure", "items": None}, quarantine_reason)],
        quarantine_destination="failed_rows",
        on_validation_failure="failed_rows",
    )
    transform = JSONExplode({"schema": {"mode": "observed"}, "array_field": "items"})
    transform.on_error = "failed_rows"
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform)],
        sinks={"default": as_sink(CollectSink("default")), "failed_rows": as_sink(_retention(target))},
    )
    db = make_landscape_db()
    original_query = QueryRepository.get_sink_error_handoffs
    predecessor_preimages = []

    def predecessor_evidence(self, **kwargs):
        items = original_query(self, **kwargs)
        if items and items[0].status is TokenWorkStatus.TERMINAL:
            predecessor_preimages.append(target.read_bytes())
            if corruption is not None:
                changes = {
                    "message": {"pending_error_message": "Different cause."},
                    "sink": {"pending_sink_name": "foreign-sink"},
                    "witness-id": {"work_item_id": "f" * 64},
                }
                return (replace(items[0], **changes[corruption]),)
        return items

    monkeypatch.setattr(QueryRepository, "get_sink_error_handoffs", predecessor_evidence)
    try:
        if corruption is not None:
            with pytest.raises(LandscapeRecordError, match=r"cause|membership"):
                Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
            assert predecessor_preimages
            assert target.read_bytes() == predecessor_preimages[0]
            return
        result = Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
        assert predecessor_preimages  # This actually traversed cumulative restoration.
        with target.open(newline="") as stream:
            records = list(csv.DictReader(stream))
        assert len(records) == 2
        by_id = {json.loads(record["row_json"])["id"]: record for record in records}
        assert by_id["source-failure"]["error_reason"] == quarantine_reason
        transform_record = by_id["transform-failure"]
        assert transform_record["path"] == "on_error_routed"
        assert "wrong_type" in transform_record["error_reason"]
        assert "not-a-list" not in transform_record["error_reason"]
        assert transform_record["error_hash"] == compute_error_hash(transform_record["error_reason"])
        assert {record["run_id"] for record in records} == {result.run_id}
        assert len({record["token_id"] for record in records}) == 2
    finally:
        db.close()
