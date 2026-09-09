"""Build-time enforcement of declared_string_input_fields (elspeth-b19dfe41fb).

Six text-scanning transforms fail CLOSED at runtime when an explicitly
configured scan field holds a non-string value: the Bedrock guardrail pair
(``fields``), the Azure safety pair (``fields`` when a named list),
``keyword_filter`` (``fields`` when a named list), and Azure
``document_intelligence`` (``source_field``). A producer whose schema declares
such a field as int/float/bool therefore kills 100% of rows — knowably, from
static configuration alone — yet both static surfaces were blind: Phase 1 edge
validation compares field NAMES only, and Phase 2 type validation is bypassed
whenever the consumer's ``schema:`` block is observed-mode, which is exactly
what the composer's required-control auto-wire hard-codes (round-5 arm B g11).

These tests pin the projection (plugin -> builder -> NodeInfo), the enforcement
(``validate_transform_string_typed_input_fields``), and the ABSTENTION rules
that keep the check from rejecting runnable pipelines: ``str`` and ``any``
declared types pass, observed/unknown producers prove nothing, and the Azure /
keyword_filter ``fields: all`` mode declares nothing because it skips
non-string values instead of failing the row.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.core.dag import schema_validation
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError, NodeInfo

# ---------------------------------------------------------------------------
# Hand-built graphs — the validator in isolation
# ---------------------------------------------------------------------------


def _shield_graph(
    *,
    source_schema: dict[str, object],
    declared_string_input_fields: frozenset[str],
    edge_mode: RoutingMode = RoutingMode.MOVE,
) -> ExecutionGraph:
    """source -> transform -> sink, with the transform's string-typed declaration under test."""
    graph = ExecutionGraph()
    graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": source_schema})
    graph.add_node(
        "shield",
        node_type=NodeType.TRANSFORM,
        plugin_name="aws_bedrock_prompt_shield",
        config={"schema": {"mode": "observed"}},
        declared_string_input_fields=declared_string_input_fields,
    )
    graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
    graph.add_edge("src", "shield", label="continue", mode=edge_mode)
    graph.add_edge("shield", "sink", label="out", mode=RoutingMode.MOVE)
    return graph


class TestTransformStringTypedInputFields:
    """A declared scan field with a provably non-string producer type must not build."""

    def test_declared_int_field_is_rejected(self) -> None:
        """The g11 shape: the shield scans the source's only column, declared int."""
        graph = _shield_graph(
            source_schema={"mode": "fixed", "fields": ["sentence_num: int"]},
            declared_string_input_fields=frozenset({"sentence_num"}),
        )

        with pytest.raises(GraphValidationError, match="sentence_num") as exc_info:
            schema_validation.validate_transform_string_typed_input_fields(graph)

        message = str(exc_info.value)
        assert "aws_bedrock_prompt_shield" in message
        assert "int" in message
        assert "src" in message

    @pytest.mark.parametrize("field_type", ["float", "bool"])
    def test_other_non_string_types_are_rejected(self, field_type: str) -> None:
        """int is not special: every provably non-string scalar type is fatal."""
        graph = _shield_graph(
            source_schema={"mode": "fixed", "fields": [f"score: {field_type}"]},
            declared_string_input_fields=frozenset({"score"}),
        )

        with pytest.raises(GraphValidationError, match="score"):
            schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_declared_str_field_builds(self) -> None:
        """Negative control: a string-typed producer field satisfies the contract."""
        graph = _shield_graph(
            source_schema={"mode": "fixed", "fields": ["prompt: str"]},
            declared_string_input_fields=frozenset({"prompt"}),
        )

        schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_any_typed_field_abstains(self) -> None:
        """ABSTENTION: `any` cannot be proven non-string, so enforcement stays per-row."""
        graph = _shield_graph(
            source_schema={"mode": "fixed", "fields": ["payload: any"]},
            declared_string_input_fields=frozenset({"payload"}),
        )

        schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_observed_producer_abstains(self) -> None:
        """ABSTENTION: an observed upstream proves nothing about value types."""
        graph = _shield_graph(
            source_schema={"mode": "observed"},
            declared_string_input_fields=frozenset({"sentence_num"}),
        )

        schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_undeclared_field_abstains(self) -> None:
        """A flexible producer that does not declare the scan field proves nothing about it."""
        graph = _shield_graph(
            source_schema={"mode": "flexible", "fields": ["other: int"]},
            declared_string_input_fields=frozenset({"prompt"}),
        )

        schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_optional_int_field_is_still_rejected(self) -> None:
        """Optional does not soften the fatality: present -> non-string kill,
        absent -> the same family fail-closes on the missing field."""
        graph = _shield_graph(
            source_schema={"mode": "fixed", "fields": ["sentence_num: int?"]},
            declared_string_input_fields=frozenset({"sentence_num"}),
        )

        with pytest.raises(GraphValidationError, match="sentence_num"):
            schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_flexible_declared_int_field_is_rejected(self) -> None:
        """Flexible mode still enforces DECLARED field types, so the proof holds."""
        graph = _shield_graph(
            source_schema={"mode": "flexible", "fields": ["sentence_num: int"]},
            declared_string_input_fields=frozenset({"sentence_num"}),
        )

        with pytest.raises(GraphValidationError, match="sentence_num"):
            schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_divert_only_predecessor_is_not_checked(self) -> None:
        """A DIVERT payload is an error envelope, not the producer's declared row."""
        graph = _shield_graph(
            source_schema={"mode": "fixed", "fields": ["sentence_num: int"]},
            declared_string_input_fields=frozenset({"sentence_num"}),
            edge_mode=RoutingMode.DIVERT,
        )

        schema_validation.validate_transform_string_typed_input_fields(graph)

    def test_reached_through_validate_edge_compatibility(self) -> None:
        """The check is wired into the surface /validate and `elspeth run` both reach."""
        graph = _shield_graph(
            source_schema={"mode": "fixed", "fields": ["sentence_num: int"]},
            declared_string_input_fields=frozenset({"sentence_num"}),
        )

        with pytest.raises(GraphValidationError, match="sentence_num"):
            graph.validate_edge_compatibility()


class TestStringTypedInputFieldsNodeInfoGuard:
    """declared_string_input_fields is TRANSFORM-only, mirroring its siblings."""

    def test_non_transform_node_is_rejected(self) -> None:
        """Offensive programming: stray declarations must not sit unread on a sink."""
        with pytest.raises(GraphValidationError, match="only meaningful for TRANSFORM nodes"):
            NodeInfo(
                node_id="n1",
                node_type=NodeType.SINK,
                plugin_name="json",
                declared_string_input_fields=frozenset({"prompt"}),
            )

    def test_transform_node_is_accepted(self) -> None:
        """Positive control for the guard above."""
        info = NodeInfo(
            node_id="n1",
            node_type=NodeType.TRANSFORM,
            plugin_name="aws_bedrock_prompt_shield",
            declared_string_input_fields=frozenset({"prompt"}),
        )

        assert info.declared_string_input_fields == frozenset({"prompt"})


# ---------------------------------------------------------------------------
# Plugin declaration surface — each family member computes the right set
# ---------------------------------------------------------------------------


class TestFamilyDeclarations:
    """Each fail-closed text scanner declares exactly its named scan fields."""

    def test_bedrock_prompt_shield_declares_fields(self) -> None:
        from elspeth.plugins.transforms.aws.bedrock_prompt_shield import AWSBedrockPromptShield

        transform = AWSBedrockPromptShield(
            {
                "guardrail_identifier": "privateguardrail",
                "guardrail_version": "7",
                "region": "us-east-1",
                "fields": ["prompt", "context"],
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_string_input_fields == frozenset({"prompt", "context"})

    def test_bedrock_content_safety_declares_fields(self) -> None:
        from elspeth.plugins.transforms.aws.bedrock_content_safety import AWSBedrockContentSafety

        transform = AWSBedrockContentSafety(
            {
                "guardrail_identifier": "privateguardrail",
                "guardrail_version": "7",
                "region": "us-east-1",
                "fields": ["response"],
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_string_input_fields == frozenset({"response"})

    def test_keyword_filter_named_fields_declare(self) -> None:
        from elspeth.plugins.transforms.keyword_filter import KeywordFilter

        transform = KeywordFilter(
            {
                "fields": ["message", "subject"],
                "blocked_patterns": ["forbidden"],
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_string_input_fields == frozenset({"message", "subject"})

    def test_keyword_filter_all_mode_declares_nothing(self) -> None:
        """'all' mode skips non-string values instead of failing the row."""
        from elspeth.plugins.transforms.keyword_filter import KeywordFilter

        transform = KeywordFilter(
            {
                "fields": "all",
                "blocked_patterns": ["forbidden"],
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_string_input_fields == frozenset()

    def test_azure_prompt_shield_named_fields_declare(self) -> None:
        from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShield

        transform = AzurePromptShield(
            {
                "endpoint": "https://test.cognitiveservices.azure.com",
                "api_key": "test-key",
                "fields": ["prompt"],
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_string_input_fields == frozenset({"prompt"})

    def test_azure_content_safety_all_mode_declares_nothing(self) -> None:
        from elspeth.plugins.transforms.azure.content_safety import AzureContentSafety

        transform = AzureContentSafety(
            {
                "endpoint": "https://test.cognitiveservices.azure.com",
                "api_key": "test-key",
                "fields": "all",
                "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2},
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_string_input_fields == frozenset()

    def test_document_intelligence_declares_source_field(self) -> None:
        from elspeth.plugins.transforms.azure.document_intelligence import AzureDocumentIntelligence

        transform = AzureDocumentIntelligence(
            {
                "endpoint": "https://test.cognitiveservices.azure.com",
                "api_key": "k",
                "model_id": "prebuilt-layout",
                "source_mode": "url",
                "source_field": "doc_url",
                "content_field": "di_content",
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_string_input_fields == frozenset({"doc_url"})


# ---------------------------------------------------------------------------
# Production build path — projection wiring, end to end (the g11 repro)
# ---------------------------------------------------------------------------


def _build(settings: Any) -> ExecutionGraph:
    """Drive the real instantiate -> build path (build_execution_graph validates internally)."""
    from elspeth.cli_helpers import instantiate_plugins_from_config

    plugins = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
    )


def _shield_settings(*, field_spec: str) -> Any:
    """g11's shape: fixed-mode CSV source -> bedrock prompt shield scanning its only column."""
    from elspeth.core.config import (
        ElspethSettings,
        SinkSettings,
        SourceSettings,
        TransformSettings,
    )

    return ElspethSettings(
        sources={
            "primary": SourceSettings(
                plugin="csv",
                on_success="seed_rows",
                options={
                    "path": "seed.csv",
                    "on_validation_failure": "discard",
                    "schema": {"mode": "fixed", "fields": [field_spec]},
                },
            )
        },
        transforms=[
            TransformSettings(
                name="shield",
                plugin="aws_bedrock_prompt_shield",
                input="seed_rows",
                on_success="output",
                on_error="discard",
                options={
                    "guardrail_identifier": "privateguardrail",
                    "guardrail_version": "7",
                    "region": "us-east-1",
                    "fields": ["sentence_num"],
                    "schema": {"mode": "observed"},
                },
            )
        ],
        sinks={
            "output": SinkSettings(
                plugin="json",
                on_write_failure="discard",
                options={"path": "out.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
            )
        },
    )


class TestBedrockShieldBuildPath:
    """The auto-wired g11 configuration must not build green."""

    def test_shield_on_declared_int_field_is_rejected(self, plugin_manager: Any) -> None:
        """At HEAD this built green, passed /validate, and killed 100% of rows at runtime."""
        settings = _shield_settings(field_spec="sentence_num: int")

        with pytest.raises(GraphValidationError, match="sentence_num"):
            _build(settings)

    def test_shield_on_declared_str_field_builds(self, plugin_manager: Any) -> None:
        """Baseline: the intended wiring (a text column) must keep building."""
        settings = _shield_settings(field_spec="sentence_num: str")

        _build(settings)

    def test_projection_reaches_node_info(self, plugin_manager: Any) -> None:
        """The builder must carry the plugin's declaration verbatim onto the node."""
        settings = _shield_settings(field_spec="sentence_num: str")

        graph = _build(settings)

        transform_nodes = [data["info"] for _nid, data in graph._graph.nodes(data=True) if data["info"].node_type == NodeType.TRANSFORM]
        assert len(transform_nodes) == 1
        assert transform_nodes[0].declared_string_input_fields == frozenset({"sentence_num"})
