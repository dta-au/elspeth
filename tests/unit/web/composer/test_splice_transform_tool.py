"""Atomic direct-path transform insertion contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

import pytest
from pydantic import TypeAdapter

from elspeth.contracts import NodeType
from elspeth.core.config import RuntimeNodeName, load_settings_from_yaml_string
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import MANIFEST, SpliceTransformArgumentsModel, redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, EdgeSpec, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools._dispatch import execute_tool, get_tool_definitions
from elspeth.web.composer.tools.transforms import _execute_splice_transform
from elspeth.web.composer.yaml_generator import generate_public_yaml
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


def _node(
    node_id: str,
    *,
    input_name: str,
    output_name: str,
    node_type: NodeType = "transform",
    on_error: str | None = "discard",
    plugin: str | None = "passthrough",
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=input_name,
        on_success=output_name,
        on_error=on_error,
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "rows.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            _node("before", input_name="rows", output_name="middle"),
            _node("after", input_name="middle", output_name="result"),
        ),
        edges=(
            EdgeSpec(id="source-before", from_node="source", to_node="before", edge_type="on_success", label=None),
            EdgeSpec(id="before-after", from_node="before", to_node="after", edge_type="on_success", label=None),
            EdgeSpec(id="after-output", from_node="after", to_node="result", edge_type="on_success", label=None),
        ),
        outputs=(
            OutputSpec(
                name="result",
                plugin="json",
                options={
                    "path": "out.jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=4,
    )


def _context() -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
    )


def _arguments(*, options: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "predecessor_id": "before",
        "successor_id": "after",
        "node": {
            "id": "inserted",
            "plugin": "passthrough",
            "options": options or {"schema": {"mode": "observed"}},
            "on_error": "discard",
        },
    }


def test_splice_transform_is_declared_through_the_public_registry() -> None:
    definitions = {definition["name"]: definition for definition in get_tool_definitions()}

    assert "splice_transform" in definitions
    assert definitions["splice_transform"]["parameters"]["required"] == [
        "predecessor_id",
        "successor_id",
        "node",
    ]


def test_splice_transform_public_node_id_schema_matches_runtime_contract() -> None:
    definitions = {definition["name"]: definition for definition in get_tool_definitions()}
    node_id_schema = definitions["splice_transform"]["parameters"]["properties"]["node"]["properties"]["id"]
    runtime_schema = TypeAdapter(RuntimeNodeName).json_schema()

    assert runtime_schema == {
        "maxLength": 38,
        "minLength": 1,
        "pattern": r"^[a-zA-Z][a-zA-Z0-9_-]*$",
        "type": "string",
    }
    assert {key: node_id_schema[key] for key in runtime_schema} == runtime_schema


def test_splice_transform_manifest_is_type_driven() -> None:
    entry = MANIFEST["splice_transform"]

    assert entry.argument_model is SpliceTransformArgumentsModel
    assert entry.policy is None


def test_splice_transform_inserts_once_with_canonical_node_and_edge_order() -> None:
    state = _state()

    result = _execute_splice_transform(_arguments(), state, _context())

    assert result.success, result.data
    assert result.updated_state.version == state.version + 1
    assert [node.id for node in result.updated_state.nodes] == ["before", "inserted", "after"]
    assert [(edge.id, edge.from_node, edge.to_node) for edge in result.updated_state.edges] == [
        ("source-before", "source", "before"),
        ("before-after", "before", "inserted"),
        ("before-after__splice__inserted", "inserted", "after"),
        ("after-output", "after", "result"),
    ]
    inserted = result.updated_state.nodes[1]
    successor = result.updated_state.nodes[2]
    assert inserted.input == "middle"
    assert inserted.on_success == "inserted_out"
    assert successor.input == "inserted_out"
    assert result.data["already_applied"] is False


def test_splice_transform_supports_a_source_predecessor() -> None:
    state = _state()
    arguments = {
        **_arguments(),
        "predecessor_id": "source",
        "successor_id": "before",
    }

    result = _execute_splice_transform(arguments, state, _context())

    assert result.success, result.data
    assert [node.id for node in result.updated_state.nodes] == ["inserted", "before", "after"]
    assert [(edge.id, edge.from_node, edge.to_node) for edge in result.updated_state.edges] == [
        ("source-before", "source", "inserted"),
        ("source-before__splice__inserted", "inserted", "before"),
        ("before-after", "before", "after"),
        ("after-output", "after", "result"),
    ]
    assert result.updated_state.sources["source"].on_success == "rows"
    assert result.updated_state.nodes[0].input == "rows"
    assert result.updated_state.nodes[1].input == "inserted_out"


def test_splice_transform_identical_review_staged_replay_is_same_object() -> None:
    state = _state()
    arguments = {
        **_arguments(),
        "node": {
            "id": "inserted",
            "plugin": "llm",
            "options": {
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "prompt_template": "Summarise {{ row.text }}.",
                "schema": {"mode": "observed"},
            },
            "on_error": "discard",
        },
    }

    first = _execute_splice_transform(arguments, state, _context())
    assert first.success, first.data
    staged = first.updated_state.nodes[1].options[INTERPRETATION_REQUIREMENTS_KEY]
    assert staged

    replay = _execute_splice_transform(arguments, first.updated_state, _context())

    assert replay.success, replay.data
    assert replay.data["already_applied"] is True
    assert replay.updated_state is first.updated_state
    assert replay.updated_state.version == first.updated_state.version


def test_splice_transform_replay_preserves_all_trusted_requirement_ids() -> None:
    state = _state()
    arguments = {
        **_arguments(),
        "node": {
            "id": "inserted",
            "plugin": "llm",
            "options": {
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "prompt_template": "Summarise using {{interpretation:summary_style}}: {{ row.text }}.",
                "schema": {"mode": "observed"},
                INTERPRETATION_REQUIREMENTS_KEY: [
                    {
                        "kind": "vague_term",
                        "user_term": "summary_style",
                        "draft": "Use one concise sentence.",
                    }
                ],
            },
            "on_error": "discard",
        },
    }
    first = _execute_splice_transform(arguments, state, _context())
    assert first.success, first.data
    before, inserted, after = first.updated_state.nodes
    custom_ids = {
        "vague_term": "trusted-authored-review-id",
        "llm_prompt_template": "trusted-prompt-review-id",
        "llm_model_choice": "trusted-model-review-id",
    }
    trusted_requirements = [
        {
            **requirement,
            "id": custom_ids[requirement["kind"]],
        }
        for requirement in inserted.options[INTERPRETATION_REQUIREMENTS_KEY]
    ]
    retained = replace(
        first.updated_state,
        nodes=(
            before,
            replace(
                inserted,
                options={
                    **inserted.options,
                    INTERPRETATION_REQUIREMENTS_KEY: trusted_requirements,
                },
            ),
            after,
        ),
    )

    replay = _execute_splice_transform(arguments, retained, _context())

    assert replay.success, replay.data
    assert replay.data["already_applied"] is True
    assert replay.updated_state is retained
    requirements = replay.updated_state.nodes[1].options[INTERPRETATION_REQUIREMENTS_KEY]
    assert {requirement["kind"]: requirement["id"] for requirement in requirements} == custom_ids


def test_splice_transform_identical_replay_rejects_noncanonical_retained_requirements() -> None:
    state = _state()
    arguments = {
        **_arguments(),
        "node": {
            "id": "inserted",
            "plugin": "llm",
            "options": {
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "prompt_template": "Summarise {{ row.text }}.",
                "schema": {"mode": "observed"},
            },
            "on_error": "discard",
        },
    }
    first = _execute_splice_transform(arguments, state, _context())
    assert first.success, first.data
    before, inserted, after = first.updated_state.nodes
    legacy_requirements = [
        {
            field: requirement[field]
            for field in (
                "id",
                "kind",
                "user_term",
                "status",
                "draft",
            )
        }
        for requirement in inserted.options[INTERPRETATION_REQUIREMENTS_KEY]
    ]
    retained = replace(
        first.updated_state,
        nodes=(
            before,
            replace(
                inserted,
                options={
                    **inserted.options,
                    INTERPRETATION_REQUIREMENTS_KEY: legacy_requirements,
                },
            ),
            after,
        ),
    )

    replay = _execute_splice_transform(arguments, retained, _context())

    assert not replay.success
    assert replay.data["error_code"] == "interpretation_requirements_invalid"
    assert replay.updated_state is retained
    assert replay.updated_state.version == retained.version


def test_splice_transform_same_id_divergent_retry_is_atomic() -> None:
    first = _execute_splice_transform(_arguments(), _state(), _context())
    assert first.success
    divergent = _arguments(options={"schema": {"mode": "fixed", "fields": []}})

    replay = _execute_splice_transform(divergent, first.updated_state, _context())

    assert not replay.success
    assert replay.updated_state is first.updated_state
    assert replay.updated_state.version == first.updated_state.version


def test_splice_transform_same_id_noncanonical_topology_retry_is_atomic() -> None:
    first = _execute_splice_transform(_arguments(), _state(), _context())
    assert first.success
    before, inserted, after = first.updated_state.nodes
    noncanonical = replace(first.updated_state, nodes=(before, after, inserted))

    replay = _execute_splice_transform(_arguments(), noncanonical, _context())

    assert not replay.success
    assert replay.updated_state is noncanonical
    assert replay.updated_state.version == noncanonical.version


def test_splice_transform_same_id_non_transform_retry_is_atomic() -> None:
    first = _execute_splice_transform(_arguments(), _state(), _context())
    assert first.success
    before, inserted, after = first.updated_state.nodes
    noncanonical = replace(
        first.updated_state,
        nodes=(before, replace(inserted, node_type="gate", condition="True"), after),
    )

    replay = _execute_splice_transform(_arguments(), noncanonical, _context())

    assert not replay.success
    assert replay.updated_state is noncanonical
    assert replay.updated_state.version == noncanonical.version


def test_splice_transform_accepts_max_length_runtime_node_id_and_round_trips() -> None:
    max_length_id = "a" * 38
    arguments = {
        **_arguments(),
        "node": {**_arguments()["node"], "id": max_length_id},
    }
    state = _state()
    context = _context()

    result = execute_tool(
        "splice_transform",
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
    )

    assert result.success, result.data
    settings = load_settings_from_yaml_string(generate_public_yaml(result.updated_state), expand_env_vars=False)
    assert settings.transforms[1].name == max_length_id


def test_splice_transform_bounds_derived_edge_identity_with_long_direct_edge_id() -> None:
    state = _state()
    long_direct_edge = replace(state.edges[1], id="e" * 160)
    state = replace(state, edges=(state.edges[0], long_direct_edge, state.edges[2]))

    result = _execute_splice_transform(_arguments(), state, _context())

    assert result.success, result.data
    assert len(result.data["new_edge_id"]) == 160
    assert result.data["new_edge_id"].endswith("__splice__inserted")
    assert result.updated_state.edges[2].id == result.data["new_edge_id"]


@pytest.mark.parametrize(
    "node_id",
    (
        "",
        "   ",
        "1bad",
        "é",
        "bad.name",
        "_bad",
        "continue",
        "fork",
        "on_success",
        "a" * 39,
    ),
)
def test_splice_transform_public_dispatch_rejects_runtime_invalid_node_id_atomically(node_id: str) -> None:
    state = _state()
    state_before = state.to_dict()
    context = _context()
    arguments = {
        **_arguments(),
        "node": {**_arguments()["node"], "id": node_id},
    }

    with pytest.raises(ToolArgumentError, match="splice_transform arguments"):
        execute_tool(
            "splice_transform",
            arguments,
            state,
            context.catalog,
            plugin_snapshot=context.plugin_snapshot,
        )

    assert state.to_dict() == state_before
    assert state.version == 4


@pytest.mark.parametrize("node_id", ("a", "valid_id-1", "Z9"))
def test_splice_transform_public_dispatch_accepts_runtime_valid_node_id_and_round_trips(node_id: str) -> None:
    state = _state()
    context = _context()
    arguments = {
        **_arguments(),
        "node": {**_arguments()["node"], "id": node_id},
    }

    result = execute_tool(
        "splice_transform",
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
    )

    assert result.success, result.data
    assert result.updated_state.nodes[1].id == node_id
    settings = load_settings_from_yaml_string(generate_public_yaml(result.updated_state), expand_env_vars=False)
    assert settings.transforms[1].name == node_id


@pytest.mark.parametrize(
    ("state_factory", "arguments"),
    [
        (lambda: _state(), {**_arguments(), "predecessor_id": "missing"}),
        (lambda: _state(), {**_arguments(), "successor_id": "missing"}),
        (
            lambda: replace(_state(), edges=tuple(edge for edge in _state().edges if edge.id != "before-after")),
            _arguments(),
        ),
        (
            lambda: replace(
                _state(),
                nodes=(
                    replace(_state().nodes[0], node_type="gate", plugin=None, condition="True", routes={"true": "middle"}),
                    _state().nodes[1],
                ),
            ),
            _arguments(),
        ),
        (
            lambda: replace(_state(), nodes=(replace(_state().nodes[0], on_error="failure"), _state().nodes[1])),
            _arguments(),
        ),
        (
            lambda: replace(_state(), nodes=(replace(_state().nodes[0], fork_to=("result",)), _state().nodes[1])),
            _arguments(),
        ),
        (
            lambda: replace(_state(), nodes=(replace(_state().nodes[0], node_type="coalesce"), _state().nodes[1])),
            _arguments(),
        ),
        (
            lambda: replace(_state(), nodes=(replace(_state().nodes[0], node_type="queue"), _state().nodes[1])),
            _arguments(),
        ),
        (
            lambda: replace(
                _state(),
                edges=(
                    *_state().edges,
                    EdgeSpec(
                        id="before-result-ambiguous",
                        from_node="before",
                        to_node="result",
                        edge_type="on_success",
                        label=None,
                    ),
                ),
            ),
            _arguments(),
        ),
        (
            lambda: replace(
                _state(),
                nodes=(*_state().nodes, _node("other", input_name="middle", output_name="other_out")),
            ),
            _arguments(),
        ),
        (
            lambda: replace(
                _state(),
                outputs=(*_state().outputs, replace(_state().outputs[0], name="middle")),
            ),
            _arguments(),
        ),
        (
            lambda: replace(_state(), edges=(*_state().edges, _state().edges[1])),
            _arguments(),
        ),
        (
            lambda: replace(_state(), nodes=(*_state().nodes, _state().nodes[1])),
            _arguments(),
        ),
        (lambda: _state(), {**_arguments(), "successor_id": "result"}),
        (
            lambda: _state(),
            {
                **_arguments(),
                "node": {**_arguments()["node"], "id": "result"},
            },
        ),
    ],
    ids=(
        "missing-predecessor",
        "missing-successor",
        "non-direct",
        "gate",
        "error-route",
        "fork",
        "coalesce",
        "queue",
        "ambiguous-visual-path",
        "multiple-consumers",
        "sink-connection",
        "duplicate-edge-id",
        "duplicate-node-id",
        "sink-successor",
        "inserted-id-collides-with-sink",
    ),
)
def test_splice_transform_rejects_unsupported_topology_atomically(
    state_factory: Callable[[], CompositionState],
    arguments: dict[str, object],
) -> None:
    state = state_factory()

    result = _execute_splice_transform(arguments, state, _context())

    assert not result.success
    assert result.updated_state is state
    assert result.updated_state.version == state.version


def test_splice_transform_rejects_derived_edge_collision_atomically() -> None:
    state = _state()
    collision = EdgeSpec(
        id="before-after__splice__inserted",
        from_node="after",
        to_node="result",
        edge_type="on_success",
        label=None,
    )
    state = replace(state, edges=(*state.edges, collision))

    result = _execute_splice_transform(_arguments(), state, _context())

    assert not result.success
    assert result.updated_state is state


def test_splice_transform_rejects_connection_name_exhaustion_atomically() -> None:
    state = _state()
    extra_outputs = tuple(
        OutputSpec(
            name="inserted_out" if index == 1 else f"inserted_out_{index}",
            plugin="json",
            options={"schema": {"mode": "observed"}},
            on_write_failure="discard",
        )
        for index in range(1, 33)
    )
    state = replace(state, outputs=(*state.outputs, *extra_outputs))

    result = _execute_splice_transform(_arguments(), state, _context())

    assert not result.success
    assert result.updated_state is state
    assert "collision-free" in result.data["error"]


def test_splice_transform_node_options_redaction_omits_values() -> None:
    sentinel = "unique-user-option-sentinel"
    arguments = _arguments(options={"nested": {"secretish": sentinel}, "schema": {"mode": "observed"}})

    redacted = redact_tool_call_arguments(
        "splice_transform",
        arguments,
        telemetry=NoopRedactionTelemetry(),
    )

    assert sentinel not in json.dumps(redacted, sort_keys=True)
    assert redacted["predecessor_id"] == "before"
    assert redacted["successor_id"] == "after"
