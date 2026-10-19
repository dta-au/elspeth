"""Public node replacements must not silently erase omitted plugin configuration."""

from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState, NodeType
from elspeth.web.composer.tools import ToolResult, execute_tool, get_tool_definitions
from tests.unit.web.composer.test_authoring_reconciliation import _exact_arguments, _trained_context
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_tools import _empty_state


def _public_upsert(arguments: dict[str, Any], state: CompositionState) -> ToolResult:
    context = _trained_context()
    return execute_tool(
        "upsert_node",
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        validate_arguments=True,
        raise_schema_argument_errors=True,
        require_data_dir_for_paths=True,
    )


def _node_arguments(state: CompositionState) -> dict[str, Any]:
    result = _exact_arguments(state)
    assert result.success
    return deep_thaw(result.data)["nodes"][0]


@pytest.mark.parametrize("omitted", [("plugin", "options"), ("plugin",), ("options",)])
def test_partial_replacement_refuses_without_changing_approved_node(omitted: tuple[str, ...]) -> None:
    state = _approved_state()
    before = state.to_dict()
    before_hash = composition_content_hash(state)
    arguments = _node_arguments(state)
    arguments["on_success"] = "rerouted_rows"
    for field in omitted:
        del arguments[field]

    result = _public_upsert(arguments, state)

    assert not result.success
    assert result.updated_state is state
    assert result.updated_state.version == state.version
    assert result.updated_state.to_dict() == before
    assert composition_content_hash(result.updated_state) == before_hash
    assert result.updated_state.nodes[0].options["approved_prompt_artifact_hash"] == state.nodes[0].options["approved_prompt_artifact_hash"]
    assert result.updated_state.nodes[0].options["interpretation_requirements"] == state.nodes[0].options["interpretation_requirements"]
    rejection = result.validation.errors[0]
    assert rejection.error_code == "argument_error"
    assert rejection.rejected_component == "node:code_themes"
    assert "upsert_node replaces" in rejection.message
    assert "complete author-owned" in rejection.message
    assert "routing" in rejection.message
    for field in omitted:
        assert field in rejection.message


@pytest.mark.parametrize("node_type", ["aggregation", "collector"])
@pytest.mark.parametrize("omitted", [("plugin", "options"), ("plugin",), ("options",)])
def test_batch_plugin_replacement_also_refuses_omitted_configuration(node_type: NodeType, omitted: tuple[str, ...]) -> None:
    state = _approved_state()
    node = replace(
        state.nodes[0],
        node_type=node_type,
        plugin="batch_stats",
        options={"schema": {"mode": "observed"}, "value_field": "amount", "required_input_fields": ["amount"]},
        scope_name="items" if node_type == "collector" else None,
        scope_opener="expand_items" if node_type == "collector" else None,
        scope_policy="require_all" if node_type == "collector" else None,
    )
    state = state.with_node(node)
    before = state.to_dict()
    arguments = _node_arguments(state)
    for field in omitted:
        del arguments[field]

    result = _public_upsert(arguments, state)

    assert not result.success
    assert result.validation.errors[0].error_code == "argument_error"
    assert result.updated_state is state
    assert result.updated_state.to_dict() == before


def test_fully_specified_routing_replacement_preserves_exact_approval_and_events() -> None:
    state = _approved_state()
    arguments = _node_arguments(state)
    arguments["on_success"] = "rerouted_rows"

    result = _public_upsert(arguments, state)

    assert result.success, result.validation.errors
    assert result.updated_state.version > state.version
    assert result.updated_state.nodes[0].on_success == "rerouted_rows"
    assert result.updated_state.nodes[0].options == state.nodes[0].options
    assert result.updated_state.nodes[0].options["approved_prompt_artifact_hash"] == state.nodes[0].options["approved_prompt_artifact_hash"]
    assert all(
        row["status"] == "resolved" and row["event_id"] is not None
        for row in result.updated_state.nodes[0].options["interpretation_requirements"]
    )


def test_fully_specified_prompt_change_still_invalidates_approval() -> None:
    state = _approved_state()
    arguments = _node_arguments(state)
    arguments["options"]["prompt_template"] = "Summarize {{ row.text }}."

    result = _public_upsert(arguments, state)

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options["prompt_template"] == "Summarize {{ row.text }}."
    assert "approved_prompt_artifact_hash" not in options
    prompt_review = next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")
    assert prompt_review["status"] == "pending"
    assert prompt_review["event_id"] is None
    assert state.nodes[0].options["approved_prompt_artifact_hash"]


def test_explicit_plugin_replacement_does_not_merge_previous_options() -> None:
    state = _approved_state()
    arguments = _node_arguments(state)
    arguments.update(plugin="passthrough", options={"schema": {"mode": "observed"}})

    result = _public_upsert(arguments, state)

    assert result.success, result.validation.errors
    assert result.updated_state.nodes[0].plugin == "passthrough"
    assert deep_thaw(result.updated_state.nodes[0].options) == {"schema": {"mode": "observed"}}


@pytest.mark.parametrize("plugin, options", [(None, {}), ("llm", {})])
def test_explicit_null_or_empty_configuration_keeps_existing_validation(plugin: str | None, options: dict[str, Any]) -> None:
    state = _approved_state()
    arguments = _node_arguments(state)
    arguments.update(plugin=plugin, options=options)

    result = _public_upsert(arguments, state)

    assert all(entry.error_code != "argument_error" for entry in result.validation.errors)
    if plugin is None:
        assert result.success
        assert result.updated_state.nodes[0].plugin is None
        assert result.updated_state.nodes[0].options == {}
        assert any(entry.error_code == "transform_missing_plugin" for entry in result.validation.errors)
    else:
        assert not result.success
        assert result.updated_state is state
        assert result.validation.errors[0].error_code == "plugin_options_invalid"


def test_fresh_node_omissions_keep_existing_incomplete_construction_behavior() -> None:
    state = _empty_state()
    arguments = {"id": "new_step", "node_type": "transform", "input": "rows", "on_error": "discard"}

    result = _public_upsert(arguments, state)

    assert result.success, result.validation.errors
    assert result.updated_state.nodes[0].plugin is None
    assert result.updated_state.nodes[0].options == {}
    assert any(entry.error_code == "transform_missing_plugin" for entry in result.validation.errors)

    # A saved incomplete node has no configured plugin to erase either.
    repeated = _public_upsert(arguments, result.updated_state)
    assert repeated.success, repeated.validation.errors
    assert repeated.updated_state.nodes[0].plugin is None


def test_plugin_kind_change_still_requires_explicit_replacement_configuration() -> None:
    state = _approved_state()
    arguments = {"id": "code_themes", "node_type": "aggregation", "input": "source_out", "on_error": "discard"}

    result = _public_upsert(arguments, state)

    assert not result.success
    assert result.validation.errors[0].error_code == "argument_error"
    assert result.updated_state is state


def test_explicit_conversion_to_structural_kind_keeps_its_omission_rules() -> None:
    state = _approved_state()
    arguments = {
        "id": "code_themes",
        "node_type": "gate",
        "input": "source_out",
        "condition": "True",
        "routes": {"true": "main", "false": "main"},
    }

    result = _public_upsert(arguments, state)

    assert result.success, result.validation.errors
    assert result.updated_state.nodes[0].node_type == "gate"
    assert result.updated_state.nodes[0].plugin is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"id": "route_step", "node_type": "gate", "input": "rows", "condition": "True", "routes": {"true": "left", "false": "right"}},
        {"id": "merge_step", "node_type": "coalesce", "input": "left", "branches": ["left", "right"]},
        {"id": "union_step", "node_type": "row_union", "input": "left", "branches": ["left", "right"], "on_success": "merged_rows"},
        {"id": "rows", "node_type": "queue", "input": "rows"},
    ],
    ids=["gate", "coalesce", "row-union", "queue"],
)
def test_structural_node_create_and_update_still_allow_omitted_plugin_options(arguments: dict[str, Any]) -> None:
    created = _public_upsert(arguments, _empty_state())
    assert created.success, created.validation.errors

    updated = _public_upsert({**arguments, "description": "Updated step description."}, created.updated_state)

    assert updated.success, updated.validation.errors
    assert updated.updated_state.nodes[0].plugin is None
    assert updated.updated_state.nodes[0].description == "Updated step description."


def test_public_declaration_teaches_full_replacement_without_global_required_fields() -> None:
    definition = next(tool for tool in get_tool_definitions() if tool["name"] == "upsert_node")
    assert "replaces" in definition["description"]
    assert "complete author-owned" in definition["description"]
    assert "routing" in definition["description"]
    assert set(definition["parameters"]["required"]) == {"id", "node_type", "input"}
