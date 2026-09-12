"""Trusted policy projection admission and closed provider envelope contracts."""

import json
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.guided.planning import guided_redacted_current_state_context
from elspeth.web.composer.planner_authoring_aids import PlannerPluginContract
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.provider_discovery_response import (
    admit_provider_current_state,
    argument_error_response,
    closed_provider_envelope,
    projected_plugin_contract_response,
    provider_node_response,
    provider_output_response,
    provider_sources_response,
    provider_state_response,
    schema_budget_failure,
    schema_projection_failure,
    surface_projection_failure,
)
from elspeth.web.composer.state import ValidationEntry
from elspeth.web.composer.tools._common import ToolResult
from tests.unit.web.composer.test_state_response_contracts import state_cases


@pytest.mark.parametrize("case", list(state_cases()))
def test_actual_policy_context_roundtrips(case):
    state, _ = state_cases()[case]
    raw = guided_redacted_current_state_context(state)
    owned = admit_provider_current_state(raw)
    assert json.dumps(provider_state_response(owned).to_wire()) == json.dumps(raw)
    assert provider_sources_response(owned).to_wire() == {"sources": raw["sources"]}
    for node, expected in zip(owned.nodes, raw["nodes"], strict=True):
        assert provider_node_response(node).to_wire() == {"node": expected}
    for output, expected in zip(owned.outputs, raw["outputs"], strict=True):
        assert provider_output_response(output).to_wire() == {"output": expected}


@pytest.mark.parametrize("key", ["schema", "version", "sources", "nodes", "outputs"])
def test_required_context_fields(key):
    state, _ = state_cases()["full"]
    raw = guided_redacted_current_state_context(state)
    del raw[key]
    with pytest.raises(FrameworkBugError):
        admit_provider_current_state(raw)


@pytest.mark.parametrize(
    "key,value", [("schema", "unknown"), ("version", True), ("sources", {}), ("nodes", None), ("outputs", ""), ("extra", [])]
)
def test_reject_context_corruption(key, value):
    state, _ = state_cases()["full"]
    raw = guided_redacted_current_state_context(state)
    raw[key] = value
    with pytest.raises(FrameworkBugError):
        admit_provider_current_state(raw)


def test_context_is_deeply_immutable_and_encoding_is_fresh():
    state, _ = state_cases()["full"]
    raw = guided_redacted_current_state_context(state)
    owned = admit_provider_current_state(MappingProxyType(raw))
    before = provider_state_response(owned).to_wire()
    raw["nodes"][0]["option_keys"].append("private")
    assert provider_state_response(owned).to_wire() == before
    with pytest.raises(FrozenInstanceError):
        owned.nodes[0].plugin = "changed"
    encoded = provider_state_response(owned).to_wire()
    encoded["nodes"].clear()
    assert provider_state_response(owned).to_wire() == before


def test_envelope_never_reads_raw_result_data():
    state, _ = state_cases()["full"]
    result = ToolResult(success=True, updated_state=state, validation=state.validate(), affected_nodes=(), data=None)
    object.__setattr__(result, "data", object())
    envelope = closed_provider_envelope(result)
    assert "data" not in envelope.to_wire()
    assert envelope.to_wire()["validation"]["semantic_contracts"] == []
    assert envelope.to_wire()["validation"]["graph_repair_suggestions"] == []


@pytest.mark.parametrize("family", ["sources", "nodes", "outputs"])
def test_every_nested_field_is_required_and_extras_are_rejected(family):
    state, _ = state_cases()["full"]
    original = guided_redacted_current_state_context(state)
    for key in original[family][0]:
        raw = guided_redacted_current_state_context(state)
        del raw[family][0][key]
        with pytest.raises(FrameworkBugError):
            admit_provider_current_state(raw)
    raw = guided_redacted_current_state_context(state)
    raw[family][0]["unexpected_private"] = "secret"
    with pytest.raises(FrameworkBugError):
        admit_provider_current_state(raw)


@pytest.mark.parametrize("family", ["sources", "nodes", "outputs"])
def test_every_nested_field_rejects_wrong_types(family):
    state, _ = state_cases()["full"]
    original = guided_redacted_current_state_context(state)
    for key in original[family][0]:
        raw = guided_redacted_current_state_context(state)
        raw[family][0][key] = [False] if key == "option_keys" else 42
        with pytest.raises(FrameworkBugError):
            admit_provider_current_state(raw)


def test_closed_node_vocabulary_and_legitimate_nulls():
    state, _ = state_cases()["full"]
    raw = guided_redacted_current_state_context(state)
    raw["nodes"][0].update(node_type="gate", plugin=None, on_success=None, on_error=None)
    owned = admit_provider_current_state(raw)
    assert owned.nodes[0].plugin is None
    raw["nodes"][0]["node_type"] = "unknown"
    with pytest.raises(FrameworkBugError):
        admit_provider_current_state(raw)


def test_envelope_preserves_order_codes_and_independent_versions():
    state, _ = state_cases()["full"]
    validation = replace(
        state.validate(),
        errors=(ValidationEntry("private component", "private message", "high", "known"),),
        warnings=(ValidationEntry("private", "private", "medium"),),
        suggestions=(ValidationEntry("private", "private", "low"),),
    )
    context = guided_redacted_current_state_context(state)
    context["version"] = 101
    response = provider_state_response(admit_provider_current_state(context))
    result = ToolResult(True, state, validation, ("t1",), data={"private": "secret"})
    envelope = closed_provider_envelope(result, success=False, data=response)
    wire = envelope.to_wire()
    assert list(wire) == ["success", "validation", "affected_nodes", "version", "data"]
    assert list(wire["validation"]) == ["is_valid", "errors", "warnings", "suggestions", "semantic_contracts", "graph_repair_suggestions"]
    assert wire["validation"]["errors"] == [{"component": "pipeline", "severity": "high", "error_code": "known"}]
    assert wire["validation"]["warnings"][0]["error_code"] == "validation_warning"
    assert wire["validation"]["suggestions"][0]["error_code"] == "validation_suggestion"
    assert wire["version"] == state.version
    assert wire["data"]["version"] == 101
    assert wire["success"] is False
    assert "private" not in json.dumps(wire)
    with pytest.raises(FrozenInstanceError):
        envelope.validation.errors[0].error_code = "changed"


@pytest.mark.parametrize(
    "factory,code",
    [
        (surface_projection_failure, "surface_projection_unavailable"),
        (schema_projection_failure, "schema_projection_unavailable"),
        (schema_budget_failure, "schema_contract_budget_exceeded"),
    ],
)
def test_fixed_failure_projections(factory, code):
    response = factory()
    wire = response.to_wire()
    assert wire["error_code"] == code
    assert list(wire) == (["error", "error_code"] if factory is surface_projection_failure else ["error", "error_code", "next_tool"])
    with pytest.raises(FrameworkBugError, match="cannot be cached"):
        response.readmit(None)


def test_projected_plugin_contract_preserves_owned_encoding_bytes():
    contract = PlannerPluginContract("transform/example", "known-hash", {"type": "object", "properties": {}}, {"fields": []}, ("hint é",))
    response = projected_plugin_contract_response(contract)
    assert json.dumps(response.to_wire()) == json.dumps(contract.to_dict())
    response.to_wire()["json_schema"]["properties"]["extra"] = {"type": "string"}
    assert json.dumps(response.to_wire()) == json.dumps(contract.to_dict())
    with pytest.raises(FrameworkBugError, match="cannot be cached"):
        response.readmit(None)


@pytest.mark.parametrize("code", [None, "SCHEMA_VALIDATION"])
def test_argument_error_projection_preserves_exact_bytes(code):
    error = ToolArgumentError(argument="content", expected="a string", actual_type="int", code=code)
    response = argument_error_response(error)
    expected = {
        "argument_error": {
            "component": "content",
            "severity": "high",
            "error_code": code or "argument_error",
            "error_class": "ToolArgumentError",
        }
    }
    assert json.dumps(response.to_wire()) == json.dumps(expected)
    with pytest.raises(FrameworkBugError, match="cannot be cached"):
        response.readmit(None)
