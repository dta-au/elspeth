# tests/unit/core/test_dag_queue_guarantee_propagation.py
"""Queue nodes are pass-through coordination: guarantee propagation must walk through them.

Sibling of the gate transparency fix (test_dag_gate_guarantee_propagation.py)
and the coalesce pass-through guarantee fix (6b431fd03, elspeth-0b14977817).
The builder assigns every queue node a hardcoded
``SchemaConfig(mode="observed", fields=None)`` ("V1 queue semantics"), so
``walk_effective_guarantee_vote`` stopped at the queue and reported an empty
guarantee. ``validate_edge_schemas`` then compared queue consumers against
``frozenset()`` and falsely rejected runnable source → queue → consumer
pipelines with "guarantees: (none - dynamic schema)" (elspeth-5a372d3267,
battery-2026-08-04 g08: an llm source guaranteeing llm_response feeding a
field_mapper that requires it).

Queues are the sanctioned fan-in point for ordinary nodes (graph invariant 7),
so unlike gates the walk must aggregate across multiple arms. The sound
fan-in rule differs from ``compose_propagation``'s abstainer-skip (which is
correct only when every predecessor delivers the SAME row): rows arrive from
exactly one arm, so

- all arms participate  → intersection of arm guarantees, participated=True
- any arm abstains      → the queue abstains entirely (an unconstrained arm
  means no field can be vouched for on every row), preserving the sink
  deferral design for dynamic upstreams (elspeth-3283f2eaec)
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts import NodeType
from elspeth.core.config import (
    QueueSettings,
    SourceSettings,
    TransformSettings,
)
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.guarantees import (
    get_effective_guaranteed_fields,
    walk_effective_guarantee_vote,
)
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.dag.wiring import WiredTransform


class _SourceWithGuarantees:
    """Mock source declaring guaranteed_fields via its config schema."""

    output_schema = None
    _on_validation_failure = "discard"

    def __init__(self, name: str, guaranteed: tuple[str, ...], on_success: str) -> None:
        self.name = name
        self.on_success = on_success
        if guaranteed:
            self.config = {"schema": {"mode": "observed", "guaranteed_fields": list(guaranteed)}}
        else:
            self.config = {"schema": {"mode": "observed"}}


class _RequiringTransform:
    """Mock transform with explicit required_input_fields in its config."""

    input_schema = None
    output_schema = None
    on_error: str | None = None
    on_success: str | None = "output"
    declared_output_fields: frozenset[str] = frozenset()
    declared_input_fields: frozenset[str] = frozenset()
    declared_string_input_fields: frozenset[str] = frozenset()
    passes_through_input: bool = False
    forwards_input_fields: bool = False
    removed_input_fields: frozenset[str] = frozenset()

    def __init__(self, name: str, required: tuple[str, ...]) -> None:
        self.name = name
        self.config = {
            "schema": {"mode": "observed"},
            "required_input_fields": list(required),
        }
        from elspeth.contracts.schema import SchemaConfig

        self._output_schema_config = SchemaConfig(mode="observed", fields=None)


class _BuilderMockSink:
    name = "mock_sink"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure: str = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


def _build_queue_graph(
    *,
    arms: dict[str, tuple[str, ...]],
    consumer_required: tuple[str, ...],
) -> ExecutionGraph:
    """arm sources (each guaranteeing its tuple; empty = dynamic) → queue → requiring consumer → sink."""
    sources = {name: _SourceWithGuarantees(f"mock_source_{name}", guaranteed, on_success="work_queue") for name, guaranteed in arms.items()}
    consumer = _RequiringTransform("queue_consumer", consumer_required)
    wired = [
        WiredTransform(
            plugin=consumer,  # type: ignore[arg-type]
            settings=TransformSettings(
                name="consumer", plugin=consumer.name, input="work_queue", on_success="output", on_error="discard", options={}
            ),
        ),
    ]
    return ExecutionGraph.from_plugin_instances(
        sources=sources,  # type: ignore[arg-type]
        source_settings_map={name: SourceSettings(plugin=src.name, on_success="work_queue", options={}) for name, src in sources.items()},
        transforms=wired,
        sinks={"output": _BuilderMockSink()},  # type: ignore[dict-item]
        aggregations={},
        queues={"work_queue": QueueSettings()},
    )


def _queue_node_id(graph: ExecutionGraph) -> str:
    queue_nodes = [n for n in graph.get_nodes() if n.node_type == NodeType.QUEUE]
    assert len(queue_nodes) == 1
    return queue_nodes[0].node_id


class TestQueueGuaranteeTransparency:
    def test_single_arm_guarantee_passes_through_queue(self) -> None:
        """The battery g08 shape: llm-style source → queue → consumer requiring a guaranteed field.

        Before the fix the build raised GraphValidationError on the
        queue → consumer edge: "Producer (queue:work_queue) guarantees:
        (none - dynamic schema)" despite the arm guaranteeing the field.
        """
        graph = _build_queue_graph(
            arms={"llm_arm": ("llm_response", "llm_response_usage", "llm_response_model")},
            consumer_required=("llm_response",),
        )
        assert get_effective_guaranteed_fields(graph, _queue_node_id(graph)) == frozenset(
            {"llm_response", "llm_response_usage", "llm_response_model"}
        )

    def test_fan_in_intersects_participating_arms(self) -> None:
        """Two participating arms: only fields guaranteed by EVERY arm survive."""
        graph = _build_queue_graph(
            arms={"arm_a": ("shared", "only_a"), "arm_b": ("shared", "only_b")},
            consumer_required=("shared",),
        )
        assert get_effective_guaranteed_fields(graph, _queue_node_id(graph)) == frozenset({"shared"})

    def test_fan_in_rejects_single_arm_field(self) -> None:
        """A field guaranteed by only one arm must NOT satisfy a queue consumer."""
        with pytest.raises(GraphValidationError, match="only_a"):
            _build_queue_graph(
                arms={"arm_a": ("shared", "only_a"), "arm_b": ("shared", "only_b")},
                consumer_required=("only_a",),
            )

    def test_abstaining_arm_collapses_queue_to_abstention(self) -> None:
        """One dynamic arm means the queue can vouch for nothing on every row.

        The consumer's requirement must still fail (fail-closed), and the
        queue's vote must ABSTAIN rather than participate-with-empty, so sink
        validation keeps deferring dynamic upstreams to per-row enforcement
        (elspeth-3283f2eaec).
        """
        with pytest.raises(GraphValidationError, match="llm_response"):
            _build_queue_graph(
                arms={"llm_arm": ("llm_response",), "dynamic_arm": ()},
                consumer_required=("llm_response",),
            )

        graph = _build_queue_graph(
            arms={"llm_arm": ("llm_response",), "dynamic_arm": ()},
            consumer_required=(),
        )
        vote = walk_effective_guarantee_vote(graph, _queue_node_id(graph), {})
        assert vote.fields == frozenset()
        assert vote.participated is False

    def test_all_arms_abstaining_queue_abstains(self) -> None:
        """Control: today's behavior for fully dynamic upstreams is unchanged."""
        graph = _build_queue_graph(
            arms={"dyn_a": (), "dyn_b": ()},
            consumer_required=(),
        )
        vote = walk_effective_guarantee_vote(graph, _queue_node_id(graph), {})
        assert vote.fields == frozenset()
        assert vote.participated is False
