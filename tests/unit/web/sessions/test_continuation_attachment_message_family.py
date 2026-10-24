"""Actual retained read result and current selected SQL/message reconstruction.

The fixture deliberately enters at the completed-dispatch input boundary. It
uses the real attachment reader, actual audit finisher and owned result graph;
it does not claim the atomic attachment finisher or a paid provider attempt.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import insert

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
)
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.continuation_attachment_read import read_current_attachment
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_codec import encode_continuation_response
from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
from elspeth.web.composer.continuation_message_family_builders import (
    attachment_result_message,
    capture_attachment_result_recipe,
    family_message_wire,
    retained_family_wire,
)
from elspeth.web.composer.continuation_message_family_recipes import AttachmentResultMessageRecipe
from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json
from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
from elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer.test_continuation_message_family_builders import completion_with_native
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_message_families import restore_case, save_family_cut
from tests.unit.web.sessions.test_continuation_prompt_producers import producer_case

operation_store = test_composer_operation_authority.operation_store


async def attachment_family_case(store, *, tool_name="read_attachment", success=True):
    kwargs, evidence, recorder, snapshot, iteration, _actors = await producer_case(store)
    produced = await load_current_message_producers(**kwargs, initial=True)
    messages = list(produced.messages)
    state = produced.producers.inputs.values.initial_state
    raw = ("ordinary customer prose é " + "x" * 2000).encode()
    blob_id = str(uuid4())
    sessions, sid = store[3], store[4]
    path = sessions._data_dir / "blobs" / str(sid) / f"{blob_id}_notes.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with store[0].begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=blob_id,
                session_id=str(sid),
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=len(raw),
                content_hash=sha256(raw).hexdigest(),
                status="ready",
                created_by="user",
                creation_modality="verbatim",
                storage_path=str(path),
                created_at=datetime.now(UTC),
            )
        )
    arguments = {"blob_id": blob_id if success else str(uuid4())}
    if tool_name == "get_blob_content":
        arguments.update(offset_chars=1, expected_content_hash=sha256(raw).hexdigest())
    actual = await read_current_attachment(**kwargs, tool_name=tool_name, arguments=arguments, state=state)
    assert actual.result.success is success and actual.result.updated_state is state
    call = _AdmittedToolCall("actual-attachment-message", _AdmittedToolFunction(tool_name, canonical_json(arguments)))
    response = encode_continuation_response(completion_with_native(calls=(call,)))
    message = attachment_result_message(call=call, result=actual.result)
    produced.producers.register_attachment_message(response=response, call=call, result=actual.result, message=message)
    decoded = decode_wire_arguments(tool_name, ToolContractDialect.NONE, arguments)
    semantic = deep_thaw(decoded.semantic)
    assert type(semantic) is dict
    authoring = PrivateToolAuthoring(call.id, tool_name, canonical_json(semantic), None, call.function.arguments)
    invocation = finish_success(
        begin_dispatch(
            call.id,
            tool_name,
            semantic,
            version_before=state.version,
            actor="composer",
            strict_sent=None,
            wire_conformant=decoded.wire_conformant,
        ),
        result_payload=actual.result.to_dict(),
        version_after=state.version,
    )
    recorder.record_private_tool_authoring(authoring)
    recorder.record(invocation)
    messages.append(message)
    evidence.transcript.observe_messages(messages)
    shelf = evidence.transcript.snapshot_for_continuation(messages, materialization=produced.producers)
    batch = ContinuationBatchState(
        state,
        actual.result.validation,
        None,
        0,
        entry_state=state,
        phase="dispatch_ready",
        next_tool_index=1,
        turn_has_discovery=True,
        all_cache_hits=False,
        tool_outcomes=[_ToolOutcome(call, actual.result, None, None, None, state.version, state.version, None, decoded.wire_conformant)],
        decoded_args_by_call_id={call.id: semantic},
        private_authorings_by_call={call.id: authoring},
    )
    case = {
        "kwargs": kwargs,
        "snapshot": snapshot,
        "shelf": shelf,
        "response": response,
        "data": RestoredBatchData(batch, BatchAccumulator(state, None, None, 0), (call,)),
        "iteration": iteration.model_copy(update={"producer_inputs": produced.producers.snapshot_inputs()}),
        "audit": recorder.snapshot_for_continuation(drained=RecorderDrainCursors()),
        "original_messages": messages,
        "call": call,
        "result": actual.result,
        "retained_read": actual.retained,
        "path": path,
    }
    kwargs["required_work"].coordinator.assert_completed()
    return await save_family_cut(store, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("read_attachment", "get_blob_content"))
@pytest.mark.parametrize("success", (False, True))
async def test_current_attachment_family_restores_exact_original_wire_and_guard_without_new_read(
    operation_store, monkeypatch, tool_name, success
):
    case = await attachment_family_case(operation_store, tool_name=tool_name, success=success)
    try:
        original = detach_prompt_guard_evidence(case["original_messages"])
        actual = await restore_case(operation_store, case)
        assert detach_prompt_guard_evidence(actual.messages) == original
        assert type(case["shelf"].producer_recipes[case["shelf"].current_indexes[-1]]) is AttachmentResultMessageRecipe
        if success:
            # Historical message reconstruction is not another file inspection.
            # The original selected result remains its source, not current bytes.
            case["path"].unlink()
        from elspeth.web.composer import continuation_attachment_read

        def forbidden(**_kw):
            raise AssertionError("historical message replayed the attachment reader")

        monkeypatch.setattr(continuation_attachment_read, "read_current_attachment", forbidden)
        repeated = await restore_case(operation_store, case)
        assert detach_prompt_guard_evidence(repeated.messages) == original
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("trust", "inspection", "wire", "call"))
async def test_attachment_recipe_refuses_one_changed_original_producer_fact_after_positive(operation_store, change):
    case = await attachment_family_case(operation_store)
    try:
        message = dict(case["original_messages"][-1])
        kwargs = {"response": case["response"], "call": case["call"], "result": case["result"], "message": message}
        assert capture_attachment_result_recipe(**kwargs) == case["shelf"].producer_recipes[case["shelf"].current_indexes[-1]]
        if change in ("trust", "inspection"):
            parts = message[PROMPT_GUARD_CONTENT_PARTS_KEY]
            assert parts
            changed = (
                replace(parts[0], trust=PromptGuardTrust.REFERENCE)
                if change == "trust"
                else replace(parts[0], inspection_text="different inspection")
            )
            message[PROMPT_GUARD_CONTENT_PARTS_KEY] = (changed, *parts[1:])
            assert family_message_wire(message) == family_message_wire(case["original_messages"][-1])
        elif change == "wire":
            message["content"] += " "
        else:
            kwargs["call"] = replace(case["call"], function=replace(case["call"].function, arguments=case["call"].function.arguments + " "))
        with pytest.raises(AuditIntegrityError):
            capture_attachment_result_recipe(**kwargs)
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_current_attachment_family_refuses_unselected_wire_after_real_positive(operation_store):
    case = await attachment_family_case(operation_store)
    try:
        await restore_case(operation_store, case)
        values = case["kwargs"]
        owner = CurrentContinuationMessageFamilies(values["service"], values["authority"], values["selected"], values["inventory"])
        recipe = case["shelf"].producer_recipes[case["shelf"].current_indexes[-1]]
        original = case["shelf"].messages_json[case["shelf"].current_indexes[-1]]
        assert (
            family_message_wire(await asyncio.to_thread(owner.attachment_message, recipe=recipe, selected_message_json=original))
            == original
        )
        wire = retained_family_wire(original)
        wire["content"] += " "
        with pytest.raises(AuditIntegrityError, match="original selected shelf wire"):
            await asyncio.to_thread(owner.attachment_message, recipe=recipe, selected_message_json=encode_transcript_object_json(wire))
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_current_attachment_family_refuses_another_call_locator_after_real_positive(operation_store):
    case = await attachment_family_case(operation_store)
    try:
        await restore_case(operation_store, case)
        values = case["kwargs"]
        owner = CurrentContinuationMessageFamilies(values["service"], values["authority"], values["selected"], values["inventory"])
        recipe = case["shelf"].producer_recipes[case["shelf"].current_indexes[-1]]
        original = case["shelf"].messages_json[case["shelf"].current_indexes[-1]]
        assert (
            family_message_wire(await asyncio.to_thread(owner.attachment_message, recipe=recipe, selected_message_json=original))
            == original
        )
        with pytest.raises(AuditIntegrityError, match="no actual selected completed read"):
            await asyncio.to_thread(
                owner.attachment_message,
                recipe=recipe.model_copy(update={"tool_call_id": "another-issued-call"}),
                selected_message_json=original,
            )
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)
