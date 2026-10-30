"""Real current SQL/shelf joins for native and failed-result message families.

Native inputs come from a real paid publication: only the SDK and guard HTTP
are controlled, so the recipe names a committed provider effect. The failed
result is a controlled completed-result input from the actual mutation
validator; it does not claim worker acceptance.
"""

import asyncio
import json
from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_THINKING_PARTS_KEY, detach_prompt_guard_evidence
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.composer._compose_loop_carriers import _ToolOutcome
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, decode_recorder_snapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, encode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import (
    encode_continuation_response,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import admit_turn_evidence, decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_iteration import encode_continuation_iteration
from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
from elspeth.web.composer.continuation_message_family_builders import (
    native_assistant_message,
    retained_family_wire,
)
from elspeth.web.composer.continuation_message_family_recipes import NativeThinkingMessageRecipe
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, encode_message_shelf
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_continuation_message_family_builders import failure_case
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import producer_case
from tests.unit.web.sessions.test_continuation_runtime import install_capture
from tests.unit.web.sessions.test_continuation_runtime_provider_history import latest, prepared_runtime, publish

operation_store = test_composer_operation_authority.operation_store


async def save_family_cut(store, case):
    engine, _repository, authority, sessions, sid = store
    kwargs = case["kwargs"]
    selected, inventory = kwargs["selected"], kwargs["inventory"]
    running = inventory.running
    shelf, response, data = case["shelf"], case["response"], case["data"]
    audit = encode_recorder_snapshot(case["audit"])
    snapshot = case["snapshot"].model_copy(update={"transcript": shelf, "recorder": audit.reference})
    evidence = encode_turn_evidence(
        admit_turn_evidence(snapshot, env_ref_names=inventory.names, source_chunks=(audit, *selected.chunks)), env_ref_names=inventory.names
    )
    transcript = encode_message_shelf(shelf)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json(
            {
                "messages_json": shelf.messages_json,
                "current_indexes": shelf.current_indexes,
            }
        ),
    )
    iteration = encode_continuation_iteration(case["iteration"])
    batch = encode_coupled_batch_chunk(batch=data.batch, accumulator=data.accumulator, calls=data.calls)
    frames = list(selected.stack.frames)
    indexes = tuple(index for index, frame in enumerate(frames) if type(frame) is ComposeFrame)
    assert len(indexes) == 1 and len(frames) in (1, 2)
    index = indexes[0]
    frames[index] = frames[index].model_copy(
        update={
            "evidence": evidence.reference,
            "audit_buffer": audit.reference,
            "transcript": transcript.reference,
            "messages": messages.reference,
            "iteration": iteration.reference,
        }
    )
    top = ToolBatchFrame(
        phase="tool_ready",
        response=response.reference,
        accumulator=batch.reference,
        next_tool_index=data.batch.next_tool_index,
        settled_effect_ids=(),
    )
    if len(frames) == 1:
        frames.append(top)
    else:
        assert type(frames[-1]) is ToolBatchFrame
        frames[-1] = top
    root = selected.stack.model_copy(update={"frames": tuple(frames)})
    with locked_session_transaction(engine, str(sid)) as conn:
        for chunk in (audit, evidence, transcript, messages, iteration, response, batch):
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        stored = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"actual message family input"}'),),
            handoff=False,
        )
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work
    )
    work.coordinator.assert_completed()
    case["kwargs"] = {**kwargs, "selected": selected, "inventory": inventory, "required_work": inventory_work(running)}
    return case


NATIVE_TOOL_CALLS = [
    {"id": "first-call", "type": "function", "function": {"name": "list_sources", "arguments": '{"note": "é", "ordered": [2,1]}'}},
    {"id": "second-call", "type": "function", "function": {"name": "list_sinks", "arguments": '{"note":"second"}'}},
]


async def native_case(store, tmp_path, monkeypatch):
    """A real paid native response, not a received-input fixture.

    Only the SDK and guard HTTP are controlled. Preparation, the provider
    effect and receipt, physical accounting and the runtime capture are real,
    so the native recipe names a committed paid source as production requires.
    """
    arguments, runtime, calls = await prepared_runtime(store, tmp_path, monkeypatch, tool_calls=NATIVE_TOOL_CALLS)
    published = await publish(store, arguments, runtime)
    completion = published.outcome.completion
    assert completion.provider_metadata.thinking_blocks and len(completion.tool_batch.calls) == 2
    message = native_assistant_message(completion)
    runtime.materialization.register_native_message(response=published.response, completion=completion, message=message)
    runtime.live.llm_messages.append(message)
    runtime.live.turn_evidence.transcript.observe_messages((message,))
    current = await latest(arguments, published.snapshot)
    captured = await capture_continuation_runtime_owners(runtime, selected=current["selected"], inventory=current["inventory"])
    current = await install_capture(store, current, captured)
    assert len(calls) == 1
    selected, inventory = current["selected"], current["inventory"]
    by_ref = {chunk.reference: chunk for chunk in selected.chunks}
    compose = selected.stack.frames[0]
    return {
        "kwargs": {"service": current["service"], "authority": current["authority"], "selected": selected, "inventory": inventory},
        "shelf": decode_turn_evidence(
            by_ref[compose.evidence], env_ref_names=inventory.names, source_chunks=selected.chunks
        ).snapshot.transcript,
        "recorder": decode_recorder_snapshot(by_ref[compose.audit_buffer]),
        "response": published.response,
        "completion": completion,
        "original_messages": list(runtime.live.llm_messages),
    }


async def install_changed_native_source(store, case, *, response=None, recorder=None):
    """Store one coherent change to the selected native source as a new checkpoint."""
    engine, _repository, authority, sessions, sid = store
    selected, inventory = case["kwargs"]["selected"], case["kwargs"]["inventory"]
    running = inventory.running
    by_ref = {chunk.reference: chunk for chunk in selected.chunks}
    compose = selected.stack.frames[0]
    snapshot = decode_turn_evidence(by_ref[compose.evidence], env_ref_names=inventory.names, source_chunks=selected.chunks).snapshot
    shelf, chunks, evidence_update, frame_update = case["shelf"], [], {}, {}
    if response is not None:
        shelf_data = shelf.model_dump(mode="python")
        shelf_data["producer_recipes"] = tuple(
            NativeThinkingMessageRecipe(response=response.reference, argument_indexes=recipe.argument_indexes)
            if type(recipe) is NativeThinkingMessageRecipe
            else recipe
            for recipe in shelf.producer_recipes
        )
        shelf = MessageShelfSnapshot.model_validate(shelf_data)
        chunks.append(response)
    if recorder is not None:
        audit = encode_recorder_snapshot(recorder)
        chunks.append(audit)
        evidence_update["recorder"] = audit.reference
        frame_update["audit_buffer"] = audit.reference
    changed = snapshot.model_copy(update={**evidence_update, "transcript": shelf})
    evidence = encode_turn_evidence(
        admit_turn_evidence(changed, env_ref_names=inventory.names, source_chunks=(*chunks, *selected.chunks)),
        env_ref_names=inventory.names,
    )
    transcript = encode_message_shelf(shelf)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json({"messages_json": shelf.messages_json, "current_indexes": shelf.current_indexes}),
    )
    chunks += [evidence, transcript, messages]
    frame_update.update({"evidence": evidence.reference, "transcript": transcript.reference, "messages": messages.reference})
    root = selected.stack.model_copy(update={"frames": (compose.model_copy(update=frame_update), *selected.stack.frames[1:])})
    with locked_session_transaction(engine, str(sid)) as conn:
        for chunk in chunks:
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        stored = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"one changed native source fact"}'),),
            handoff=False,
        )
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work
    )
    work.coordinator.assert_completed()
    case["kwargs"] = {**case["kwargs"], "selected": selected, "inventory": inventory}
    case["shelf"] = shelf


async def failure_current_case(store):
    kwargs, evidence, recorder, snapshot, iteration, _actors = await producer_case(store)
    produced = await producers.load_current_message_producers(**kwargs, initial=True)
    kwargs["required_work"].coordinator.assert_completed()
    messages = list(produced.messages)
    _availability, catalog = kwargs["service"]._policy_context.build(produced.producers.inputs.bootstrap.actor_user_id)
    call, result, catalog, response, message = failure_case(
        view=catalog, state=produced.producers.inputs.values.initial_state, user_id=produced.producers.inputs.bootstrap.actor_user_id
    )
    # The result comes from the actual failed mutation validator. This is the
    # controlled completed-result input boundary, not another mutation run.
    produced.producers.register_failure_schema_message(response=response, call=call, result=result, catalog=catalog, message=message)
    decoded = decode_wire_arguments(call.function.name, ToolContractDialect.NONE, json.loads(call.function.arguments))
    semantic = deep_thaw(decoded.semantic)
    assert type(semantic) is dict
    authoring = PrivateToolAuthoring(call.id, call.function.name, canonical_json(semantic), None, call.function.arguments)
    invocation = finish_success(
        begin_dispatch(
            call.id,
            call.function.name,
            semantic,
            version_before=result.updated_state.version,
            actor="composer",
            strict_sent=None,
            wire_conformant=decoded.wire_conformant,
        ),
        result_payload=result.to_dict(),
        version_after=result.updated_state.version,
    )
    recorder.record_private_tool_authoring(authoring)
    recorder.record(invocation)
    messages.append(message)
    evidence.transcript.observe_messages(messages)
    shelf = evidence.transcript.snapshot_for_continuation(messages, materialization=produced.producers)
    state = result.updated_state
    batch = ContinuationBatchState(
        state,
        result.validation,
        None,
        0,
        entry_state=state,
        phase="dispatch_ready",
        next_tool_index=1,
        tool_outcomes=[_ToolOutcome(call, result, None, None, None, state.version, state.version, None, decoded.wire_conformant)],
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
    }
    return await save_family_cut(store, case)


async def restore_case(store, case):
    before = stored_rows(store[0])
    work = inventory_work(case["kwargs"]["inventory"].running)
    try:
        actual = await producers.load_current_message_producers(**{**case["kwargs"], "required_work": work}, retained_shelf=case["shelf"])
    finally:
        work.coordinator.assert_completed()
        assert stored_rows(store[0]) == before
    return actual


@pytest.mark.asyncio
async def test_actual_selected_native_response_audit_and_shelf_restore_without_new_rows(operation_store, tmp_path, monkeypatch):
    case = await native_case(operation_store, tmp_path, monkeypatch)
    try:
        actual = await restore_case(operation_store, case)
        assert detach_prompt_guard_evidence(actual.messages).messages == detach_prompt_guard_evidence(case["original_messages"]).messages
        assert actual.messages[-1][PROMPT_GUARD_THINKING_PARTS_KEY] == case["original_messages"][-1][PROMPT_GUARD_THINKING_PARTS_KEY]
        await restore_case(operation_store, case)
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("missing_ordered", "missing_audit", "missing_recorder", "audit_metadata"))
async def test_native_current_source_refuses_one_coherent_changed_fact_after_real_positive(operation_store, tmp_path, monkeypatch, change):
    case = await native_case(operation_store, tmp_path, monkeypatch)
    try:
        await restore_case(operation_store, case)
        value = json.loads(case["response"].canonical)
        recorder = None
        if change == "missing_ordered":
            value.pop("native_thinking_json")
        elif change == "missing_audit":
            value["audit_call_json"] = None
        elif change == "missing_recorder":
            recorder = replace(case["recorder"], llm_calls=())
        else:
            changed = replace(case["recorder"].llm_calls[0], provider_request_id="other-physical-provider-request")
            recorder = replace(case["recorder"], llm_calls=(changed,))
            value["audit_call_json"] = json.loads(encode_continuation_response(case["completion"], audit_call=changed).canonical)[
                "audit_call_json"
            ]
        response = (
            None if change == "missing_recorder" else encode_private_chunk(codec="composer.response.v1", canonical=canonical_json(value))
        )
        await install_changed_native_source(operation_store, case, response=response, recorder=recorder)
        # Changing a paid response's content changes its digest, so the
        # committed provider-effect binding refuses it before any field check.
        expected = (
            "native message source differs from its actual original recorder call"
            if change == "missing_recorder"
            else "historical primary response has no unique exact provider effect"
        )
        with pytest.raises(AuditIntegrityError, match=expected):
            await restore_case(operation_store, case)
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_native_current_owner_refuses_unselected_argument_wire_after_actual_positive(operation_store, tmp_path, monkeypatch):
    case = await native_case(operation_store, tmp_path, monkeypatch)
    try:
        await restore_case(operation_store, case)
        kwargs = case["kwargs"]
        current = CurrentContinuationMessageFamilies(kwargs["service"], kwargs["authority"], kwargs["selected"], kwargs["inventory"])
        index = next(index for index, recipe in enumerate(case["shelf"].producer_recipes) if type(recipe) is NativeThinkingMessageRecipe)
        recipe = case["shelf"].producer_recipes[index]
        original = case["shelf"].messages_json[index]
        normal = await asyncio.to_thread(current.native_message, recipe=recipe, selected_message_json=original)
        assert normal[PROMPT_GUARD_THINKING_PARTS_KEY]
        wire = retained_family_wire(original)
        wire["tool_calls"][0]["function"]["arguments"] = '{"unselected_untrusted_argument":"changed"}'
        from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="original selected shelf wire"):
            await asyncio.to_thread(current.native_message, recipe=recipe, selected_message_json=encode_transcript_object_json(wire))
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_selected_failed_result_rebuilds_current_policy_schema_without_new_rows(operation_store):
    case = await failure_current_case(operation_store)
    try:
        actual = await restore_case(operation_store, case)
        assert detach_prompt_guard_evidence(actual.messages).messages == detach_prompt_guard_evidence(case["original_messages"]).messages
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_failed_result_without_actual_private_plugin_identity_cannot_mint_reference_after_positive(operation_store):
    case = await failure_current_case(operation_store)
    try:
        await restore_case(operation_store, case)
        data = case["data"]
        outcome = data.batch.tool_outcomes[0]
        changed = replace(
            outcome.response,
            validation=replace(
                outcome.response.validation,
                errors=tuple(replace(entry, plugin_identity=None) for entry in outcome.response.validation.errors),
            ),
        )
        batch = replace(data.batch, tool_outcomes=[replace(outcome, response=changed)])
        case["data"] = RestoredBatchData(batch, data.accumulator, data.calls)
        await save_family_cut(operation_store, case)
        with pytest.raises(AuditIntegrityError, match="actual current policy"):
            await restore_case(operation_store, case)
    finally:
        operation_store[1].release(case["kwargs"]["inventory"].running.session_operation_context)
