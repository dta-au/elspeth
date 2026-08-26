"""Build-time type validation across OBSERVED producers (elspeth-e6e552ce34).

Live repro this closes: csv source (mode: observed, guaranteed_fields) → gate
fork → two observed pass-through llm branches → union coalesce (observed) →
field_mapper declaring ``id: int``. Authoring validation and preview passed;
every merged row then died typed at the consumer's input preflight because the
CSV emits ``id`` as ``str`` by construction. Ruling (John, 2026-08-26): this
must fail at BUILD/PREVIEW so the planner self-repairs through ordinary
validation feedback.

Three coordinated pieces under test here:

1. Two new plugin contract facts threaded onto ``NodeInfo`` by the builder:
   ``observed_value_type`` (SOURCE-only — the structural type of every cell an
   observed-mode source emits; csv declares ``"str"``) and
   ``preserves_input_values`` (TRANSFORM-only — process() never changes the
   VALUE of a field present on the input row; adding fields is fine).
2. Two new arms in ``resolve_guaranteed_field_type``: recursion through an
   undeclaring pass-through that promises value preservation, and a structural
   answer at an observed source for fields in its own guaranteed set.
3. ``validate_observed_producer_declared_types`` — the final phase-2 pass that
   applies ``resolved_guarantee_type_mismatch`` to a typed consumer's required
   fields when the effective producer schema is observed/dynamic.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.guarantees import resolve_guaranteed_field_type
from elspeth.core.dag.models import GraphValidationError

_OBSERVED_GUARANTEES_ID = SchemaConfig(mode="observed", guaranteed_fields=("id",))
_OBSERVED_BARE = SchemaConfig(mode="observed")


def _observed_source_graph(*, observed_value_type: str | None = "str") -> ExecutionGraph:
    """csv-shaped observed source guaranteeing ``id``, nothing else."""
    graph = ExecutionGraph()
    graph.add_node(
        "src",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        output_schema_config=_OBSERVED_GUARANTEES_ID,
        observed_value_type=observed_value_type,
    )
    return graph


def _append_passthrough(
    graph: ExecutionGraph,
    node_id: str,
    upstream: str,
    *,
    preserves_input_values: bool,
    config: SchemaConfig | None = _OBSERVED_BARE,
) -> None:
    graph.add_node(
        node_id,
        node_type=NodeType.TRANSFORM,
        plugin_name="passthrough",
        output_schema_config=config,
        passes_through_input=True,
        preserves_input_values=preserves_input_values,
    )
    graph.add_edge(upstream, node_id, label="continue")


class TestNodeInfoContractFacts:
    """NodeInfo carries the two new plugin facts, guarded by node type."""

    def test_defaults_are_absent(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("t", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        assert graph.get_node_info("src").observed_value_type is None
        assert graph.get_node_info("src").preserves_input_values is False
        assert graph.get_node_info("t").preserves_input_values is False
        assert graph.get_node_info("t").observed_value_type is None

    def test_add_node_threads_both_facts(self) -> None:
        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            observed_value_type="str",
        )
        graph.add_node(
            "t",
            node_type=NodeType.TRANSFORM,
            plugin_name="passthrough",
            preserves_input_values=True,
        )
        assert graph.get_node_info("src").observed_value_type == "str"
        assert graph.get_node_info("t").preserves_input_values is True

    def test_observed_value_type_is_source_only(self) -> None:
        """Offensive-programming guard, mirroring declared_required_fields."""
        graph = ExecutionGraph()
        with pytest.raises(GraphValidationError, match="observed_value_type"):
            graph.add_node(
                "t",
                node_type=NodeType.TRANSFORM,
                plugin_name="passthrough",
                observed_value_type="str",
            )

    def test_preserves_input_values_is_transform_only(self) -> None:
        """A source/sink carrying the transform fact is a wiring bug."""
        graph = ExecutionGraph()
        with pytest.raises(GraphValidationError, match="preserves_input_values"):
            graph.add_node(
                "src",
                node_type=NodeType.SOURCE,
                plugin_name="csv",
                preserves_input_values=True,
            )


class TestStructuralSourceResolution:
    """The observed source answers its structural cell type — narrowly."""

    def test_guaranteed_field_resolves_to_structural_type(self) -> None:
        graph = _observed_source_graph()
        resolved = resolve_guaranteed_field_type(graph, "src", "id")
        assert resolved is not None
        assert resolved.field_type == "str"
        assert resolved.declared_by == frozenset({"src"})

    def test_unguaranteed_field_abstains(self) -> None:
        """A field outside the source's own guaranteed set gets NO structural
        answer — this is the self-limiting edge that keeps over-recursion
        sound: a mid-path-introduced field (an llm response) can never be
        attributed to the source."""
        graph = _observed_source_graph()
        assert resolve_guaranteed_field_type(graph, "src", "answer_a") is None

    def test_source_without_structural_type_abstains(self) -> None:
        graph = _observed_source_graph(observed_value_type=None)
        assert resolve_guaranteed_field_type(graph, "src", "id") is None

    def test_declared_fields_mode_still_wins(self) -> None:
        """A fixed/flexible source resolves through its declaration, not the
        structural arm — the declaration is what the source coerces into."""
        from elspeth.contracts.schema import FieldDefinition

        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            output_schema_config=SchemaConfig(
                mode="flexible",
                fields=(FieldDefinition("id", "int"),),
            ),
            observed_value_type="str",
        )
        resolved = resolve_guaranteed_field_type(graph, "src", "id")
        assert resolved is not None
        assert resolved.field_type == "int"


class TestValuePreservingPassThroughResolution:
    """Recursion through undeclaring pass-throughs is gated on the promise."""

    def test_preserving_passthrough_recurses_to_source(self) -> None:
        graph = _observed_source_graph()
        _append_passthrough(graph, "keep", "src", preserves_input_values=True)
        resolved = resolve_guaranteed_field_type(graph, "keep", "id")
        assert resolved is not None
        assert resolved.field_type == "str"
        assert resolved.declared_by == frozenset({"src"})

    def test_non_preserving_passthrough_abstains(self) -> None:
        """Existing posture pinned: an undeclaring pass-through that has NOT
        promised value preservation (type_coerce/value_transform shape)
        still abstains."""
        graph = _observed_source_graph()
        _append_passthrough(graph, "rewrite", "src", preserves_input_values=False)
        assert resolve_guaranteed_field_type(graph, "rewrite", "id") is None

    def test_preserving_passthrough_with_declaration_settles_first(self) -> None:
        """A preserving pass-through that DID declare the field keeps the
        nearest-declaration-wins rule — the promise only unlocks recursion
        where there is nothing declared to consult."""
        from elspeth.contracts.schema import FieldDefinition

        graph = _observed_source_graph()
        _append_passthrough(
            graph,
            "declares",
            "src",
            preserves_input_values=True,
            config=SchemaConfig(mode="flexible", fields=(FieldDefinition("id", "float"),)),
        )
        resolved = resolve_guaranteed_field_type(graph, "declares", "id")
        assert resolved is not None
        assert resolved.field_type == "float"
        assert resolved.declared_by == frozenset({"declares"})

    def test_coalesce_unanimity_still_required(self) -> None:
        """Two branches resolving to different structural types abstain —
        unanimity mutation-kill for the new arms."""
        graph = ExecutionGraph()
        graph.add_node(
            "src_str",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            output_schema_config=_OBSERVED_GUARANTEES_ID,
            observed_value_type="str",
        )
        graph.add_node(
            "src_int",
            node_type=NodeType.SOURCE,
            plugin_name="intsource",
            output_schema_config=_OBSERVED_GUARANTEES_ID,
            observed_value_type="int",
        )
        graph.add_node(
            "merge",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce",
            output_schema_config=_OBSERVED_BARE,
        )
        graph.add_edge("src_str", "merge", label="a")
        graph.add_edge("src_int", "merge", label="b")
        assert resolve_guaranteed_field_type(graph, "merge", "id") is None

    def test_coalesce_agreement_resolves(self) -> None:
        graph = ExecutionGraph()
        for src in ("src_a", "src_b"):
            graph.add_node(
                src,
                node_type=NodeType.SOURCE,
                plugin_name="csv",
                output_schema_config=_OBSERVED_GUARANTEES_ID,
                observed_value_type="str",
            )
        graph.add_node(
            "merge",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce",
            output_schema_config=_OBSERVED_BARE,
        )
        graph.add_edge("src_a", "merge", label="a")
        graph.add_edge("src_b", "merge", label="b")
        resolved = resolve_guaranteed_field_type(graph, "merge", "id")
        assert resolved is not None
        assert resolved.field_type == "str"
        assert resolved.declared_by == frozenset({"src_a", "src_b"})

    def test_divert_in_edge_still_abstains(self) -> None:
        """The DIVERT abstention survives the new arms (mutation-kill)."""
        graph = _observed_source_graph()
        graph.add_node(
            "errsrc",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            output_schema_config=_OBSERVED_GUARANTEES_ID,
            observed_value_type="str",
        )
        _append_passthrough(graph, "keep", "src", preserves_input_values=True)
        graph.add_edge("errsrc", "keep", label="divert", mode=RoutingMode.DIVERT)
        assert resolve_guaranteed_field_type(graph, "keep", "id") is None


class TestPluginDeclarations:
    """The real plugins carry the facts, and the builder threads them."""

    _PIPELINE = """sources:
  primary:
    plugin: csv
    on_success: rows
    options:
      path: examples/fork_coalesce/input.csv
      schema:
        mode: observed
        guaranteed_fields: [id, description]
      on_validation_failure: discard

transforms:
- name: keep
  plugin: passthrough
  input: rows
  on_success: output
  on_error: discard
  options:
    schema:
      mode: observed

sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: out.jsonl
      format: jsonl
      schema:
        mode: observed
"""

    def _graph(self) -> ExecutionGraph:
        from elspeth.cli_helpers import instantiate_plugins_from_config
        from elspeth.core.config import load_settings_from_yaml_string

        settings = load_settings_from_yaml_string(self._PIPELINE)
        plugins = instantiate_plugins_from_config(settings)
        return ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=settings.gates,
            coalesce_settings=settings.coalesce,
        )

    def test_csv_source_declares_str_observed_cells(self) -> None:
        graph = self._graph()
        source_ids = [nid for nid, data in graph._graph.nodes(data=True) if data["info"].node_type is NodeType.SOURCE]
        assert len(source_ids) == 1
        assert graph.get_node_info(source_ids[0]).observed_value_type == "str"

    def test_passthrough_declares_value_preservation(self) -> None:
        graph = self._graph()
        transform_ids = [nid for nid, data in graph._graph.nodes(data=True) if data["info"].node_type is NodeType.TRANSFORM]
        assert len(transform_ids) == 1
        assert graph.get_node_info(transform_ids[0]).preserves_input_values is True

    def test_llm_transform_declares_value_preservation(self) -> None:
        from elspeth.plugins.transforms.llm.transform import LLMTransform

        assert LLMTransform.preserves_input_values is True

    def test_rewriting_declarers_stay_false(self) -> None:
        """type_coerce, value_transform, and truncate rewrite forwarded
        values in place — the promise must never appear on them."""
        from elspeth.plugins.transforms.truncate import Truncate
        from elspeth.plugins.transforms.type_coerce import TypeCoerce
        from elspeth.plugins.transforms.value_transform import ValueTransform

        assert TypeCoerce.preserves_input_values is False
        assert ValueTransform.preserves_input_values is False
        assert Truncate.preserves_input_values is False


# ---------------------------------------------------------------------------
# The validation pass (validate_observed_producer_declared_types): repro shape
# and its green controls, built through the real builder.
# ---------------------------------------------------------------------------

_REPRO_PIPELINE = """sources:
  primary:
    plugin: csv
    on_success: raw
    options:
      path: examples/fork_coalesce/input.csv
      schema:
        mode: observed
        guaranteed_fields: [id, description]
      on_validation_failure: discard

gates:
- name: fork_gate
  input: raw
  condition: "True"
  routes:
    'true': fork
    'false': discard
  fork_to:
    - path_a
    - path_b

transforms:
- name: branch_a
  plugin: {branch_plugin}
  input: path_a
  on_success: done_a
  on_error: discard
  options:
{branch_a_options}
- name: branch_b
  plugin: passthrough
  input: path_b
  on_success: done_b
  on_error: discard
  options:
    schema:
      mode: observed
- name: tidy_columns
  plugin: truncate
  input: merge_results
  on_success: output
  on_error: discard
  options:
    fields:
      description: 10
    suffix: "..."
    schema:
      mode: flexible
      fields:
      - 'id: {consumer_id_type}'
      - 'description: str'

coalesce:
- name: merge_results
  branches:
    path_a: done_a
    path_b: done_b
  policy: require_all
  merge: union
  union_collision_policy: last_wins

sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: out.jsonl
      format: jsonl
      schema:
        mode: observed
"""

_PASSTHROUGH_OBSERVED_OPTIONS = """    schema:
      mode: observed"""

_TRUNCATE_BRANCH_OPTIONS = """    fields:
      description: 60
    suffix: "..."
    schema:
      mode: observed"""


def _build_repro_graph(
    *,
    consumer_id_type: str = "int",
    branch_plugin: str = "passthrough",
    branch_a_options: str = _PASSTHROUGH_OBSERVED_OPTIONS,
    source_schema_override: str | None = None,
) -> ExecutionGraph:
    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.core.config import load_settings_from_yaml_string

    yaml_text = _REPRO_PIPELINE.format(
        consumer_id_type=consumer_id_type,
        branch_plugin=branch_plugin,
        branch_a_options=branch_a_options,
    )
    if source_schema_override is not None:
        yaml_text = yaml_text.replace(
            "      schema:\n        mode: observed\n        guaranteed_fields: [id, description]\n",
            source_schema_override,
        )
    settings = load_settings_from_yaml_string(yaml_text)
    plugins = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=settings.gates,
        coalesce_settings=settings.coalesce,
    )


class TestObservedProducerDeclaredTypesPass:
    """The live-repro family fails at BUILD; its neighbours stay green."""

    def test_repro_shape_is_rejected_at_build(self) -> None:
        """elspeth-e6e552ce34's exact family: observed csv → fork → observed
        value-preserving branches → observed union coalesce → consumer
        declaring id: int. Previously built green and quarantined 5/5 rows
        at runtime."""
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_repro_graph()

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.type_mismatches == (("id", "int", "str"),)
        message = str(exc_info.value)
        assert "Observed-schema type violation" in message
        assert exc_info.value.to_node_id is not None and exc_info.value.to_node_id.startswith("transform_tidy_columns")
        # The declaring source is named so the planner can see WHERE str came from.
        assert "source_primary" in message

    def test_matching_consumer_type_builds(self) -> None:
        _build_repro_graph(consumer_id_type="str")

    def test_rewriting_branch_abstains_and_builds(self) -> None:
        """One branch is a truncate (preserves_input_values=False): the walk
        abstains, so the pass stays silent and the per-row preflight keeps
        the verdict — the historical posture for unprovable types."""
        _build_repro_graph(branch_plugin="truncate", branch_a_options=_TRUNCATE_BRANCH_OPTIONS)

    def test_declared_source_coercion_builds(self) -> None:
        """mode: fixed source declaring id: int coerces at ingest — the
        declared arm resolves int and the consumer's int agrees."""
        _build_repro_graph(
            source_schema_override=(
                "      schema:\n        mode: fixed\n        fields:\n        - 'id: int'\n        - 'product: str'\n        - 'price: int'\n        - 'category: str'\n        - 'description: str'\n"
            )
        )

    def test_direct_observed_edge_is_rejected(self) -> None:
        """The defect family without the coalesce: source (observed, str) →
        consumer declaring id: int on a direct edge."""
        from elspeth.core.dag.models import EdgeContractError

        pipeline = """sources:
  primary:
    plugin: csv
    on_success: raw
    options:
      path: examples/fork_coalesce/input.csv
      schema:
        mode: observed
        guaranteed_fields: [id, description]
      on_validation_failure: discard

transforms:
- name: tidy_columns
  plugin: truncate
  input: raw
  on_success: output
  on_error: discard
  options:
    fields:
      description: 10
    suffix: "..."
    schema:
      mode: flexible
      fields:
      - 'id: int'
      - 'description: str'

sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: out.jsonl
      format: jsonl
      schema:
        mode: observed
"""
        from elspeth.cli_helpers import instantiate_plugins_from_config
        from elspeth.core.config import load_settings_from_yaml_string

        settings = load_settings_from_yaml_string(pipeline)
        plugins = instantiate_plugins_from_config(settings)
        with pytest.raises(EdgeContractError) as exc_info:
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=settings.gates,
                coalesce_settings=settings.coalesce,
            )
        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.type_mismatches == (("id", "int", "str"),)

    def test_missing_field_error_keeps_precedence(self) -> None:
        """A graph tripping BOTH the phase-1 missing-fields contract and this
        pass keeps reporting the pre-existing missing-fields error."""
        from elspeth.core.dag.models import EdgeContractError

        yaml_extra_required = _REPRO_PIPELINE.format(
            consumer_id_type="int",
            branch_plugin="passthrough",
            branch_a_options=_PASSTHROUGH_OBSERVED_OPTIONS,
        ).replace(
            "    fields:\n      description: 10\n    suffix: \"...\"\n",
            "    fields:\n      description: 10\n    suffix: \"...\"\n    required_input_fields:\n    - not_a_column\n",
        )
        from elspeth.cli_helpers import instantiate_plugins_from_config
        from elspeth.core.config import load_settings_from_yaml_string

        settings = load_settings_from_yaml_string(yaml_extra_required)
        plugins = instantiate_plugins_from_config(settings)
        with pytest.raises(EdgeContractError) as exc_info:
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=settings.gates,
                coalesce_settings=settings.coalesce,
            )
        assert "Missing fields" in str(exc_info.value)
        assert "Observed-schema type violation" not in str(exc_info.value)
