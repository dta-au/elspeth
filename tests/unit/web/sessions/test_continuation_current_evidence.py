"""Join real current message producers to the original shared evidence owner."""

import asyncio
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PROMPT_GUARD_TRUST_KEY
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.composer.continuation_current_evidence import restore_current_turn_evidence
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_prompt_producers import producer_case, save_produced_shelf

operation_store = operations.operation_store


async def guarded_case(store):
    kwargs, evidence, recorder, snapshot, iteration, _actors = await producer_case(store)
    production = await load_current_message_producers(**kwargs, initial=True)
    assert production.messages is not None
    initial = list(production.messages)
    assert any(PROMPT_GUARD_TRUST_KEY in message for message in initial)
    recovery = production.producers.recovery_instruction()
    reply = production.producers.reply_instruction()
    distinct = {"role": "user", "content": "Equal but distinct retained input"}
    equal = dict(distinct)
    transcript = evidence.transcript
    transcript.begin_messages(initial)
    transcript.observe_messages((recovery, reply, distinct, equal))
    messages = [*initial, recovery, reply, distinct, equal, distinct]
    following, shelf = await save_produced_shelf(
        store, kwargs, snapshot, iteration, transcript, messages, production.producers, recorder=recorder
    )
    kwargs["required_work"].coordinator.assert_completed()
    current = await load_current_message_producers(**following)
    assert current.messages is None
    return following, current.producers, shelf, recorder


@pytest.mark.asyncio
async def test_current_materialization_restores_one_complete_evidence_shelf(operation_store):
    kwargs, materialization, shelf, original = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    try:
        # Existing plain-wire restore refuses guarded recipes; it never silently
        # downgrades their trust or invokes the provider.
        with pytest.raises(AuditIntegrityError, match="retained producers require current selected materialization"):
            await restore_turn_evidence(**kwargs)
        restored = await restore_current_turn_evidence(**kwargs, materialization=materialization)
        messages = restored.messages
        assert messages[-1] is messages[-3]
        assert messages[-2] is not messages[-3]
        assert any(messages[0] is item for item in restored.evidence.transcript._excluded)
        assert any(messages[-3] is item.message for item in restored.evidence.transcript._observed)
        assert any(PROMPT_GUARD_TRUST_KEY in message for message in messages)
        assert restored.evidence.transcript.snapshot_for_continuation(messages, materialization=materialization) == shelf
        assert restored.recorder.snapshot_for_continuation(drained=restored.drained) == original.snapshot_for_continuation(
            drained=restored.drained
        )
        assert all(
            materialization.capture_message(message)[0] == shelf.messages_json[index]
            for message, index in zip(messages, shelf.current_indexes, strict=True)
        )
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("selected", "inventory"))
async def test_current_graph_refuses_a_different_nominal_materialization_binding_after_positive(operation_store, change):
    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    try:
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        different = dict(kwargs)
        if change == "selected":
            selected = kwargs["selected"]
            different["selected"] = SelectedContinuationState(selected.checkpoint_id, selected.stack, selected.chunks)
        else:
            different["inventory"] = replace(kwargs["inventory"])
        with pytest.raises(AuditIntegrityError, match="actual selected message producers"):
            await restore_current_turn_evidence(**different, materialization=materialization)
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_graph_preserves_original_worker_error_identity(operation_store, monkeypatch):
    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    original = ValueError("Actual current source read failure")

    def failed(*args, **keywords):
        raise original

    try:
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        monkeypatch.setattr(producers, "_read_current_inputs", failed)
        with pytest.raises(ValueError) as caught:
            await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert caught.value is original
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_graph_refuses_stop_after_actual_reconstruction(operation_store, monkeypatch):
    from elspeth.web.composer import continuation_current_evidence as bridge

    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    actual = bridge._restore_current_evidence

    def stop_after_graph(*args, **values):
        restored = actual(*args, **values)
        with operation_store[0].begin() as conn:
            conn.execute(
                update(jobs).where(jobs.c.operation_id == running.claim.operation_id).values(cancel_requested_at=database_now(conn))
            )
        return restored

    try:
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        monkeypatch.setattr(bridge, "_restore_current_evidence", stop_after_graph)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await restore_current_turn_evidence(**kwargs, materialization=materialization)
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_started_graph_worker_cancellation_is_delivered_after_completion(operation_store, monkeypatch):
    from elspeth.web.composer import continuation_current_evidence as bridge

    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    entered, release = Event(), Event()
    actual = bridge._restore_current_message_evidence
    completed = []

    def held(**values):
        result = actual(**values)
        completed.append(result)
        entered.set()
        assert release.wait(10)
        return result

    try:
        await restore_current_turn_evidence(**kwargs, materialization=materialization)
        monkeypatch.setattr(bridge, "_restore_current_message_evidence", held)
        task = asyncio.create_task(restore_current_turn_evidence(**kwargs, materialization=materialization))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel("current evidence cancellation")
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError, match="current evidence cancellation"):
            await task
        assert len(completed) == 1
        assert completed[0].messages[-1] is completed[0].messages[-3]
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
