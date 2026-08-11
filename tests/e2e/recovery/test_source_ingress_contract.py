"""Production source-ingress contracts for TS-02, PB-01, and F-11.

These tests enter through ``Orchestrator.run`` and inspect the complete durable
run image.  Source-exclusion assertions therefore cover both the scheduler row
and its mandatory event journal, rather than inferring exclusion from a mocked
repository call.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts import NodeStateStatus, RunStatus, SourceRow, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import SourceGuaranteedFieldsViolation
from elspeth.core.landscape import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from tests.fixtures.base_classes import _TestSchema, _TestSourceBase, as_sink, as_source
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink, ListSource
from tests.helpers.state_engine import STATE_ENGINE_TABLES, StateEngineImage, capture_state_engine_image


class _SynchronousLoadFailureSource(_TestSourceBase):
    name = "synchronous_load_failure"
    output_schema = _TestSchema

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        del ctx
        raise FileNotFoundError("source-load-failed-before-iterator")


class _FirstNextFailureIterator:
    def __iter__(self) -> Iterator[SourceRow]:
        return self

    def __next__(self) -> SourceRow:
        raise FileNotFoundError("source-iterator-failed-before-row")


class _FirstNextFailureSource(_TestSourceBase):
    name = "first_next_failure"
    output_schema = _TestSchema

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        del ctx
        return _FirstNextFailureIterator()


class _QuarantineSource(_TestSourceBase):
    name = "quarantine_source"
    output_schema = _TestSchema
    _on_validation_failure = "quarantine"

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        del ctx
        yield SourceRow.quarantined(
            row={"value": "not-an-integer"},
            error="value must be an integer",
            destination="quarantine",
            source_row_index=7,
        )


def _run_image(
    tmp_path: Path,
    *,
    run_id: str,
    source: _TestSourceBase,
    sinks: dict[str, CollectSink],
) -> tuple[StateEngineImage, RunStatus]:
    db = LandscapeDB(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        payload_store = FilesystemPayloadStore(tmp_path / "payloads")
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={name: as_sink(sink) for name, sink in sinks.items()},
        )
        result = Orchestrator(db).run(
            config,
            graph=build_production_graph(config),
            payload_store=payload_store,
            run_id=run_id,
        )
        return capture_state_engine_image(db, run_id=run_id), result.status
    finally:
        db.close()


def _failed_run_image(
    tmp_path: Path,
    *,
    run_id: str,
    source: _TestSourceBase,
    expected_error: type[Exception],
    match: str,
) -> StateEngineImage:
    db = LandscapeDB(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        payload_store = FilesystemPayloadStore(tmp_path / "payloads")
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={"output": as_sink(CollectSink("output"))},
        )
        with pytest.raises(expected_error, match=match):
            Orchestrator(db).run(
                config,
                graph=build_production_graph(config),
                payload_store=payload_store,
                run_id=run_id,
            )
        return capture_state_engine_image(db, run_id=run_id)
    finally:
        db.close()


def _assert_complete_image(image: StateEngineImage) -> None:
    assert set(image.tables) == set(STATE_ENGINE_TABLES)
    assert len(image.tables["runs"]) == 1


def _assert_no_scheduler_work(image: StateEngineImage) -> None:
    assert image.tables["token_work_items"] == ()
    assert image.tables["scheduler_events"] == ()


def test_accepted_source_row_enters_complete_ts02_and_terminalizes(tmp_path: Path) -> None:
    source = ListSource([{"value": 42}], name="accepted_source", on_success="output")
    sink = CollectSink("output")

    image, status = _run_image(
        tmp_path,
        run_id="run-source-ingress-accepted",
        source=source,
        sinks={"output": sink},
    )

    _assert_complete_image(image)
    assert status == RunStatus.COMPLETED
    assert sink.results == [{"value": 42}]
    assert len(image.tables["rows"]) == 1
    assert len(image.tables["tokens"]) == 1
    assert [
        (row["status"], row["step_index"], row["attempt"]) for row in image.tables["node_states"] if row["node_id"] == source.node_id
    ] == [(NodeStateStatus.COMPLETED.value, 0, 0)]
    assert [(row["status"], row["attempt"]) for row in image.tables["token_work_items"]] == [("terminal", 1)]
    event_types = tuple(str(row["event_type"]) for row in image.tables["scheduler_events"])
    assert sorted(event_types) == [
        "claim_ready",
        "enqueue",
        "mark_pending_sink",
        "mark_pending_sink_terminal",
    ]
    assert [(row["outcome"], row["path"], row["completed"]) for row in image.tables["token_outcomes"]] == [
        (TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value, 1)
    ]
    assert image.tables["run_sources"][0]["lifecycle_state"] == "exhausted"
    assert image.tables["runs"][0]["status"] == RunStatus.COMPLETED.value


def test_yielded_row_boundary_failure_records_audit_without_scheduler_admission(tmp_path: Path) -> None:
    source = ListSource([{"value": 42}], name="boundary_failure_source", on_success="output")
    source.declared_guaranteed_fields = frozenset({"required_field"})

    image = _failed_run_image(
        tmp_path,
        run_id="run-source-ingress-boundary-failure",
        source=source,
        expected_error=SourceGuaranteedFieldsViolation,
        match="required_field",
    )

    _assert_complete_image(image)
    _assert_no_scheduler_work(image)
    assert len(image.tables["rows"]) == 1
    assert len(image.tables["tokens"]) == 1
    assert [(row["status"], row["step_index"], row["attempt"]) for row in image.tables["node_states"]] == [
        (NodeStateStatus.FAILED.value, 0, 0)
    ]
    assert [(row["outcome"], row["path"], row["completed"]) for row in image.tables["token_outcomes"]] == [
        (TerminalOutcome.FAILURE.value, TerminalPath.UNROUTED.value, 1)
    ]
    assert [(row["operation_type"], row["status"]) for row in image.tables["operations"]] == [("source_load", "failed")]
    assert image.tables["run_sources"][0]["lifecycle_state"] == "loading"
    assert image.tables["runs"][0]["status"] == RunStatus.FAILED.value


@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param(_SynchronousLoadFailureSource(), "source-load-failed-before-iterator", id="synchronous-load"),
        pytest.param(_FirstNextFailureSource(), "source-iterator-failed-before-row", id="first-next"),
    ],
)
def test_pre_row_source_failure_records_operation_without_row_token_or_scheduler_work(
    tmp_path: Path,
    source: _TestSourceBase,
    message: str,
) -> None:
    image = _failed_run_image(
        tmp_path,
        run_id=f"run-source-ingress-{source.name}",
        source=source,
        expected_error=FileNotFoundError,
        match=message,
    )

    _assert_complete_image(image)
    _assert_no_scheduler_work(image)
    assert image.tables["rows"] == ()
    assert image.tables["tokens"] == ()
    assert image.tables["node_states"] == ()
    assert image.tables["token_outcomes"] == ()
    assert [(row["operation_type"], row["status"], row["error_message"]) for row in image.tables["operations"]] == [
        ("source_load", "failed", message)
    ]
    assert image.tables["run_sources"][0]["lifecycle_state"] == "loading"
    assert image.tables["runs"][0]["status"] == RunStatus.FAILED.value


def test_quarantined_source_row_records_diversion_and_never_enters_scheduler(tmp_path: Path) -> None:
    source = _QuarantineSource()
    output_sink = CollectSink("output")
    quarantine_sink = CollectSink("quarantine")

    image, status = _run_image(
        tmp_path,
        run_id="run-source-ingress-quarantine",
        source=source,
        sinks={"output": output_sink, "quarantine": quarantine_sink},
    )

    _assert_complete_image(image)
    _assert_no_scheduler_work(image)
    assert status == RunStatus.COMPLETED_WITH_FAILURES
    assert output_sink.results == []
    assert quarantine_sink.results == [{"value": "not-an-integer"}]
    assert [(row["source_row_index"], row["ingest_sequence"]) for row in image.tables["rows"]] == [(7, 0)]
    assert len(image.tables["tokens"]) == 1
    source_states = [row for row in image.tables["node_states"] if row["node_id"] == source.node_id]
    assert [(row["status"], row["step_index"]) for row in source_states] == [(NodeStateStatus.FAILED.value, 0)]
    assert "value must be an integer" in str(source_states[0]["error_json"])
    assert len(image.tables["routing_events"]) == 1
    assert image.tables["routing_events"][0]["mode"] == "divert"
    assert [(row["outcome"], row["path"], row["sink_name"], row["completed"]) for row in image.tables["token_outcomes"]] == [
        (TerminalOutcome.FAILURE.value, TerminalPath.QUARANTINED_AT_SOURCE.value, "quarantine", 1)
    ]
    assert image.tables["run_sources"][0]["lifecycle_state"] == "exhausted"
    assert image.tables["runs"][0]["status"] == RunStatus.COMPLETED_WITH_FAILURES.value
