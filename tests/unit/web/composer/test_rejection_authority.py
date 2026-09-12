"""Rejection facts survive consumer boundaries without data-field aliases."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.no_tool_policy import _tool_failure_detail
from elspeth.web.composer.redaction import GetBlobContentResponseModel, redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import ValidationEntry, ValidationSummary
from elspeth.web.composer.tools import ToolResult, execute_tool
from elspeth.web.composer.tools._common import _failure_result, _merged_component_rejection_result
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.routes._helpers import _GUIDED_SOURCE_PATH_ALLOWLIST_DETAIL, _guided_source_commit_failure_detail
from tests.unit.web.composer._helpers import _empty_state, _mock_catalog


def test_blob_rejection_serializes_and_redacts_without_data() -> None:
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    result = execute_tool(
        "get_blob_content",
        {"blob_id": "11111111-1111-4111-8111-111111111111"},
        _empty_state(),
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
    )
    assert not result.success
    wire = result.to_dict()
    assert "data" not in wire
    assert wire["validation"]["errors"][0]["message"] == "Blob tools require session context."
    redacted = redact_tool_call_response("get_blob_content", wire, telemetry=NoopRedactionTelemetry())
    assert redacted["success"] is False
    assert "data" not in redacted
    with pytest.raises(ValidationError, match="requires data"):
        GetBlobContentResponseModel.model_validate({**wire, "success": True})


@pytest.mark.parametrize("feeder", ["empty", "successful", "no_errors", "standing_error"])
def test_component_merge_rejects_malformed_feeders(feeder: str) -> None:
    rejection = _failure_result(_empty_state(), "actual rejection", with_state_validation=False)
    match feeder:
        case "empty":
            results = []
        case "successful":
            results = [rejection, replace(rejection, success=True)]
        case "no_errors":
            results = [replace(rejection, validation=ValidationSummary(is_valid=True, errors=()))]
        case "standing_error":
            results = [replace(rejection, validation=_empty_state().validate())]
        case _:
            raise AssertionError(feeder)
    with pytest.raises(AssertionError, match=r"requires at least one|leading rejected_mutation"):
        _merged_component_rejection_result(results, components_withheld=0)


def test_component_merge_retains_order_attribution_and_independent_metadata() -> None:
    first = replace(
        _failure_result(_empty_state(), "first", error_code="first_code", rejected_component="source:input", with_state_validation=False),
        data={"credential_fields": ["api_key"], "repair": {"instruction": "wire_secret_ref"}},
    )
    second = _failure_result(_empty_state(), "second", error_code="second_code", rejected_component="node:llm", with_state_validation=False)
    merged = _merged_component_rejection_result([first, second], components_withheld=2)
    assert merged.validation.errors == (*first.validation.errors, *second.validation.errors)
    assert merged.to_dict()["data"] == {
        "credential_fields": ["api_key"],
        "repair": {"instruction": "wire_secret_ref"},
        "components_withheld": 2,
    }


def test_generic_failure_detail_accepts_empty_errors_and_preserves_control_errors() -> None:
    result = ToolResult(
        success=False, updated_state=_empty_state(), validation=ValidationSummary(is_valid=True, errors=()), affected_nodes=()
    )
    try:
        detail = _tool_failure_detail(result.to_dict())
    except IndexError:
        pytest.fail("Generic failures with empty validation.errors must return the fallback without indexing errors")
    assert detail == "."
    assert _tool_failure_detail({"error": "bad arguments"}) == ": bad arguments"
    assert _tool_failure_detail(_failure_result(_empty_state(), "actual rejection").to_dict()) == ": actual rejection"


@pytest.mark.parametrize("component", ["rejected_mutation", "pipeline"])
def test_guided_source_detail_only_discloses_its_closed_rejection(component: str) -> None:
    result = ToolResult(
        success=False,
        updated_state=_empty_state(),
        validation=ValidationSummary(
            is_valid=False,
            errors=(ValidationEntry(component=component, message="Path violation (S2): Source file paths sentinel", severity="high"),),
        ),
        affected_nodes=(),
    )
    expected = _GUIDED_SOURCE_PATH_ALLOWLIST_DETAIL if component == "rejected_mutation" else "Step 1 source commit failed"
    assert _guided_source_commit_failure_detail(result) == expected
    assert _guided_source_commit_failure_detail(replace(result, success=True)) == "Step 1 source commit failed"
    assert (
        _guided_source_commit_failure_detail(replace(result, validation=ValidationSummary(is_valid=True, errors=())))
        == "Step 1 source commit failed"
    )
