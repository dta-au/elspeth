# tests/unit/engine/orchestrator/test_source_lifecycle_recorder.py
"""Unit tests for SourceLifecycleRecorder — the source-metadata recording seam.

Extracted from ``SourceIterationDriver`` (elspeth-27d7bfc14b). These drive the
collaborator in isolation: field-resolution recording (skip-when-None,
skip-when-unchanged, write-and-emit-on-change) and run_source lifecycle
recording with and without field-resolution evidence. The finalizer's use of
these methods stays covered by test_finalize_source_iteration.py.

Mock discipline: the Landscape recorder and ceremony are ``spec``-bound mocks
(the recording is asserted through them); the source input is a plain
``SimpleNamespace`` fake.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.events import FieldResolutionApplied
from elspeth.contracts.types import NodeID
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import RunSourceLifecycleState
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.engine.orchestrator.source_lifecycle_recorder import SourceLifecycleRecorder

# The recorder forwards the token BY VALUE (ADR-048 §3); it is the test's subject, so a
# direct construction is honest here — nothing below reads a seat.
_TOKEN = CoordinationToken(run_id="run-1", worker_id="worker:run-1:test", leader_epoch=1)


def _make_source(*, field_resolution: tuple[dict[str, str], str | None] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="rows",
        config={},
        get_field_resolution=lambda: field_resolution,
        output_schema=SimpleNamespace(model_json_schema=lambda: {}),
        get_schema_contract=lambda: None,
    )


class TestRecordFieldResolution:
    def test_none_resolution_records_nothing(self) -> None:
        ceremony = MagicMock(spec=RunCeremony)
        recorder = SourceLifecycleRecorder(ceremony=ceremony)
        factory = MagicMock(spec=RecorderFactory)

        result = recorder.record_field_resolution(factory, active_source=_make_source(field_resolution=None), coordination_token=_TOKEN)

        assert result is None
        factory.run_lifecycle.record_source_field_resolution.assert_not_called()
        ceremony.emit_telemetry.assert_not_called()

    def test_unchanged_snapshot_skips_write_and_telemetry(self) -> None:
        ceremony = MagicMock(spec=RunCeremony)
        recorder = SourceLifecycleRecorder(ceremony=ceremony)
        factory = MagicMock(spec=RecorderFactory)
        snapshot = ({"id": "id"}, "v1")

        result = recorder.record_field_resolution(
            factory,
            active_source=_make_source(field_resolution=snapshot),
            previously_recorded=snapshot,
            coordination_token=_TOKEN,
        )

        assert result == snapshot
        factory.run_lifecycle.record_source_field_resolution.assert_not_called()
        ceremony.emit_telemetry.assert_not_called()

    def test_grown_union_writes_and_emits(self) -> None:
        ceremony = MagicMock(spec=RunCeremony)
        recorder = SourceLifecycleRecorder(ceremony=ceremony)
        factory = MagicMock(spec=RecorderFactory)
        union = {"id": "id", "Extra Field": "extra_field"}

        result = recorder.record_field_resolution(
            factory,
            active_source=_make_source(field_resolution=(union, "v1")),
            previously_recorded=({"id": "id"}, "v1"),
            coordination_token=_TOKEN,
        )

        assert result == (union, "v1")
        factory.run_lifecycle.record_source_field_resolution.assert_called_once_with(
            resolution_mapping=union,
            normalization_version="v1",
            coordination_token=_TOKEN,
        )
        (event,), _ = ceremony.emit_telemetry.call_args
        assert isinstance(event, FieldResolutionApplied)
        assert event.field_count == len(union)
        assert event.normalization_version == "v1"


class TestRecordRunSourceLifecycle:
    def test_records_with_field_resolution(self) -> None:
        recorder = SourceLifecycleRecorder(ceremony=MagicMock(spec=RunCeremony))
        factory = MagicMock(spec=RecorderFactory)
        source = _make_source(field_resolution=({"id": "id"}, "v1"))

        recorder.record_run_source_lifecycle(
            factory,
            NodeID("source-node"),
            "rows",
            source,
            RunSourceLifecycleState.EXHAUSTED,
            coordination_token=_TOKEN,
        )

        kwargs = factory.run_lifecycle.record_run_source.call_args.kwargs
        assert kwargs["coordination_token"] is _TOKEN
        assert "run_id" not in kwargs
        assert kwargs["source_node_id"] == NodeID("source-node")
        assert kwargs["field_resolution_mapping"] == {"id": "id"}
        assert kwargs["normalization_version"] == "v1"
        assert kwargs["lifecycle_state"] is RunSourceLifecycleState.EXHAUSTED

    def test_records_without_field_resolution(self) -> None:
        recorder = SourceLifecycleRecorder(ceremony=MagicMock(spec=RunCeremony))
        factory = MagicMock(spec=RecorderFactory)
        source = _make_source(field_resolution=None)

        recorder.record_run_source_lifecycle(
            factory,
            NodeID("source-node"),
            "rows",
            source,
            RunSourceLifecycleState.INTERRUPTED,
            coordination_token=_TOKEN,
        )

        kwargs = factory.run_lifecycle.record_run_source.call_args.kwargs
        assert kwargs["field_resolution_mapping"] is None
        assert kwargs["normalization_version"] is None
        assert kwargs["lifecycle_state"] is RunSourceLifecycleState.INTERRUPTED
