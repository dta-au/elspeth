"""Focused coverage for meaningful DAG-builder validation legs."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts import FrameworkBugError
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.config import GateSettings, QueueSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.dag.wiring import WiredTransform


class _Source:
    name = "mock_source"
    output_schema = None
    _on_validation_failure = "discard"
    _output_schema_config: SchemaConfig | None = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else {"schema": {"mode": "observed"}}


class _Sink:
    name = "mock_sink"
    input_schema = None
    declared_required_fields: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        on_write_failure: str = "discard",
    ) -> None:
        self.config = config if config is not None else {}
        self._on_write_failure = on_write_failure

    def _reset_diversion_log(self) -> None:
        pass


class _Transform:
    input_schema = None
    output_schema = None
    on_error: str | None = None
    on_success: str | None = "output"
    creates_tokens = False
    declared_output_fields: frozenset[str] = frozenset()
    declared_input_fields: frozenset[str] = frozenset()
    declared_string_input_fields: frozenset[str] = frozenset()
    passes_through_input = False
    forwards_input_fields = False
    removed_input_fields: frozenset[str] = frozenset()

    def __init__(self, name: str = "mock_transform") -> None:
        self.name = name
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = SchemaConfig(mode="observed", fields=None)


def _source_settings(*, on_success: str) -> dict[str, SourceSettings]:
    return {"primary": SourceSettings(plugin="mock_source", on_success=on_success, options={})}


def test_invalid_sink_raw_schema_is_reported_as_graph_validation_error() -> None:
    with pytest.raises(GraphValidationError, match="Invalid schema config"):
        ExecutionGraph.from_plugin_instances(
            sources={"primary": _Source()},  # type: ignore[arg-type]
            source_settings_map=_source_settings(on_success="output"),
            sinks={"output": _Sink(config={"schema": {"mode": "invalid"}})},  # type: ignore[dict-item]
        )


def test_gate_rejects_upstream_source_without_schema_contract() -> None:
    gate = GateSettings(
        name="router",
        input="source_out",
        condition="True",
        routes={"true": "output", "false": "output"},
    )

    with pytest.raises(FrameworkBugError, match="has no output_schema_config"):
        ExecutionGraph.from_plugin_instances(
            sources={"primary": _Source(config={})},  # type: ignore[arg-type]
            source_settings_map=_source_settings(on_success="source_out"),
            sinks={"output": _Sink()},  # type: ignore[dict-item]
            gates=[gate],
        )


def test_declared_queue_without_producer_is_rejected_as_dangling() -> None:
    with pytest.raises(GraphValidationError, match=r"Dangling output connections.*'orphan'"):
        ExecutionGraph.from_plugin_instances(
            sources={"primary": _Source()},  # type: ignore[arg-type]
            source_settings_map=_source_settings(on_success="output"),
            sinks={"output": _Sink()},  # type: ignore[dict-item]
            queues={"orphan": QueueSettings()},
        )


def test_transform_on_error_rejects_unknown_sink_with_suggestion() -> None:
    transform = _Transform()
    wired = WiredTransform(
        plugin=transform,  # type: ignore[arg-type]
        settings=TransformSettings(
            name="worker",
            plugin=transform.name,
            input="source_out",
            on_success="output",
            on_error="outpt",
            options={},
        ),
    )

    with pytest.raises(GraphValidationError, match=r"unknown sink\. Did you mean: output\?"):
        ExecutionGraph.from_plugin_instances(
            sources={"primary": _Source()},  # type: ignore[arg-type]
            source_settings_map=_source_settings(on_success="source_out"),
            transforms=[wired],
            sinks={"output": _Sink()},  # type: ignore[dict-item]
        )


def test_gate_on_error_rejects_unknown_sink_with_suggestion() -> None:
    gate = GateSettings(
        name="router",
        input="source_out",
        condition="True",
        routes={"true": "output", "false": "output"},
        on_error="outpt",
    )

    with pytest.raises(GraphValidationError, match=r"unknown sink\. Did you mean: output\?"):
        ExecutionGraph.from_plugin_instances(
            sources={"primary": _Source()},  # type: ignore[arg-type]
            source_settings_map=_source_settings(on_success="source_out"),
            sinks={"output": _Sink()},  # type: ignore[dict-item]
            gates=[gate],
        )


def test_sink_on_write_failure_rejects_unknown_failsink() -> None:
    with pytest.raises(GraphValidationError, match="which is not in sink_ids"):
        ExecutionGraph.from_plugin_instances(
            sources={"primary": _Source()},  # type: ignore[arg-type]
            source_settings_map=_source_settings(on_success="output"),
            sinks={"output": _Sink(on_write_failure="missing")},  # type: ignore[dict-item]
        )
