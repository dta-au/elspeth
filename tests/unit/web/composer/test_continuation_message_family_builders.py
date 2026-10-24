"""Complete native wire and current-policy failure-schema producer boundaries."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PromptGuardContentPart,
    PromptGuardToolArgumentParts,
    PromptGuardTrust,
)
from elspeth.contracts.token_usage import TokenUsage
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedLLMProviderMetadata,
    _AdmittedToolBatch,
    _AdmittedToolCall,
    _AdmittedToolFunction,
)
from elspeth.web.composer.continuation_codec import decode_continuation_response, encode_continuation_response, encode_private_chunk
from elspeth.web.composer.continuation_message_family_builders import (
    capture_failure_schema_recipe,
    capture_native_message_recipe,
    failure_schema_message,
    family_message_wire,
    native_assistant_message,
    readmit_failure_schema_result,
    rebuild_native_message,
    require_native_message_refresh,
    retained_family_wire,
)
from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json
from elspeth.web.composer.discovery_cache import serialize_tool_result, tool_result_content_parts
from elspeth.web.composer.prompt_guard_rendering import json_argument_content_parts
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


def completion_with_native(*, calls=None):
    if calls is None:
        calls = (
            _AdmittedToolCall("first-call", _AdmittedToolFunction("list_sources", '{"note": "é", "ordered": [2,1]}')),
            _AdmittedToolCall("second-call", _AdmittedToolFunction("list_sinks", '{"note":"second"}')),
        )
    return _AdmittedLLMCompletion(
        _AdmittedAssistantMessage("provider prose"),
        _AdmittedToolBatch(calls, frozenset(call.id for call in calls)),
        _AdmittedLLMProviderMetadata(
            "native-family-model",
            "tool_calls",
            TokenUsage(prompt_tokens=11, completion_tokens=7),
            None,
            "not_available",
            "native-family-request",
            None,
            None,
            [
                {"type": "thinking", "thinking": "é thought " + "z" * 17001, "signature": "opaque-signature"},
                {"type": "redacted_thinking", "data": "opaque-redacted-data"},
            ],
            "native-family-provider",
        ),
    )


def failure_case(*, view=None, state=None, user_id=None):
    if view is None:
        catalog = create_catalog_service()
        availability = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        view = PolicyCatalogView.for_trained_operator(catalog, availability)
    availability = view.snapshot
    if state is None:
        state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    arguments = {
        "source": {
            "plugin": "csv",
            "on_success": "main",
            "on_validation_failure": "discard",
            "options": {"schema": {"mode": "observed"}},
        },
        "nodes": [],
        "edges": [],
        "outputs": [{"sink_name": "main", "plugin": "json", "options": {"schema": {"mode": "observed"}}, "on_write_failure": "discard"}],
    }
    result = execute_tool("set_pipeline", arguments, state, view, plugin_snapshot=availability, user_id=user_id, validate_arguments=True)
    assert result.success is False and result.updated_state is state and result.plugin_schemas
    # A state may also have ordinary validation errors without a plugin stamp.
    # The actual augmentation reads only identities stamped by the validator.
    assert any(entry.plugin_identity == ("source", "csv") for entry in result.validation.errors)
    call = _AdmittedToolCall("failed-call", _AdmittedToolFunction("set_pipeline", canonical_json({"pipeline": arguments})))
    completion = completion_with_native(calls=(call,))
    response = encode_continuation_response(completion)
    serialized = serialize_tool_result(result)
    message = {
        "role": "tool",
        "tool_call_id": call.id,
        "content": serialized,
        PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(result, serialized),
    }
    return call, result, view, response, message


@pytest.mark.parametrize("empty_blocks", (False, True))
def test_legacy_non_native_response_bytes_are_an_exact_inverse(empty_blocks):
    # Literal old complete wire, independent of the new retained DTO dump.
    legacy = (
        '{"advisor_result_metadata":null,"audit_call_json":null,"calls":[],"content":"legacy non-native response",'
        '"metadata":{"finish_reason":"stop","model_returned":"legacy-model","provider_cost":null,'
        '"provider_cost_source":"not_available","provider_request_id":"legacy-provider-request","provider_served":null,'
        '"reasoning_content":null,"reasoning_details":null,"thinking_blocks":null,"usage":{"cache_creation_input_tokens":null,'
        '"cache_read_input_tokens":null,"cached_prompt_tokens":null,"completion_tokens":2,"prompt_tokens":1,'
        '"reasoning_tokens":null,"reported_total":3}},"schema_version":"composer.response.v1"}'
    )
    if empty_blocks:
        value = json.loads(legacy)
        value["metadata"]["thinking_blocks"] = []
        legacy = canonical_json(value)
    old = encode_private_chunk(codec="composer.response.v1", canonical=legacy)
    decoded = decode_continuation_response(old)
    assert encode_continuation_response(decoded) == old
    value = json.loads(old.canonical)
    assert "native_thinking_json" not in value
    assert "audit_call_json" in value and value["audit_call_json"] is None
    assert "advisor_result_metadata" in value and value["advisor_result_metadata"] is None


def test_native_response_refuses_a_changed_ordered_source_after_complete_inverse():
    completion = completion_with_native()
    response = encode_continuation_response(completion)
    assert encode_continuation_response(decode_continuation_response(response)) == response
    value = json.loads(response.canonical)
    blocks = json.loads(value["native_thinking_json"])
    blocks[0]["signature"] += "changed"
    value["native_thinking_json"] = json.dumps(blocks, ensure_ascii=False, separators=(",", ":"))
    changed = encode_private_chunk(codec="composer.response.v1", canonical=canonical_json(value))
    with pytest.raises(AuditIntegrityError, match="complete response metadata"):
        decode_continuation_response(changed)


def test_native_rebuild_retains_original_order_complete_tail_and_exact_untrusted_partitions():
    completion = completion_with_native()
    message = native_assistant_message(completion)
    response = encode_continuation_response(completion)
    decoded = decode_continuation_response(response)
    assert list(message["thinking_blocks"][0]) == ["type", "thinking", "signature"]
    assert list(decoded.provider_metadata.thinking_blocks[0]) == list(message["thinking_blocks"][0])
    # Actual argument admission can rewrite only the assistant arguments.
    message["tool_calls"][0]["function"]["arguments"] = '{"custody_safe": "é", "ordered": [2,1]}'
    message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (
        PromptGuardToolArgumentParts(0, json_argument_content_parts(message["tool_calls"][0]["function"]["arguments"])),
    )
    recipe = capture_native_message_recipe(response=response, completion=completion, message=message)
    text = family_message_wire(message)
    actual = rebuild_native_message(response=response, recipe=recipe, selected_message_json=text)
    assert family_message_wire(actual) == text
    assert actual["thinking_blocks"] == message["thinking_blocks"]
    assert list(actual["thinking_blocks"][0]) == ["type", "thinking", "signature"]
    assert actual[PROMPT_GUARD_THINKING_PARTS_KEY] == message[PROMPT_GUARD_THINKING_PARTS_KEY]
    assert actual[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] == message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY]
    assert recipe.argument_indexes == (0,)
    assert all(part.trust is None for part in actual[PROMPT_GUARD_THINKING_PARTS_KEY].parts)
    assert "opaque-signature" in "".join(part.inspection_text or part.text for part in actual[PROMPT_GUARD_THINKING_PARTS_KEY].parts)
    assert "z" * 8000 in "".join(part.inspection_text or part.text for part in actual[PROMPT_GUARD_THINKING_PARTS_KEY].parts)


@pytest.mark.parametrize("change", ("native_tail", "content", "call_order", "call_name"))
def test_native_source_refuses_one_changed_fact_after_complete_positive(change):
    completion = completion_with_native()
    response = encode_continuation_response(completion)
    message = native_assistant_message(completion)
    recipe = capture_native_message_recipe(response=response, completion=completion, message=message)
    text = family_message_wire(message)
    assert family_message_wire(rebuild_native_message(response=response, recipe=recipe, selected_message_json=text)) == text
    wire = retained_family_wire(text)
    if change == "native_tail":
        wire["thinking_blocks"][-1]["data"] += "changed"
    elif change == "content":
        wire["content"] += "changed"
    elif change == "call_order":
        wire["tool_calls"].reverse()
    else:
        wire["tool_calls"][0]["function"]["name"] = "get_plugin_schema"
    with pytest.raises(AuditIntegrityError):
        rebuild_native_message(response=response, recipe=recipe, selected_message_json=encode_transcript_object_json(wire))


def test_native_refresh_preserves_only_actual_argument_rewrite_and_monotonic_positions():
    completion = completion_with_native()
    response = encode_continuation_response(completion)
    message = native_assistant_message(completion)
    previous = capture_native_message_recipe(response=response, completion=completion, message=message)
    original = family_message_wire(message)
    message["tool_calls"][0]["function"]["arguments"] = '{"redacted":"actual custody projection"}'
    message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (
        PromptGuardToolArgumentParts(0, json_argument_content_parts(message["tool_calls"][0]["function"]["arguments"])),
    )
    recipe = capture_native_message_recipe(response=response, completion=completion, message=message)
    require_native_message_refresh(previous=previous, previous_wire=original, recipe=recipe, message=message)
    message.pop(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY)
    # The actual rewritten bytes cannot be captured without their admitted
    # position. This is now refused at the earlier source boundary.
    with pytest.raises(AuditIntegrityError, match="unadmitted original argument bytes"):
        capture_native_message_recipe(response=response, completion=completion, message=message)
    # Independently retain the exact monotonic-refresh oracle: even a forged
    # nominal historical recipe cannot remove the previous admitted position.
    removed = recipe.model_copy(update={"argument_indexes": ()})
    with pytest.raises(AuditIntegrityError, match="removed an admitted argument"):
        require_native_message_refresh(previous=recipe, previous_wire=family_message_wire(message), recipe=removed, message=message)


def test_native_full_field_credential_scan_remains_before_reconstruction():
    completion = completion_with_native()
    response = encode_continuation_response(completion)
    message = native_assistant_message(completion)
    recipe = capture_native_message_recipe(response=response, completion=completion, message=message)
    text = family_message_wire(message)
    assert rebuild_native_message(response=response, recipe=recipe, selected_message_json=text)
    wire = retained_family_wire(text)
    wire["thinking_blocks"][0]["signature"] = "https://" + "name:synthetic-private-value@example.test/"
    with pytest.raises(CredentialMaterialRefused):
        rebuild_native_message(response=response, recipe=recipe, selected_message_json=encode_transcript_object_json(wire))


def test_native_capture_refuses_incomplete_argument_partition_after_actual_positive():
    completion = completion_with_native()
    response = encode_continuation_response(completion)
    message = native_assistant_message(completion)
    message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (
        PromptGuardToolArgumentParts(0, json_argument_content_parts(message["tool_calls"][0]["function"]["arguments"])),
    )
    assert capture_native_message_recipe(response=response, completion=completion, message=message).argument_indexes == (0,)
    message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (
        PromptGuardToolArgumentParts(0, json_argument_content_parts('{"other_untrusted_value":1}')),
    )
    with pytest.raises(AuditIntegrityError, match="does not cover the complete wire arguments"):
        capture_native_message_recipe(response=response, completion=completion, message=message)


def test_native_capture_refuses_wrong_argument_partition_after_actual_positive():
    completion = completion_with_native()
    response = encode_continuation_response(completion)
    message = native_assistant_message(completion)
    arguments = message["tool_calls"][0]["function"]["arguments"]
    semantic_parts = json_argument_content_parts(arguments)
    message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (PromptGuardToolArgumentParts(0, semantic_parts),)
    assert capture_native_message_recipe(response=response, completion=completion, message=message).argument_indexes == (0,)
    actual_wire = family_message_wire(message)
    # Same complete arguments and nominal wholly untrusted parts. Detachment
    # admits both; only the actual producer's exact semantic partition differs.
    different_parts = (PromptGuardContentPart(arguments),)
    assert different_parts != semantic_parts
    assert "".join(part.text for part in different_parts) == arguments
    assert all(part.trust is None for part in different_parts)
    message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (PromptGuardToolArgumentParts(0, different_parts),)
    assert family_message_wire(message) == actual_wire
    with pytest.raises(AuditIntegrityError, match="argument provenance"):
        capture_native_message_recipe(response=response, completion=completion, message=message)


def test_actual_failure_augmentation_rebuilds_only_current_reference_and_preserves_owned_state_validation():
    call, result, view, response, message = failure_case()
    rebuilt = readmit_failure_schema_result(call=call, result=result, catalog=view)
    assert rebuilt.updated_state is result.updated_state and rebuilt.validation is result.validation
    assert rebuilt.affected_nodes is result.affected_nodes
    actual = failure_schema_message(call=call, result=result, catalog=view)
    assert family_message_wire(actual) == family_message_wire(message)
    assert actual[PROMPT_GUARD_CONTENT_PARTS_KEY] == message[PROMPT_GUARD_CONTENT_PARTS_KEY]
    assert any(part.trust is PromptGuardTrust.REFERENCE for part in actual[PROMPT_GUARD_CONTENT_PARTS_KEY])
    recipe = capture_failure_schema_recipe(response=response, call=call, result=result, catalog=view, message=message)
    assert recipe.response == response.reference and recipe.tool_call_id == call.id


def test_failure_reference_cannot_be_recovered_from_unstamped_message_text_after_positive():
    call, result, view, _response, _message = failure_case()
    assert readmit_failure_schema_result(call=call, result=result, catalog=view).plugin_schemas
    changed = replace(
        result,
        validation=replace(result.validation, errors=tuple(replace(entry, plugin_identity=None) for entry in result.validation.errors)),
    )
    with pytest.raises(AuditIntegrityError, match="actual current policy"):
        readmit_failure_schema_result(call=call, result=changed, catalog=view)


def test_changed_saved_schema_cannot_supply_its_own_reference_after_positive():
    call, result, view, _response, _message = failure_case()
    assert readmit_failure_schema_result(call=call, result=result, catalog=view).plugin_schemas
    changed_schemas = {key: {**value, "description": "changed saved schema"} for key, value in result.plugin_schemas.items()}
    changed = replace(result, plugin_schemas=changed_schemas)
    with pytest.raises(AuditIntegrityError, match="actual current policy"):
        readmit_failure_schema_result(call=call, result=changed, catalog=view)
