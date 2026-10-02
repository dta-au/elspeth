"""Closed provider envelope and policy-owned discovery response contracts."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.planner_authoring_aids import PlannerPluginContract
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.provider_discovery_response import (
    argument_error_response,
    closed_provider_envelope,
    projected_plugin_contract_response,
    schema_budget_failure,
    schema_projection_failure,
)
from elspeth.web.composer.state import ValidationEntry
from elspeth.web.composer.tools._common import ToolResult
from tests.unit.web.composer.test_state_response_contracts import state_cases


def test_envelope_never_reads_raw_result_data():
    state, _ = state_cases()["full"]
    result = ToolResult(success=True, updated_state=state, validation=state.validate(), affected_nodes=(), data=None)
    object.__setattr__(result, "data", object())
    envelope = closed_provider_envelope(result)
    assert "data" not in envelope.to_wire()
    assert envelope.to_wire()["validation"]["semantic_contracts"] == []
    assert envelope.to_wire()["validation"]["graph_repair_suggestions"] == []


def test_envelope_preserves_validation_codes_and_owns_projected_data() -> None:
    state, _ = state_cases()["full"]
    validation = replace(
        state.validate(),
        errors=(ValidationEntry("private component", "private message", "high", "known"),),
        warnings=(ValidationEntry("private", "private", "medium"),),
        suggestions=(ValidationEntry("private", "private", "low"),),
    )
    contract = PlannerPluginContract("transform/example", "known-hash", {"type": "object"}, {"fields": []}, ())
    result = ToolResult(True, state, validation, ("t1",), data={"private": "secret"})
    envelope = closed_provider_envelope(result, success=False, data=projected_plugin_contract_response(contract))
    wire = envelope.to_wire()

    assert list(wire) == ["success", "validation", "affected_nodes", "version", "data"]
    assert list(wire["validation"]) == ["is_valid", "errors", "warnings", "suggestions", "semantic_contracts", "graph_repair_suggestions"]
    assert wire["validation"]["errors"] == [{"component": "pipeline", "severity": "high", "error_code": "known"}]
    assert wire["validation"]["warnings"][0]["error_code"] == "validation_warning"
    assert wire["validation"]["suggestions"][0]["error_code"] == "validation_suggestion"
    assert wire["version"] == state.version
    assert wire["data"] == contract.to_dict()
    assert wire["success"] is False
    assert "private" not in json.dumps(wire)
    with pytest.raises(FrozenInstanceError):
        envelope.validation.errors[0].error_code = "changed"


@pytest.mark.parametrize(
    "factory,code",
    [
        (schema_projection_failure, "schema_projection_unavailable"),
        (schema_budget_failure, "schema_contract_budget_exceeded"),
    ],
)
def test_fixed_failure_projections(factory, code):
    response = factory()
    wire = response.to_wire()
    assert wire["error_code"] == code
    assert list(wire) == ["error", "error_code", "next_tool"]
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


@pytest.mark.parametrize(
    ("code", "category"),
    [(None, None), ("SCHEMA_VALIDATION", ToolArgumentErrorCategory.SCHEMA_SHAPE)],
)
def test_argument_error_projection_preserves_exact_bytes(code, category):
    error = ToolArgumentError(argument="content", expected="a string", actual_type="int", code=code, category=category)
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
