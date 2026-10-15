"""Closed provider envelope and policy-owned discovery response contracts."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest
from jsonschema import Draft202012Validator

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
            "error_category": error.category.value,
        }
    }
    assert json.dumps(response.to_wire()) == json.dumps(expected)
    with pytest.raises(FrameworkBugError, match="cannot be cached"):
        response.readmit(None)


@pytest.mark.parametrize(
    ("arguments", "loc", "code", "message", "category"),
    [
        (
            {"plugin_type": "node", "name": "ok"},
            ["plugin_type"],
            "invalid_choice",
            "Value is not an allowed choice",
            ToolArgumentErrorCategory.SCHEMA_SHAPE,
        ),
        (
            {"plugin_type": "source", "name": 19},
            ["name"],
            "invalid_type",
            "Value has invalid type",
            ToolArgumentErrorCategory.SCHEMA_SHAPE,
        ),
        (
            {"plugin_type": "source", "name": "PRIVATE_REJECTED_VALUE"},
            ["name"],
            "out_of_bounds",
            "Value is outside allowed bounds",
            ToolArgumentErrorCategory.SCHEMA_BOUND,
        ),
        (
            {"plugin_type": "source", "name": "ok", "PRIVATE_API_KEY_CANARY": "PRIVATE_REJECTED_VALUE"},
            [],
            "unexpected",
            "Unexpected value",
            ToolArgumentErrorCategory.SCHEMA_SHAPE,
        ),
        (
            {"plugin_type": "source", "name": "ok", "options": {"PRIVATE_API_KEY_CANARY": "PRIVATE_REJECTED_VALUE"}},
            ["options", "item"],
            "invalid_type",
            "Value has invalid type",
            ToolArgumentErrorCategory.SCHEMA_SHAPE,
        ),
        ([], [], "invalid_type", "Value has invalid type", ToolArgumentErrorCategory.SCHEMA_SHAPE),
    ],
    ids=["choice", "type", "bound", "unknown-root", "unknown-mapping-key", "malformed-root"],
)
def test_argument_error_projection_keeps_closed_repair_facts_without_private_input(arguments, loc, code, message, category):
    from elspeth.web.composer.pipeline_planner import _allowlisted_argument_error_entry
    from elspeth.web.composer.tools._dispatch import require_arguments_conform_to_schema

    schema = {
        "type": "object",
        "properties": {
            "plugin_type": {"type": "string", "enum": ["source", "transform", "sink"]},
            "name": {"type": "string", "maxLength": 4},
            "options": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
        "required": ["plugin_type", "name"],
        "additionalProperties": False,
    }
    with pytest.raises(ToolArgumentError) as caught:
        require_arguments_conform_to_schema("get_plugin_schema", Draft202012Validator(schema), arguments)
    error = caught.value
    error.__cause__ = RuntimeError("PRIVATE_CAUSE_CANARY")

    response = argument_error_response(error)
    entry = response.to_wire()["argument_error"]
    assert entry["error_category"] == category.value
    assert entry["validation_errors"] == [{"loc": loc, "msg": message, "type": code}]
    assert _allowlisted_argument_error_entry(error) == entry
    encoded = json.dumps(response.to_wire())
    assert "PRIVATE_" not in encoded
    entry["validation_errors"][0]["loc"].append("MUTATED_WIRE")
    assert "MUTATED_WIRE" not in json.dumps(response.to_wire())


def test_argument_error_projection_marks_over_budget_without_leaking_values():
    from elspeth.web.composer.tools._dispatch import require_arguments_conform_to_schema

    with pytest.raises(ToolArgumentError) as caught:
        require_arguments_conform_to_schema(
            "get_plugin_schema",
            Draft202012Validator({"type": "array", "items": {"type": "integer"}}),
            ["PRIVATE_REJECTED_VALUE"] * 9,
        )
    entry = argument_error_response(caught.value).to_wire()["argument_error"]
    assert entry["validation_errors"] == [{"loc": [], "msg": "Validation produced more than 8 errors", "type": "truncated"}]
    assert "PRIVATE_" not in json.dumps(entry)
