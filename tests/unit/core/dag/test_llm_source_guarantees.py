"""Graph-level guarantee propagation for the single-prompt LLM source.

Regression tests for elspeth-db98d3f660: the DAG builder must consume the
source's plugin-computed output SchemaConfig — which guarantees the
``<response_field>`` / ``_usage`` / ``_model`` trio — rather than re-parsing
the raw options dict (which stays ``mode: observed`` with no guarantees).
The composer's raw-level guarantee synthesis
(``get_raw_producer_guaranteed_fields``) must agree with the engine.
"""

from __future__ import annotations

from typing import Any, ClassVar

from elspeth.contracts.schema import SchemaConfig, get_raw_producer_guaranteed_fields
from elspeth.core.config import SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.guarantees import get_effective_guaranteed_fields
from elspeth.core.dag.wiring import WiredTransform
from elspeth.plugins.sources.llm.source import LLMSource

_BRIEFING_TRIO = frozenset({"briefing", "briefing_usage", "briefing_model"})


def _llm_source_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "api_key": "test-api-key",
        "prompt_template": "Summarise the audit topic.",
        "response_field": "briefing",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    options.update(overrides)
    return options


class _UsageConsumerTransform:
    """Stub consumer requiring one of the LLM source's guaranteed metadata fields."""

    name = "usage_consumer"
    input_schema = None
    output_schema = None
    creates_tokens = False
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    preserves_input_values = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        self.config: dict[str, Any] = {
            "schema": {"mode": "observed"},
            "required_input_fields": ["briefing_usage"],
        }
        self._output_schema_config: SchemaConfig | None = None


class _CollectorSink:
    name = "collector"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {"schema": {"mode": "observed"}}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


def _build_graph(source: LLMSource) -> ExecutionGraph:
    return ExecutionGraph.from_plugin_instances(
        sources={"llm_brief": source},
        source_settings_map={
            "llm_brief": SourceSettings(plugin="llm", on_success="briefed", options=dict(source.config)),
        },
        transforms=[
            WiredTransform(
                plugin=_UsageConsumerTransform(),  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="usage_consumer",
                    plugin="usage_consumer",
                    input="briefed",
                    on_success="output",
                    on_error="discard",
                    options={},
                ),
            )
        ],
        sinks={"output": _CollectorSink()},  # type: ignore[dict-item]
        aggregations={},
        gates=[],
        coalesce_settings=[],
    )


class TestLLMSourceGuaranteePropagation:
    def test_downstream_requirement_on_guaranteed_trio_field_builds(self) -> None:
        """Decisive repro: transform requiring briefing_usage must build.

        Before the fix this raised GraphValidationError with
        "Producer (llm) guarantees: (none - dynamic schema)" because the
        builder re-parsed the raw ``mode: observed`` options dict instead of
        the augmented plugin-computed contract.
        """
        source = LLMSource(_llm_source_options())
        graph = _build_graph(source)

        source_node = graph.get_sources()[0]
        assert get_effective_guaranteed_fields(graph, source_node) >= _BRIEFING_TRIO

    def test_builder_prefers_plugin_computed_schema_config(self) -> None:
        source = LLMSource(_llm_source_options())
        assert source._output_schema_config is not None

        graph = _build_graph(source)
        node_info = graph.get_node_info(graph.get_sources()[0])
        assert node_info.output_schema_config == source._output_schema_config
        assert node_info.output_schema_config.get_effective_guaranteed_fields() == _BRIEFING_TRIO

    def test_engine_and_composer_guarantee_arms_agree(self) -> None:
        """The composer's raw-level synthesis must match the engine contract."""
        options = _llm_source_options()
        source = LLMSource(options)
        graph = _build_graph(source)

        composer_raw = get_raw_producer_guaranteed_fields("llm", options, owner="source:llm_brief")
        assert composer_raw == source.declared_guaranteed_fields
        assert composer_raw == get_effective_guaranteed_fields(graph, graph.get_sources()[0])
