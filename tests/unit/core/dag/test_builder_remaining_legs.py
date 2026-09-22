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
    observed_value_type: str | None = None

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
    preserves_input_values = False
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


def test_builder_sets_the_name_keyed_transform_id_map() -> None:
    """The name-keyed transform map is the diagnostics attribution authority
    (elspeth-9f21f3c57d): its keys are the settings names (composer node ids)
    and its values agree with the positional sequence map."""
    transform = _Transform()
    wired = WiredTransform(
        plugin=transform,  # type: ignore[arg-type]
        settings=TransformSettings(
            name="worker",
            plugin=transform.name,
            input="source_out",
            on_success="output",
            on_error="output",
            options={},
        ),
    )

    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": _Source()},  # type: ignore[arg-type]
        source_settings_map=_source_settings(on_success="source_out"),
        transforms=[wired],
        sinks={"output": _Sink()},  # type: ignore[dict-item]
    )

    name_map = graph.get_transform_name_id_map()
    seq_map = graph.get_transform_id_map()
    assert set(name_map) == {"worker"}
    assert name_map["worker"] == seq_map[0]
    assert str(name_map["worker"]).startswith("transform_worker_")


# ---------------------------------------------------------------------------
# Aggregation error edge (elspeth-d2e3f29d10)
# ---------------------------------------------------------------------------


def _aggregation_graph(on_error: str, *, extra_sinks: tuple[str, ...] = ("quarantine",)) -> ExecutionGraph:
    """source -> aggregation(batch_stats) -> output, plus the named extra sinks."""
    from elspeth.core.config import AggregationSettings, TriggerConfig
    from elspeth.plugins.transforms.batch_stats import BatchStats
    from tests.fixtures.base_classes import as_sink, as_source, as_transform
    from tests.fixtures.plugins import CollectSink, ListSource

    source = ListSource([{"value": 1}], on_success="agg_in")
    stats = BatchStats({"schema": {"mode": "observed"}, "value_field": "value"})
    settings = AggregationSettings(
        name="stats",
        plugin=stats.name,
        input="agg_in",
        on_success="output",
        on_error=on_error,
        trigger=TriggerConfig(count=2),
    )
    sinks = {name: CollectSink(name) for name in ("output", *extra_sinks)}
    return ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="agg_in", options={})},
        transforms=[],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
        aggregations={"stats": (as_transform(stats), settings)},
        gates=[],
    )


def test_aggregation_on_error_sink_gets_a_divert_edge_and_the_graph_validates() -> None:
    """A named aggregation on_error sink is wired, so it is reachable.

    Before elspeth-d2e3f29d10 no aggregation error-edge loop existed, so the
    sink named by on_error had no inbound edge and ``validate()`` refused the
    graph as "unreachable node" — every pipeline naming an aggregation error
    sink was unbuildable.
    """
    from elspeth.contracts import RoutingMode
    from elspeth.contracts.enums import error_edge_label
    from elspeth.contracts.types import AggregationName, SinkName

    graph = _aggregation_graph("quarantine")
    graph.validate()

    agg_id = graph.get_aggregation_id_map()[AggregationName("stats")]
    quarantine_id = graph.get_sink_id_map()[SinkName("quarantine")]
    diverts = [edge for edge in graph.get_edges() if edge.from_node == agg_id and edge.mode is RoutingMode.DIVERT]
    assert [(edge.to_node, edge.label) for edge in diverts] == [(quarantine_id, error_edge_label("stats"))]


def test_aggregation_on_error_discard_adds_no_divert_edge() -> None:
    from elspeth.contracts import RoutingMode
    from elspeth.contracts.types import AggregationName

    graph = _aggregation_graph("discard", extra_sinks=())
    graph.validate()

    agg_id = graph.get_aggregation_id_map()[AggregationName("stats")]
    assert [edge for edge in graph.get_edges() if edge.from_node == agg_id and edge.mode is RoutingMode.DIVERT] == []


def test_aggregation_node_id_does_not_depend_on_on_error() -> None:
    """on_error is not part of the aggregation node identity.

    The route is recorded by the DIVERT ``edges`` row. Folding on_error into
    the node config would re-hash every aggregation node_id and every
    checkpoint's topology hash for no audit gain.
    """
    from elspeth.contracts.types import AggregationName

    discard_id = _aggregation_graph("discard").get_aggregation_id_map()[AggregationName("stats")]
    routed_id = _aggregation_graph("quarantine").get_aggregation_id_map()[AggregationName("stats")]
    assert discard_id == routed_id


def test_aggregation_on_error_rejects_unknown_sink_with_suggestion() -> None:
    with pytest.raises(
        GraphValidationError, match=r"Aggregation 'stats' on_error 'quarantin' references unknown sink\. Did you mean: quarantine\?"
    ):
        _aggregation_graph("quarantin")


def test_aggregation_on_error_naming_a_closer_is_an_unknown_sink() -> None:
    """Aggregations get no rule-9 closer deferral.

    Rule 6 bans aggregations inside every bound region, so an aggregation can
    never sit inside the region a closer closes. A closer-named on_error is
    therefore rejected as an unknown sink at the error-edge loop, never
    deferred to a rule-9 resolution that could only ever refuse it.
    """
    from elspeth.core.config import AggregationSettings, CoalesceSettings, GateSettings, TriggerConfig
    from elspeth.plugins.transforms.batch_stats import BatchStats
    from tests.fixtures.base_classes import as_sink, as_source, as_transform
    from tests.fixtures.plugins import CollectSink, ListSource

    source = ListSource([{"value": 1}], on_success="agg_in")
    stats = BatchStats({"schema": {"mode": "observed"}, "value_field": "value"})
    settings = AggregationSettings(
        name="stats",
        plugin=stats.name,
        input="agg_in",
        on_success="stats_out",
        on_error="merge",
        trigger=TriggerConfig(count=2),
    )
    with pytest.raises(GraphValidationError, match=r"Aggregation 'stats' on_error 'merge' references unknown sink\."):
        ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="agg_in", options={})},
            transforms=[],
            sinks={"output": as_sink(CollectSink("output"))},
            aggregations={"stats": (as_transform(stats), settings)},
            gates=[GateSettings(name="splitter", input="stats_out", condition="'all'", routes={"all": "fork"}, fork_to=["p", "q"])],
            coalesce_settings=[
                CoalesceSettings(name="merge", branches={"p": "p", "q": "q"}, policy="require_all", merge="union", on_success="output")
            ],
        )
