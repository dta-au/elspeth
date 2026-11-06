"""Fork authoring instructions agree with the admitted gate contracts."""

from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.core.config import GateSettings
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext, ToolResult, execute_tool, get_tool_definitions
from tests.unit.web.composer.test_authoring_reconciliation import _trained_context


@pytest.fixture(scope="module")
def context() -> ToolContext:
    return _trained_context()


def _state(*, include_failed_rows: bool = True) -> CompositionState:
    output_names = ("branch_one", "branch_two", "failed_rows") if include_failed_rows else ("branch_one", "branch_two")
    return CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard"),
        nodes=(),
        edges=(),
        outputs=tuple(OutputSpec(name=name, plugin="csv", options={}, on_write_failure="discard") for name in output_names),
        metadata=PipelineMetadata(name="fork conformance"),
        version=7,
    )


def _arguments() -> dict[str, Any]:
    return {
        "id": "split_rows",
        "node_type": "gate",
        "input": "rows",
        "condition": "row['accepted'] == True",
        "routes": {"true": "fork", "false": "failed_rows"},
        "fork_to": ["branch_one", "branch_two"],
    }


def _upsert(arguments: dict[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
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


@pytest.mark.parametrize("surface", ["routes", "fork_to", "summary"])
def test_delivered_fork_guidance_allows_the_real_compound_call(surface: str, context: ToolContext) -> None:
    arguments = _arguments()
    result = _upsert(arguments, _state(), context)
    assert result.success, result.validation.errors
    assert result.updated_state.validate().is_valid, result.validation.errors
    gate = GateSettings(
        name=arguments["id"],
        input=arguments["input"],
        condition=arguments["condition"],
        routes=arguments["routes"],
        fork_to=arguments["fork_to"],
    )
    assert gate.routes == arguments["routes"]
    assert gate.fork_to == arguments["fork_to"]

    declaration = next(tool for tool in get_tool_definitions() if tool["name"] == "upsert_node")
    if surface == "summary":
        guidance = declaration["description"]
        assert "condition+routes (or fork_to)" not in guidance
    else:
        guidance = declaration["parameters"]["properties"][surface]["description"]
        assert "mutually exclusive" not in guidance.lower()
        assert "'fork'" in guidance
    assert "routes" in guidance
    assert "fork_to" in guidance


def test_unconditional_fork_keeps_both_boolean_route_labels(context: ToolContext) -> None:
    arguments = {**_arguments(), "condition": "True", "routes": {"true": "fork", "false": "fork"}}

    result = _upsert(arguments, _state(include_failed_rows=False), context)

    assert result.success, result.validation.errors
    assert result.updated_state.validate().is_valid, result.validation.errors
    node = result.updated_state.nodes[0]
    assert node.routes == {"true": "fork", "false": "fork"}
    assert node.fork_to == ("branch_one", "branch_two")
    assert GateSettings(name=node.id, input=node.input, condition=node.condition, routes=node.routes, fork_to=list(node.fork_to))


@pytest.mark.parametrize(
    "routes", [None, {}, {"true": "fork"}, {"all": "fork"}], ids=["omitted", "empty", "missing-false", "nonboolean-label"]
)
def test_boolean_fork_without_required_route_labels_refuses_and_preserves_state(
    routes: dict[str, str] | None, context: ToolContext
) -> None:
    accepted = _upsert(_arguments(), _state(), context)
    assert accepted.success, accepted.validation.errors
    state = accepted.updated_state
    before = state.to_dict()
    before_hash = composition_content_hash(state)
    arguments = {**_arguments(), "condition": "True"}
    if routes is None:
        del arguments["routes"]
    else:
        arguments["routes"] = routes

    result = _upsert(arguments, state, context)

    assert not result.success
    assert result.updated_state is state
    assert result.updated_state.version == state.version
    assert result.updated_state.to_dict() == before
    assert composition_content_hash(result.updated_state) == before_hash
    [rejection] = result.validation.errors
    assert rejection.error_code == "gate_route_labels_mismatch"
    assert rejection.rejected_component == "node:split_rows"
    assert "Missing required labels" in rejection.message


@pytest.mark.parametrize(
    ("routes", "fork_to", "error_code", "runtime_message"),
    [
        ({"true": "fork", "false": "failed_rows"}, None, "gate_fork_route_without_fork_to", "fork_to is required"),
        (
            {"true": "branch_one", "false": "failed_rows"},
            ["branch_one", "branch_two"],
            "gate_fork_to_without_fork_route",
            "fork_to is only valid",
        ),
    ],
    ids=["fork-route-without-destinations", "destinations-without-fork-route"],
)
def test_incomplete_fork_combinations_report_the_runtime_consistency_error(
    routes: dict[str, str], fork_to: list[str] | None, error_code: str, runtime_message: str, context: ToolContext
) -> None:
    arguments = {**_arguments(), "routes": routes, "fork_to": fork_to}

    result = _upsert(arguments, _state(), context)

    # Incremental authoring admits incomplete topology but cannot validate it.
    assert result.success
    assert not result.updated_state.validate().is_valid
    assert any(entry.error_code == error_code for entry in result.validation.errors), result.validation.errors
    with pytest.raises(ValidationError, match=runtime_message):
        GateSettings(name=arguments["id"], input=arguments["input"], condition=arguments["condition"], routes=routes, fork_to=fork_to)
