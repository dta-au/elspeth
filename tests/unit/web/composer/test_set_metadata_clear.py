"""set_metadata: explicit null clears description, the receipt names what changed, a no-op says so.

Battery defect (0.8.16): asked to remove the pipeline description, the composer
sent a patch carrying only the unchanged ``name``, got a plain success receipt
and told the user the description was removed. Null was not admitted, the
receipt did not say which fields changed, and a patch that changed nothing
advanced the version like a fresh edit.

Every call here goes through the production argument gates
(``validate_arguments`` + ``raise_schema_argument_errors``), the same flags the
compose loop and the composer MCP server pass, so a null the declared schema
refuses is refused here too.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import REDACTED_UNKNOWN_RESPONSE_FIELD, redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import ToolResult, get_tool_definitions
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments, wire_tool_definitions
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool


def _state(*, name: str = "Orders", description: str = "Joins orders to customers") -> CompositionState:
    return replace(_empty_state(), metadata=PipelineMetadata(name=name, description=description))


def _set_metadata(patch: dict[str, Any], state: CompositionState) -> ToolResult:
    return execute_tool(
        "set_metadata",
        {"patch": patch},
        state,
        _mock_catalog(),
        validate_arguments=True,
        raise_schema_argument_errors=True,
    )


def _set_metadata_schema() -> dict[str, Any]:
    definition: dict[str, Any] = next(d for d in get_tool_definitions() if d["name"] == "set_metadata")
    return definition


def test_null_description_clears_it_and_the_receipt_names_it() -> None:
    state = _state()

    result = _set_metadata({"description": None}, state)

    assert result.success is True
    assert result.updated_state.metadata.description == ""
    assert result.updated_state.metadata.name == "Orders"
    assert result.updated_state.version == state.version + 1
    assert result.data is not None
    assert list(result.data["changed_fields"]) == ["description"]
    assert "message" not in result.data
    assert result.to_dict()["data"] == {"changed_fields": ["description"]}


def test_omitted_field_is_unchanged() -> None:
    state = _state()

    result = _set_metadata({"name": "Orders v2"}, state)

    assert result.success is True
    assert result.updated_state.metadata.name == "Orders v2"
    assert result.updated_state.metadata.description == "Joins orders to customers"
    assert result.data is not None
    assert list(result.data["changed_fields"]) == ["name"]


def test_both_fields_changed_are_both_named_in_declared_order() -> None:
    result = _set_metadata({"description": "New", "name": "Renamed"}, _state())

    assert result.data is not None
    assert list(result.data["changed_fields"]) == ["name", "description"]


def test_patch_resending_the_unchanged_name_is_a_reported_no_op() -> None:
    """The exact battery call: only the unchanged name, meant as 'remove the description'."""
    state = _state()

    result = _set_metadata({"name": "Orders"}, state)

    assert result.success is True
    assert result.data is not None
    assert list(result.data["changed_fields"]) == []
    assert result.data["message"] == "No field changed: the supplied values match the saved metadata."
    # A no-op is still an edit: it writes a version (required-control staging depends on it),
    # and the receipt, not the version, says that nothing changed.
    assert result.updated_state.version == state.version + 1
    assert result.updated_state.metadata == state.metadata


def test_clearing_an_already_empty_description_is_a_no_op() -> None:
    state = _state(description="")

    result = _set_metadata({"description": None}, state)

    assert result.data is not None
    assert list(result.data["changed_fields"]) == []
    assert result.updated_state.metadata == state.metadata
    assert result.updated_state.version == state.version + 1


def test_empty_patch_is_a_no_op() -> None:
    state = _state()

    result = _set_metadata({}, state)

    assert result.data is not None
    assert list(result.data["changed_fields"]) == []
    assert result.updated_state.metadata == state.metadata
    assert result.updated_state.version == state.version + 1


def test_only_the_field_that_changed_is_named() -> None:
    state = _state()

    result = _set_metadata({"name": "Orders", "description": None}, state)

    assert result.data is not None
    assert list(result.data["changed_fields"]) == ["description"]
    assert result.updated_state.version == state.version + 1


def test_null_name_is_refused_and_state_is_untouched() -> None:
    """A pipeline always has a name (PipelineMetadata.name is a non-optional str)."""
    state = _state()

    with pytest.raises(ToolArgumentError) as exc_info:
        _set_metadata({"name": None}, state)

    # The S gate's closed repair signal points the planner at the field.
    assert [violation.loc for violation in exc_info.value.schema_violations] == [("patch", "name")]
    assert state.metadata.name == "Orders"
    # The why lives in the schema the planner reads.
    name_schema = _set_metadata_schema()["parameters"]["properties"]["patch"]["properties"]["name"]
    assert "cannot be cleared" in name_schema["description"].lower()


def test_schema_admits_null_for_description_only() -> None:
    validator = Draft202012Validator(_set_metadata_schema()["parameters"])

    assert validator.is_valid({"patch": {"description": None}})
    assert not validator.is_valid({"patch": {"name": None}})


def test_tool_description_teaches_null_clear_and_the_receipt() -> None:
    description = _set_metadata_schema()["description"]

    assert "null" in description
    assert "`changed_fields`" in description
    assert "`message`" in description


@pytest.mark.parametrize("patch", [{"description": None}, {"name": "Orders"}])
def test_persisted_receipt_admits_the_data_key(patch: dict[str, Any]) -> None:
    result = _set_metadata(patch, _state())

    stored = redact_tool_call_response("set_metadata", result.to_dict(), telemetry=NoopRedactionTelemetry())

    assert "data" in stored
    assert REDACTED_UNKNOWN_RESPONSE_FIELD not in stored


# OpenAI-strict routes make every optional key required and decode a null there
# as an omission, so on that dialect null leaves description unchanged and the
# empty string is what clears it. The none dialect admits both.


def _strict_set_metadata(wire_patch: dict[str, Any], state: CompositionState) -> ToolResult:
    decoded = decode_wire_arguments("set_metadata", ToolContractDialect.OPENAI_STRICT, {"patch": wire_patch})
    assert decoded.wire_conformant
    # tool_batch thaws the decoded arguments before dispatch; do the same.
    arguments: dict[str, Any] = deep_thaw(decoded.semantic)
    return _set_metadata(arguments["patch"], state)


def test_empty_string_clears_description_on_every_dialect() -> None:
    state = _state()

    none_result = _set_metadata({"description": ""}, state)
    strict_result = _strict_set_metadata({"name": None, "description": ""}, state)

    for result in (none_result, strict_result):
        assert result.updated_state.metadata.description == ""
        assert result.data is not None
        assert list(result.data["changed_fields"]) == ["description"]


def test_strict_null_description_is_unchanged_not_cleared() -> None:
    state = _state()

    result = _strict_set_metadata({"name": "Orders v2", "description": None}, state)

    assert result.updated_state.metadata.description == "Joins orders to customers"
    assert result.data is not None
    assert list(result.data["changed_fields"]) == ["name"]


def test_strict_description_teaches_the_empty_string_not_null() -> None:
    strict = next(
        tool["function"] for tool in wire_tool_definitions(ToolContractDialect.OPENAI_STRICT) if tool["function"]["name"] == "set_metadata"
    )
    none = next(tool["function"] for tool in wire_tool_definitions(ToolContractDialect.NONE) if tool["function"]["name"] == "set_metadata")

    assert "Pass an empty string for description to clear it (null leaves it unchanged);" in strict["description"]
    assert "Pass null or an empty string for description to clear it;" in none["description"]
