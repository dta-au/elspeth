"""Join real current message producers to the original shared evidence owner."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PROMPT_GUARD_TRUST_KEY
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.composer.continuation_current_evidence import restore_current_turn_evidence
from elspeth.web.composer.continuation_evidence import admit_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_prompt_producers import CurrentMessageProduction, load_current_message_producers
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
async def test_actual_current_source_records_close_bindings_without_freezing_live_messages(operation_store):
    kwargs, _materialization, shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    try:
        production = await load_current_message_producers(**kwargs, retained_shelf=shelf)
        assert type(production) is CurrentMessageProduction and production.messages is not None
        restored = await restore_current_turn_evidence(**kwargs, materialization=production.producers)
        assert type(restored) is RestoredTurnEvidence
        messages, owner = production.messages, production.producers
        graph_messages, evidence, recorder, drained = restored.messages, restored.evidence, restored.recorder, restored.drained
        assert restored.evidence.transcript.snapshot_for_continuation(graph_messages, materialization=owner) == shelf

        equal_production = CurrentMessageProduction(owner, tuple(dict(message) for message in messages))
        assert equal_production == production and equal_production is not production
        assert equal_production.messages[0] is not messages[0]
        equal_graph = RestoredTurnEvidence(evidence, list(graph_messages), recorder, drained)
        assert equal_graph == restored and equal_graph.messages is not graph_messages
        assert restored.batch is restored.applications is restored.selected_audit is restored.selected_dialect is None
        assert production != restored

        with pytest.raises(FrozenInstanceError):
            production.messages = ()
        with pytest.raises(FrozenInstanceError):
            del production.messages
        with pytest.raises(FrozenInstanceError):
            production._sealed = False
        with pytest.raises(FrozenInstanceError):
            production.extra = object()
        with pytest.raises(FrozenInstanceError, match="reinitialized"):
            CurrentMessageProduction.__init__(production, owner, ())
        with pytest.raises(AttributeError):
            _ = production.__dict__
        assert production.producers is owner and production.messages is messages

        with pytest.raises(FrozenInstanceError):
            restored.messages = []
        with pytest.raises(FrozenInstanceError):
            del restored.evidence
        with pytest.raises(FrozenInstanceError):
            restored._sealed = False
        with pytest.raises(FrozenInstanceError):
            restored.extra = object()
        with pytest.raises(FrozenInstanceError, match="reinitialized"):
            RestoredTurnEvidence.__init__(restored, evidence, [], recorder, drained)
        with pytest.raises(AttributeError):
            _ = restored.__dict__
        assert restored.messages is graph_messages and restored.evidence is evidence
        assert restored.recorder is recorder and restored.drained is drained

        message = messages[0]
        assert type(message) is dict
        original_body = message["content"]
        assert type(original_body) is str
        wire = owner.capture_message(message)
        message["content"] = original_body + " Changed live payload."
        try:
            with pytest.raises(AuditIntegrityError):
                owner.capture_message(message)
        finally:
            message["content"] = original_body
        assert production.messages is messages and messages[0] is message and owner.capture_message(message) == wire
        assert restored.evidence.transcript.snapshot_for_continuation(graph_messages, materialization=owner) == shelf
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_restore_receives_the_exact_fresh_sql_admission_and_keeps_the_final_read(operation_store, monkeypatch):
    from elspeth.web.composer import continuation_current_evidence as bridge

    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    original_inputs = materialization.inputs
    actual_read = producers.CurrentContinuationMessageProducers._require_current
    actual_restore = bridge._restore_current_evidence
    observed = []
    transferred = []

    def read(self):
        value = actual_read(self)
        observed.append(value)
        return value

    def restore(evidence, **values):
        assert observed and evidence is observed[-1].admitted_evidence
        assert evidence is not original_inputs.admitted_evidence
        result = actual_restore(evidence, **values)
        transferred.append((evidence, len(observed)))
        return result

    monkeypatch.setattr(producers.CurrentContinuationMessageProducers, "_require_current", read)
    monkeypatch.setattr(bridge, "_restore_current_evidence", restore)
    try:
        result = await restore_current_turn_evidence(**kwargs, materialization=materialization)
        assert len(transferred) == 1
        assert len(observed) > transferred[0][1]
        assert observed[-1].admitted_evidence is not transferred[0][0]
        assert all(value == original_inputs for value in observed)
        assert all(value.evidence is value.admitted_evidence.snapshot for value in observed)
        assert len({id(value.admitted_evidence) for value in observed}) == len(observed)
        assert result.messages[-1] is result.messages[-3]
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_input_equality_retains_complete_snapshot_data_and_derives_it_from_one_admission(operation_store):
    kwargs, materialization, _shelf, _recorder = await guarded_case(operation_store)
    running = kwargs["inventory"].running
    try:
        first = await asyncio.to_thread(materialization._require_current)
        second = await asyncio.to_thread(materialization._require_current)
        assert first == second
        assert first.admitted_evidence is not second.admitted_evidence
        assert first.evidence is first.admitted_evidence.snapshot
        assert second.evidence is second.admitted_evidence.snapshot
        changed_source = first.evidence.model_copy(update={"actor_user_id": "different-retained-actor"})
        changed_admission = admit_turn_evidence(
            changed_source, env_ref_names=kwargs["inventory"].names, source_chunks=first.admitted_evidence.dependency_chunks
        )
        changed = replace(first, admitted_evidence=changed_admission)
        assert changed.evidence is changed_admission.snapshot and changed.evidence != first.evidence
        assert changed.bootstrap is first.bootstrap and changed.values is first.values
        assert changed.producer_inputs is first.producer_inputs and changed.evidence_messages_json == first.evidence_messages_json
        assert changed != first
        # This is only a DATA equality discriminator, never current authority.
        # Python 3.12 raises ValueError here; Python 3.13 raises TypeError.
        with pytest.raises((TypeError, ValueError), match="init=False"):
            replace(first, evidence=changed_source)
        with pytest.raises(TypeError, match="actual source-admitted evidence"):
            replace(first, admitted_evidence=changed_source)
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
