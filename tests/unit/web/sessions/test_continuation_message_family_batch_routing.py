"""Actual selected SQL reads of historical prepared/completed batch graphs.

The existing real codec fixtures supply DATA cuts. No active call is executed,
no audit is rebound, and no provider or committed writer receipt is fabricated.
"""

import asyncio
import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import (
    decode_memory_completion_batch_data,
    decode_prepared_batch_data,
    encode_memory_completion_batch_data,
    encode_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import (
    encode_continuation_response,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_continuation_memory_completion_batch_data import completed_cut
from tests.unit.web.composer.test_continuation_message_family_builders import completion_with_native
from tests.unit.web.composer.test_continuation_prepared_batch_data import prepared_cut
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import producer_case

operation_store = test_composer_operation_authority.operation_store


def encoded_cut(monkeypatch, family):
    # The existing fixture controls actual DispatchAudit timer samples. Keep
    # that patch inside this synchronous capture; no SQL/event-loop timer uses it.
    with monkeypatch.context() as clock_patch:
        if family == "prepared":
            batch, acc, calls, dialect = prepared_cut(clock_patch)
            encoded = encode_prepared_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)
            expected = decode_prepared_batch_data(encoded)
        else:
            batch, acc, calls, dialect, _invocation = completed_cut(clock_patch, kind="schema")
            encoded = encode_memory_completion_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)
            expected = decode_memory_completion_batch_data(encoded)
    assert batch.current_call.audit is not None and expected.batch.current_call.audit is None
    chunk = encode_private_chunk(codec="composer.tool-batch.v1", canonical=encoded)
    response = encode_continuation_response(completion_with_native(calls=calls))
    return chunk, response, expected


async def install_source(store, kwargs, chunk, response, next_index):
    engine, _repository, authority, sessions, sid = store
    selected = kwargs["selected"]
    running = kwargs["inventory"].running
    frames = selected.stack.frames
    if type(frames[-1]) is ToolBatchFrame:
        frames = frames[:-1]
    top = ToolBatchFrame(
        phase="tool_ready",
        response=response.reference,
        accumulator=chunk.reference,
        next_tool_index=next_index,
        settled_effect_ids=(),
    )
    root = selected.stack.model_copy(update={"frames": (*frames, top)})

    def store_cut():
        with locked_session_transaction(engine, str(sid)) as conn:
            for value in (response, chunk):
                put_chunk(
                    conn,
                    session_id=str(sid),
                    operation_id=running.claim.operation_id,
                    key=private_chunk_key(value),
                    payload=value.canonical.encode("utf-8"),
                    codec=value.reference.codec,
                )
            return append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root),
                event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"historical batch source input"}'),),
                handoff=False,
            )

    stored = await asyncio.to_thread(store_cut)
    work = inventory_work(running)
    current = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions), authority=authority, selected=current, required_work=work
    )
    work.coordinator.assert_completed()
    return {**kwargs, "selected": current, "inventory": inventory}


async def read_source(store, kwargs):
    owner = CurrentContinuationMessageFamilies(kwargs["service"], kwargs["authority"], kwargs["selected"], kwargs["inventory"])
    before = stored_rows(store[0])
    try:
        return await asyncio.to_thread(owner._read)
    finally:
        assert stored_rows(store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ("prepared", "memory"))
async def test_actual_selected_tool_batch_payload_preserves_complete_historical_roots(operation_store, monkeypatch, family):
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    try:
        chunk, response, expected = encoded_cut(monkeypatch, family)
        assert chunk.reference.codec == "composer.tool-batch.v1"
        assert json.loads(chunk.canonical)["schema_version"] == (
            "composer.prepared-batch-data.v1" if family == "prepared" else "composer.memory-completion-batch-data.v1"
        )
        kwargs = await install_source(operation_store, kwargs, chunk, response, expected.batch.next_tool_index)
        sources = await read_source(operation_store, kwargs)
        assert len(sources.batches) == 1 and sources.batches[0].response == response.reference
        data = sources.batches[0].data
        assert data.batch == expected.batch and data.accumulator == expected.accumulator and data.calls == expected.calls
        call = data.batch.current_call
        assert call.audit is None
        assert call.tool_call is data.calls[2]
        assert call.pre_state is data.batch.state is data.batch.entry_state is data.accumulator.state
        assert call.arguments is call.audit_arguments is data.batch.decoded_args_by_call_id[call.tool_call.id]
        assert data.batch.tool_outcomes[0].response is data.batch.tool_outcomes[1].response
        assert data.batch.last_runtime_preflight is data.accumulator.last_runtime_preflight
        assert (data.accumulator.advisor_calls_used, data.batch.advisor_calls_used) == (2, 4)
        assert data.accumulator.authority_revocation == expected.accumulator.authority_revocation
        if family == "memory":
            assert (
                call.phase == "application_pending" and call.owner_tool_receipt_json == expected.batch.current_call.owner_tool_receipt_json
            )
            assert type(call.memory_result) is type(expected.batch.current_call.memory_result)
            assert call.memory_result.to_dict() == expected.batch.current_call.memory_result.to_dict()
            assert call.memory_result.result.updated_state is call.pre_state
            schema = call.memory_result.result.data
            assert schema.json_schema["properties"]["items"]["default"] is schema.knob_schema["fields"][0]["default"]
        else:
            assert call.phase == "prepared" and call.owner_tool_receipt_json is None
            assert call.original_creator_id == "old-creator" and call.original_creator_epoch == 4
        # A second actual current read does not turn historical data into a live audit.
        repeated = await read_source(operation_store, kwargs)
        assert repeated.batches[0].data.batch.current_call.audit is None
    finally:
        operation_store[1].release(kwargs["inventory"].running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ("prepared", "memory"))
@pytest.mark.parametrize("change", ("payload_tag", "outer_codec", "cursor", "response_call"))
async def test_selected_batch_routing_refuses_changed_real_binding_after_positive(operation_store, monkeypatch, family, change):
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    try:
        chunk, response, expected = encoded_cut(monkeypatch, family)
        kwargs = await install_source(operation_store, kwargs, chunk, response, expected.batch.next_tool_index)
        assert len((await read_source(operation_store, kwargs)).batches) == 1
        cursor = expected.batch.next_tool_index
        if change == "payload_tag":
            payload = json.loads(chunk.canonical)
            payload["schema_version"] = "composer.unsupported-batch.v1"
            chunk = encode_private_chunk(codec="composer.tool-batch.v1", canonical=canonical_json(payload))
            message = "no implemented selected batch payload"
        elif change == "outer_codec":
            chunk = encode_private_chunk(codec="composer.tools.v1", canonical=chunk.canonical)
            message = "no implemented selected batch codec"
        elif change == "cursor":
            cursor += 1
            message = "actual call/cursor join"
        else:
            calls = (*expected.calls[:2], replace(expected.calls[2], id="another-issued-call"))
            response = encode_continuation_response(completion_with_native(calls=calls))
            message = "actual call/cursor join"
        kwargs = await install_source(operation_store, kwargs, chunk, response, cursor)
        with pytest.raises(AuditIntegrityError, match=message):
            await read_source(operation_store, kwargs)
    finally:
        operation_store[1].release(kwargs["inventory"].running.session_operation_context)
