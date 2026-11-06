"""The live loop and boot surface disclose the canonical runtime naming rules."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.capability_skill import load_pipeline_capability_core, validate_capability_field_contract
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments, wire_tool_definitions
from tests.unit.web.composer._scripted_sdk import _pipeline


def _set_pipeline(tools):
    return next(tool["function"] for tool in tools if tool["function"]["name"] == "set_pipeline")


@pytest.mark.parametrize("dialect", tuple(ToolContractDialect))
def test_actual_loop_set_pipeline_advertises_runtime_naming_and_documented_fields(dialect):
    tools = provider_gateway.composer_loop_tool_definitions(dialect)
    function = _set_pipeline(tools)
    schema = function["parameters"]["properties"]["pipeline"]
    expected = canonical_set_pipeline_schema()
    assert schema == expected
    validate_capability_field_contract(schema, load_pipeline_capability_core())
    node_id = schema["properties"]["nodes"]["items"]["properties"]["id"]
    assert node_id["pattern"] and node_id["maxLength"] > 0
    registered = next(definition for definition in get_tool_definitions() if definition["name"] == "set_pipeline")
    assert schema != registered["parameters"]
    if dialect is ToolContractDialect.OPENAI_STRICT:
        assert function["strict"] is False


@pytest.mark.parametrize("dialect", tuple(ToolContractDialect))
def test_loop_schema_is_fresh_and_keeps_wire_framing_and_stamps(dialect):
    baseline = wire_tool_definitions(dialect)
    before = deepcopy(baseline)
    first = _set_pipeline(provider_gateway.composer_loop_tool_definitions(dialect))
    original = _set_pipeline(before)
    assert first.keys() == original.keys()
    assert first["parameters"].keys() == original["parameters"].keys()
    assert first["parameters"]["required"] == ["pipeline"]
    assert first["parameters"]["additionalProperties"] is False
    first["parameters"]["properties"]["pipeline"]["properties"]["nodes"]["items"]["properties"].clear()
    second = _set_pipeline(provider_gateway.composer_loop_tool_definitions(dialect))
    assert second["parameters"]["properties"]["pipeline"] == canonical_set_pipeline_schema()
    assert wire_tool_definitions(dialect) == baseline == before


def test_actual_loop_builder_refuses_schema_document_field_drift(monkeypatch):
    schema = canonical_set_pipeline_schema()
    schema["properties"]["future_topology"] = {"type": "object"}
    tools = wire_tool_definitions(ToolContractDialect.NONE)
    _set_pipeline(tools)["parameters"]["properties"]["pipeline"] = schema
    monkeypatch.setattr(provider_gateway, "wire_tool_definitions", lambda dialect: tools)
    with pytest.raises(AuditIntegrityError, match="advertised pipeline schema differs from canonical"):
        provider_gateway.composer_loop_tool_definitions(ToolContractDialect.NONE)


def test_actual_loop_builder_checks_document_even_when_schema_copies_match(monkeypatch):
    schema = canonical_set_pipeline_schema()
    schema["properties"]["future_topology"] = {"type": "object"}
    tools = wire_tool_definitions(ToolContractDialect.NONE)
    _set_pipeline(tools)["parameters"]["properties"]["pipeline"] = schema
    monkeypatch.setattr(provider_gateway, "wire_tool_definitions", lambda dialect: tools)
    monkeypatch.setattr(provider_gateway, "canonical_set_pipeline_schema", lambda: deepcopy(schema))
    with pytest.raises(AuditIntegrityError, match="documented capability fields drifted"):
        provider_gateway.composer_loop_tool_definitions(ToolContractDialect.NONE)


def test_actual_loop_builder_refuses_missing_set_pipeline(monkeypatch):
    tools = [tool for tool in wire_tool_definitions(ToolContractDialect.NONE) if tool["function"]["name"] != "set_pipeline"]
    monkeypatch.setattr(provider_gateway, "wire_tool_definitions", lambda dialect: tools)
    with pytest.raises(AuditIntegrityError, match="must advertise set_pipeline"):
        provider_gateway.composer_loop_tool_definitions(ToolContractDialect.NONE)


@pytest.mark.parametrize("dialect", tuple(ToolContractDialect))
@pytest.mark.parametrize("node_id,valid", [("clean_records", True), ("invalid name", False)])
def test_wire_conformance_uses_the_same_naming_schema_as_the_actual_loop(tmp_path, dialect, node_id, valid):
    pipeline = _pipeline(tmp_path)
    pipeline["nodes"] = [
        {
            "id": node_id,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "rows",
            "on_error": "discard",
            "options": {},
        }
    ]
    raw = {"pipeline": pipeline}
    schema = _set_pipeline(provider_gateway.composer_loop_tool_definitions(dialect))["parameters"]
    assert Draft202012Validator(schema).is_valid(raw) is valid
    assert decode_wire_arguments("set_pipeline", dialect, raw).wire_conformant is valid
