"""Actual B2 ordinary/native message ownership and current SQL reconstruction."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    detach_prompt_guard_evidence,
)
from elspeth.web.composer.continuation_codec import encode_continuation_response
from elspeth.web.composer.continuation_message_family_builders import family_message_wire
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_attachment_application import application_case

operation_store = operations.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("native", (False, True))
async def test_real_b2_registers_same_assistant_and_current_sql_restores_exact_guard_positions(
    operation_store, tmp_path, monkeypatch, native
):
    arguments, runtime, ctx, outcome, source, original_audit, sdk, _path = await application_case(
        operation_store, tmp_path, monkeypatch, native=native
    )
    running = arguments["inventory"].running
    try:
        batch = runtime.graph.batch.batch
        index = batch.assistant_message_index
        assert index is not None and batch.current_call.audit is original_audit
        message = runtime.live.llm_messages[index]
        assert ctx.message_producers is runtime.materialization
        wire, recipe = runtime.materialization.capture_message(message)
        assert recipe.kind == ("native_thinking" if native else "tool_call_arguments")
        assert recipe.argument_indexes == (0,) and len(sdk) == 1
        response = encode_continuation_response(outcome.completion, audit_call=outcome.audit_call)
        assert recipe.response == response.reference
        assert (
            runtime.materialization.register_tool_call_message(
                response=response,
                completion=outcome.completion,
                message=message,
                tool_call_id=outcome.completion.tool_batch.calls[0].id,
            )
            is message
        )
        assert runtime.live.llm_messages[index] is message
        cold = await restore_continuation_runtime(**arguments)
        assert cold.live.llm_messages is cold.graph.messages
        assert cold.live.recorder is cold.graph.recorder
        assert cold.live.turn_evidence is cold.graph.evidence
        cold_message = cold.live.llm_messages[cold.graph.batch.batch.assistant_message_index]
        assert family_message_wire(cold_message) == wire
        assert cold_message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] == message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY]
        assert detach_prompt_guard_evidence([cold_message]) == detach_prompt_guard_evidence([message])
        assert cold.materialization.capture_message(cold_message) == (wire, recipe)
        assert cold.graph.batch.batch.current_call.audit is None
        assert cold.graph.selected_audit is not None and source.read_audit is not None
        assert cold.live.recorder.llm_calls == runtime.live.recorder.llm_calls
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("same_wire_copy", "guard_value", "source", "prose"))
async def test_real_ordinary_registry_refuses_changed_alias_guard_source_or_nonargument_wire(
    operation_store, tmp_path, monkeypatch, change
):
    arguments, runtime, _ctx, outcome, _source, _audit, _sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    batch = runtime.graph.batch.batch
    message = runtime.live.llm_messages[batch.assistant_message_index]
    original_content = message["content"]
    original_parts = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY]
    try:
        wire, recipe = runtime.materialization.capture_message(message)
        assert recipe.kind == "tool_call_arguments" and recipe.argument_indexes == (0,)
        response = encode_continuation_response(outcome.completion, audit_call=outcome.audit_call)
        assert (
            runtime.materialization.register_tool_call_message(
                response=response,
                completion=outcome.completion,
                message=message,
                tool_call_id=outcome.completion.tool_batch.calls[0].id,
            )
            is message
        )
        if change == "guard_value":
            entry = original_parts[0]
            changed_part = replace(entry.parts[0], inspection_text="different inspection value")
            message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (replace(entry, parts=(changed_part, *entry.parts[1:])), *original_parts[1:])
            # The complete marker-free wire is identical. Provenance is not.
            assert family_message_wire(message) == wire
            with pytest.raises(AuditIntegrityError):
                runtime.materialization.capture_message(message)
        else:
            changed = dict(message) if change == "same_wire_copy" else message
            if change == "prose":
                changed["content"] += " changed"
            elif change == "source":
                response = encode_continuation_response(
                    replace(outcome.completion, message=replace(outcome.completion.message, content="different original response")),
                    audit_call=outcome.audit_call,
                )
            with pytest.raises(AuditIntegrityError):
                runtime.materialization.register_tool_call_message(
                    response=response,
                    completion=outcome.completion,
                    message=changed,
                    tool_call_id=outcome.completion.tool_batch.calls[0].id,
                )
    finally:
        message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = original_parts
        message["content"] = original_content
        arguments["required_work"].coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)
