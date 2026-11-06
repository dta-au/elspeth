"""Exact ordinary/native source, partial guard positions and one-call refresh."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
)
from elspeth.web.composer.continuation_codec import encode_continuation_response
from elspeth.web.composer.continuation_message_family_builders import family_message_wire, native_assistant_message
from elspeth.web.composer.continuation_message_family_recipes import NativeThinkingMessageRecipe, ToolCallArgumentsMessageRecipe
from elspeth.web.composer.continuation_tool_argument_messages import (
    capture_tool_call_message_recipe,
    rebuild_tool_argument_message,
    require_tool_call_message_refresh,
)
from elspeth.web.composer.tool_batch import _retain_llm_tool_argument_parts
from tests.unit.web.composer.test_continuation_message_family_builders import completion_with_native


def actual_case(native):
    completion = completion_with_native()
    if not native:
        completion = replace(completion, provider_metadata=replace(completion.provider_metadata, thinking_blocks=None))
    return completion, encode_continuation_response(completion), native_assistant_message(completion)


@pytest.mark.parametrize("native", (False, True))
def test_same_actual_message_retains_only_admitted_positions_and_each_original_alias(native):
    completion, response, message = actual_case(native)
    messages = [message, message]
    initial = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    assert type(initial) is (NativeThinkingMessageRecipe if native else ToolCallArgumentsMessageRecipe)
    assert initial.argument_indexes == ()
    initial_wire = family_message_wire(message)
    require_tool_call_message_refresh(
        previous=None, previous_wire=None, recipe=initial, completion=completion, message=message, tool_call_id=None
    )
    _retain_llm_tool_argument_parts(messages, tool_call_id=completion.tool_batch.calls[0].id)
    first = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    require_tool_call_message_refresh(
        previous=initial,
        previous_wire=initial_wire,
        recipe=first,
        completion=completion,
        message=message,
        tool_call_id=completion.tool_batch.calls[0].id,
    )
    assert messages[0] is messages[1] is message and first.argument_indexes == (0,)
    parts = detach_prompt_guard_evidence([message]).tool_argument_parts[0]
    assert parts is not None and len(parts) == 1 and parts[0].tool_call_index == 0
    assert all(part.trust is None for part in parts[0].parts)
    if not native:
        rebuilt = rebuild_tool_argument_message(response=response, recipe=first, selected_message_json=family_message_wire(message))
        assert family_message_wire(rebuilt) == family_message_wire(message)
        assert rebuilt[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] == message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY]
    first_wire = family_message_wire(message)
    _retain_llm_tool_argument_parts(messages, tool_call_id=completion.tool_batch.calls[1].id)
    second = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    require_tool_call_message_refresh(
        previous=first,
        previous_wire=first_wire,
        recipe=second,
        completion=completion,
        message=message,
        tool_call_id=completion.tool_batch.calls[1].id,
    )
    assert second.argument_indexes == (0, 1)


@pytest.mark.parametrize("change", ("prose", "identity", "order", "source", "trust", "native"))
def test_ordinary_producer_refuses_source_or_guard_change_after_real_positive(change):
    completion, response, message = actual_case(False)
    normal = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    assert type(normal) is ToolCallArgumentsMessageRecipe
    if change == "prose":
        message["content"] += " changed"
    elif change == "identity":
        message["tool_calls"][0]["id"] += "-other"
    elif change == "order":
        message["tool_calls"].reverse()
    elif change == "source":
        response = encode_continuation_response(replace(completion, message=replace(completion.message, content="other")))
    elif change == "trust":
        message[PROMPT_GUARD_TRUST_KEY] = PromptGuardTrust.INSTRUCTION
    else:
        message["thinking_blocks"] = [{"type": "thinking", "thinking": "not received", "signature": "foreign"}]
    with pytest.raises(AuditIntegrityError):
        capture_tool_call_message_recipe(response=response, completion=completion, message=message)


@pytest.mark.parametrize("change", ("other_argument", "drop_position", "wrong_call", "new_source"))
def test_refresh_refuses_changes_outside_the_one_actual_admitted_call(change):
    completion, response, message = actual_case(False)
    previous = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    previous_wire = family_message_wire(message)
    _retain_llm_tool_argument_parts([message], tool_call_id=completion.tool_batch.calls[0].id)
    current = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    require_tool_call_message_refresh(
        previous=previous,
        previous_wire=previous_wire,
        recipe=current,
        completion=completion,
        message=message,
        tool_call_id=completion.tool_batch.calls[0].id,
    )
    if change == "other_argument":
        message["tool_calls"][1]["function"]["arguments"] = '{"changed":true}'
    elif change == "drop_position":
        previous, previous_wire = current, family_message_wire(message)
        del message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY]
        current = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    elif change == "new_source":
        other = encode_continuation_response(replace(completion, message=replace(completion.message, content="other")))
        current = current.model_copy(update={"response": other.reference})
    call_id = "absent" if change == "wrong_call" else completion.tool_batch.calls[0].id
    with pytest.raises(AuditIntegrityError):
        require_tool_call_message_refresh(
            previous=previous, previous_wire=previous_wire, recipe=current, completion=completion, message=message, tool_call_id=call_id
        )


@pytest.mark.parametrize("native", (False, True))
@pytest.mark.parametrize("partial", (False, True))
@pytest.mark.parametrize("boundary", ("cold", "capture"))
def test_unadmitted_original_argument_bytes_survive_initial_and_partial_history(native, partial, boundary):
    from elspeth.web.composer.continuation_message_family_builders import rebuild_native_message, retained_family_wire
    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    completion, response, message = actual_case(native)
    if partial:
        # Preserve actual supported custody rewriting on the admitted first
        # call while the original second call remains unadmitted.
        message["tool_calls"][0]["function"]["arguments"] = '{"custody_safe":"actual admitted replacement"}'
        _retain_llm_tool_argument_parts([message], tool_call_id=completion.tool_batch.calls[0].id)
    recipe = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    assert recipe.argument_indexes == ((0,) if partial else ())
    original_wire = family_message_wire(message)
    if native:
        restored = rebuild_native_message(response=response, recipe=recipe, selected_message_json=original_wire)
    else:
        restored = rebuild_tool_argument_message(response=response, recipe=recipe, selected_message_json=original_wire)
    assert family_message_wire(restored) == original_wire
    assert restored.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY) == message.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY)
    assert restored["tool_calls"][1]["function"]["arguments"] == completion.tool_batch.calls[1].function.arguments
    changed = '{"changed_unadmitted":"ordinary source value"}'
    if boundary == "capture":
        message["tool_calls"][1]["function"]["arguments"] = changed
        with pytest.raises(AuditIntegrityError, match="unadmitted original argument bytes"):
            capture_tool_call_message_recipe(response=response, completion=completion, message=message)
    else:
        wire = retained_family_wire(original_wire)
        wire["tool_calls"][1]["function"]["arguments"] = changed
        changed_wire = encode_transcript_object_json(wire)
        # This is complete valid ordered wire and the exact original source
        # recipe; no malformed JSON or fake current authority earns refusal.
        with pytest.raises(AuditIntegrityError, match="unadmitted original argument bytes"):
            if native:
                rebuild_native_message(response=response, recipe=recipe, selected_message_json=changed_wire)
            else:
                rebuild_tool_argument_message(response=response, recipe=recipe, selected_message_json=changed_wire)
