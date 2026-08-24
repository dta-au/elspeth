"""Collector executor groundwork (WS3+WS4 integration phase 1, C0 / META-29).

Everything a collector-bearing graph needs BEFORE the hard refusal lifts:
the graph's transform-instance accessor, traversal classification of the
collector node, plugin-backed audit metadata for it, and the processor
factory's leader/follower wiring plus its fail-closed settings guards. None
of this makes a collector run — the refusal in ``graph_registration.py``
still stands until the lift commit — but every piece here is exercised
against a REAL built graph, not a stub.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.enums import NodeType
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.contracts.types import CollectorName, NodeID
from elspeth.core.config import ElspethSettings, load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.engine.orchestrator import PipelineConfig
from elspeth.engine.orchestrator.graph_wiring import assign_plugin_node_ids, build_dag_traversal_context
from elspeth.engine.orchestrator.landscape_registration import resolve_node_audit_metadata
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.engine.orchestrator.processor_factory import build_row_processor
from elspeth.engine.scheduler_drain import ProcessorMode
from elspeth.engine.spans import SpanFactory
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from tests.fixtures.landscape import make_recorder_with_run
from tests.fixtures.stores import MockPayloadStore

_ONE_COLLECTOR_YAML = """
sources:
  main:
    plugin: csv
    on_success: rows
    options:
      path: in.csv
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: pages
    on_error: discard
    options:
      array_field: items
      schema:
        mode: observed
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
    on_error: discard
    options:
      value_field: item
      schema:
        mode: observed
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
    on_group_failure: quarantine
sinks:
  out:
    plugin: json
    options:
      path: out.json
      schema:
        mode: observed
    on_write_failure: discard
"""

_TWO_COLLECTOR_YAML = """
sources:
  main:
    plugin: csv
    on_success: rows
    options:
      path: in.csv
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
  - name: explode_a
    plugin: json_explode
    input: rows
    on_success: pages_a
    on_error: discard
    options:
      array_field: items
      schema:
        mode: observed
  - name: explode_b
    plugin: json_explode
    input: mid
    on_success: pages_b
    on_error: discard
    options:
      array_field: items
      schema:
        mode: observed
collectors:
  - name: stitch_a
    plugin: batch_stats
    input: pages_a
    on_success: mid
    on_error: discard
    options:
      value_field: item
      schema:
        mode: observed
  - name: stitch_b
    plugin: batch_stats
    input: pages_b
    on_success: out
    on_error: discard
    options:
      value_field: item
      schema:
        mode: observed
scopes:
  - name: scope_a
    opener: explode_a
    closer: stitch_a
    policy: require_all
  - name: scope_b
    opener: explode_b
    closer: stitch_b
    policy: require_all
sinks:
  out:
    plugin: json
    options:
      path: out.json
      schema:
        mode: observed
    on_write_failure: discard
"""


def _build(settings_yaml: str) -> tuple[ElspethSettings, Any, ExecutionGraph, PipelineConfig]:
    """Settings -> plugin bundle -> real graph -> PipelineConfig, the production shape."""
    settings = load_settings_from_yaml_string(settings_yaml)
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=SinkEffectExecutionPurpose.FRESH)
    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    execution_bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    sink_effect_modes = sink_effect_modes_from_runtime_bindings(
        execution_sinks,
        execution_bindings,
        purpose=SinkEffectExecutionPurpose.FRESH,
        configured_options={name: settings.sinks[name].options for name in execution_sinks},
    )
    sink_effect_admission = validate_pipeline_sink_effect_capabilities(
        execution_sinks,
        configured_modes=sink_effect_modes,
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes),
        max_bound_region_depth=settings.max_bound_region_depth,
    )
    graph.validate()
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=sink_effect_modes,
        sink_effect_admission=sink_effect_admission,
    )
    source_id_map = {name: graph.get_sources()[0] for name in config.sources}
    assign_plugin_node_ids(
        sources=config.sources,
        transforms=config.transforms,
        sinks=config.sinks,
        source_id_map=source_id_map,
        transform_id_map=graph.get_transform_id_map(),
        sink_id_map=graph.get_sink_id_map(),
        aggregation_node_ids=frozenset(graph.get_aggregation_id_map().values()),
    )
    return settings, bundle, graph, config


def _build_processor(graph: ExecutionGraph, config: PipelineConfig, settings: ElspethSettings | None, *, mode: ProcessorMode) -> Any:
    setup = make_recorder_with_run(source_node_id=str(graph.get_sources()[0]))
    processor, _coalesce_map, _coalesce_executor = build_row_processor(
        graph=graph,
        config=config,
        settings=settings,
        factory=setup.factory,
        run_id=setup.run_id,
        source_id=graph.get_sources()[0],
        edge_map={},
        route_resolution_map=None,
        config_gate_id_map={},
        coalesce_id_map={},
        payload_store=MockPayloadStore(),
        span_factory=SpanFactory(),
        clock=None,
        max_workers=None,
        telemetry=None,
        mode=mode,
        scheduler_lease_owner="follower-1" if mode is ProcessorMode.FOLLOWER else None,
    )
    return processor


class TestGraphAccessor:
    def test_transform_map_returns_the_exact_instances_the_builder_received(self) -> None:
        _settings, bundle, graph, _config = _build(_ONE_COLLECTOR_YAML)
        transform_map = graph.get_collector_transform_map()
        assert set(transform_map) == set(graph.get_collector_id_map()) == {CollectorName("page_stitcher")}
        # Identity, not equality: plugin lifecycles key on the instance.
        assert transform_map[CollectorName("page_stitcher")] is bundle.collectors["page_stitcher"][0]

    def test_transform_map_is_a_copy(self) -> None:
        _settings, _bundle, graph, _config = _build(_ONE_COLLECTOR_YAML)
        graph.get_collector_transform_map().clear()
        assert set(graph.get_collector_transform_map()) == {CollectorName("page_stitcher")}


class TestTraversalClassification:
    def test_collector_node_is_structural_and_in_node_to_next(self) -> None:
        _settings, _bundle, graph, config = _build(_ONE_COLLECTOR_YAML)
        collector_node = graph.get_collector_id_map()[CollectorName("page_stitcher")]

        traversal = build_dag_traversal_context(graph, config, {})

        assert traversal.collector_node_map == {CollectorName("page_stitcher"): collector_node}
        assert collector_node in traversal.structural_node_ids
        assert collector_node in traversal.node_to_next
        assert collector_node not in traversal.node_to_plugin
        assert graph.get_node_info(collector_node).node_type is NodeType.COLLECTOR


class TestAuditMetadata:
    def test_collector_node_resolves_plugin_backed_metadata_from_the_accessor(self) -> None:
        _settings, bundle, graph, config = _build(_ONE_COLLECTOR_YAML)
        collector_node = graph.get_collector_id_map()[CollectorName("page_stitcher")]
        collector_plugin = bundle.collectors["page_stitcher"][0]

        metadata = resolve_node_audit_metadata(
            config,
            graph,
            source_id_map={"main": graph.get_sources()[0]},
            transform_id_map=graph.get_transform_id_map(),
            sink_id_map=graph.get_sink_id_map(),
            config_gate_node_ids=set(),
            aggregation_node_ids=set(),
            coalesce_node_ids=set(),
            collector_id_map=graph.get_collector_id_map(),
            collector_transforms=graph.get_collector_transform_map(),
        )

        assert metadata[collector_node].plugin_version == collector_plugin.plugin_version
        assert metadata[collector_node].determinism is collector_plugin.determinism
        assert metadata[collector_node].source_file_hash == collector_plugin.source_file_hash

    def test_collector_id_without_instance_fails_closed(self) -> None:
        _settings, _bundle, graph, config = _build(_ONE_COLLECTOR_YAML)
        with pytest.raises(OrchestrationInvariantError, match="no transform instance"):
            resolve_node_audit_metadata(
                config,
                graph,
                source_id_map={"main": graph.get_sources()[0]},
                transform_id_map=graph.get_transform_id_map(),
                sink_id_map=graph.get_sink_id_map(),
                config_gate_node_ids=set(),
                aggregation_node_ids=set(),
                coalesce_node_ids=set(),
                collector_id_map=graph.get_collector_id_map(),
                collector_transforms={},
            )


class TestFactoryWiring:
    def test_leader_registers_every_collector_with_its_scope(self) -> None:
        settings, _bundle, graph, config = _build(_TWO_COLLECTOR_YAML)
        processor = _build_processor(graph, config, settings, mode=ProcessorMode.LEADER)
        executor = processor.collector_executor
        assert executor is not None
        assert sorted(executor.get_registered_names()) == ["stitch_a", "stitch_b"]
        assert processor._collector_node_ids == graph.get_collector_id_map()

    def test_follower_gets_no_collector_executor_and_no_settings_raise(self) -> None:
        _settings, _bundle, graph, config = _build(_ONE_COLLECTOR_YAML)
        processor = _build_processor(graph, config, None, mode=ProcessorMode.FOLLOWER)
        assert processor.collector_executor is None
        # The cursor map still exists on a follower — arrival routing keys on it.
        assert processor._collector_node_ids == graph.get_collector_id_map()

    def test_pipeline_without_collectors_leaves_the_executor_none(self) -> None:
        plain_yaml = _ONE_COLLECTOR_YAML.split("collectors:")[0] + "sinks:" + _ONE_COLLECTOR_YAML.split("sinks:")[1]
        plain_yaml = plain_yaml.replace("on_success: pages", "on_success: out")
        settings, _bundle, graph, config = _build(plain_yaml)
        processor = _build_processor(graph, config, settings, mode=ProcessorMode.LEADER)
        assert processor.collector_executor is None
        assert processor._collector_node_ids == {}

    def test_missing_collector_settings_fails_closed(self) -> None:
        settings, _bundle, graph, config = _build(_ONE_COLLECTOR_YAML)
        stripped = settings.model_copy(update={"collectors": [], "scopes": []})
        with pytest.raises(OrchestrationInvariantError, match=r"settings\.collectors is missing"):
            _build_processor(graph, config, stripped, mode=ProcessorMode.LEADER)

    def test_settings_naming_a_subset_of_the_graphs_collectors_fails_closed(self) -> None:
        settings, _bundle, graph, config = _build(_TWO_COLLECTOR_YAML)
        subset = settings.model_copy(update={"collectors": [settings.collectors[0]]})
        with pytest.raises(OrchestrationInvariantError, match="every graph collector needs exactly one settings entry"):
            _build_processor(graph, config, subset, mode=ProcessorMode.LEADER)

    def test_settings_naming_a_collector_the_graph_lacks_fails_closed(self) -> None:
        settings, _bundle, graph, config = _build(_ONE_COLLECTOR_YAML)
        foreign = settings.collectors[0].model_copy(update={"name": "ghost"})
        scope = settings.scopes[0].model_copy(update={"closer": "ghost"})
        renamed = settings.model_copy(update={"collectors": [foreign], "scopes": [scope]})
        with pytest.raises(OrchestrationInvariantError, match="no such collector node"):
            _build_processor(graph, config, renamed, mode=ProcessorMode.LEADER)

    def test_collector_without_a_scope_entry_fails_closed(self) -> None:
        settings, _bundle, graph, config = _build(_ONE_COLLECTOR_YAML)
        scopeless = settings.model_copy(update={"scopes": []})
        with pytest.raises(OrchestrationInvariantError, match="no scopes: entry"):
            _build_processor(graph, config, scopeless, mode=ProcessorMode.LEADER)


def test_collector_node_id_type_is_the_graphs() -> None:
    _settings, _bundle, graph, _config = _build(_ONE_COLLECTOR_YAML)
    node_id = graph.get_collector_id_map()[CollectorName("page_stitcher")]
    assert isinstance(node_id, str)
    assert NodeID(node_id) == node_id
