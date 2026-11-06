"""Actual paid publication, latest runtime capture and historical native joins.

Only external SDK/guard HTTP are controlled. SQL admission, selected sources,
physical attempts, accounting, actual reply producer and capture are real. The
next action-ready input is an explicit phase-owner fixture, not a full driver.
"""

import asyncio
import json
from hashlib import sha256

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_THINKING_PARTS_KEY
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_primary import read_continuation_primary, run_continuation_primary
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners, restore_continuation_runtime
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.provider_thinking import attach_provider_thinking
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, read_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as audit_messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected, new_owners, recovery_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST, current_service
from tests.unit.web.sessions.test_continuation_runtime import install_capture, runtime_case

operation_store = operations.operation_store


def full_rows(engine):
    with engine.connect() as conn:
        return (
            recovery_rows(engine),
            tuple(conn.execute(select(physical).order_by(*physical.primary_key.columns)).all()),
            tuple(conn.execute(select(usage).order_by(*usage.primary_key.columns)).all()),
        )


async def prepared_runtime(store, tmp_path, monkeypatch, *, tool_calls=None):
    with store[0].begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    arguments, _cache, _attachments = await runtime_case(store)
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    arguments["service"]._provider_gateway = ProviderGateway(
        model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    running = arguments["inventory"].running
    calls = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            store[0], running.session_operation_context, requests, role="composer", user_prompt=REQUEST, expected_count=len(calls) + 1
        )
        with store[0].connect() as conn:
            active = conn.execute(select(effects).where(effects.c.disposition == "dispatched")).all()
            assert len(active) == 1 and active[0].result_chunk_key is None
        calls.append(kwargs)
        number = len(calls)
        return ModelResponse(
            model="test/model",
            choices=[
                {
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                    "message": {
                        "role": "assistant",
                        "content": f"Actual native reply {number}",
                        "tool_calls": tool_calls,
                        "thinking_blocks": [
                            {
                                "type": "thinking",
                                "thinking": f"Actual ordered native thought {number}",
                                "signature": f"opaque-native-signature-{number}",
                            }
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 13 + number, "completion_tokens": 5, "total_tokens": 18 + number, "cost": 0.01},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    runtime = await restore_continuation_runtime(**arguments)
    return arguments, runtime, calls


async def publish(store, arguments, runtime):
    with prompt_guard_user_scope(REQUEST):
        return await run_continuation_primary(
            service=arguments["service"],
            authority=arguments["authority"],
            selected=arguments["selected"],
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=arguments["required_work"],
            provider_owner=arguments["provider_owner"],
            timeout=runtime.source.policy.actions.primary.total,
        )


async def latest(arguments, stored):
    work = arguments["required_work"]
    selected = await load_selected_continuation(authority=arguments["authority"], running=work.running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=arguments["service"]._advisor_checkpoint, authority=arguments["authority"], selected=selected, required_work=work
    )
    return {**arguments, "selected": selected, "inventory": inventory}


async def append_actual_native_and_capture(store, arguments, runtime, result):
    old_selection, old_inventory = runtime.materialization.selected, runtime.materialization.inventory
    completion = result.outcome.completion
    assert completion.provider_metadata.thinking_blocks
    message = attach_provider_thinking(
        {"role": "assistant", "content": completion.message.content, "tool_calls": []}, completion.provider_metadata.thinking_blocks
    )
    runtime.materialization.register_native_message(response=result.response, completion=completion, message=message)
    runtime.live.llm_messages.append(message)
    runtime.live.turn_evidence.transcript.observe_messages((message,))
    current = await latest(arguments, result.snapshot)
    assert current["selected"].checkpoint_id != old_selection.checkpoint_id
    assert current["inventory"] is not old_inventory
    captured = await capture_continuation_runtime_owners(runtime, selected=current["selected"], inventory=current["inventory"])
    # Actual warm registrations remain attached to their original owner. No
    # selected/inventory field is replaced to bless a saved message wire.
    assert runtime.materialization.selected is old_selection and runtime.materialization.inventory is old_inventory
    assert captured.stack.frames[0].pending_response == result.response.reference
    assert captured.stack.frames[0].provider_effect_key == result.effect_key
    current = await install_capture(store, current, captured)
    cold = await restore_continuation_runtime(**current)
    assert cold.live.llm_messages[-1]["content"] == message["content"]
    assert cold.live.llm_messages[-1]["thinking_blocks"] == message["thinking_blocks"]
    assert PROMPT_GUARD_THINKING_PARTS_KEY in cold.live.llm_messages[-1]
    read = await read_continuation_primary(
        authority=current["authority"], selected=current["selected"], required_work=current["required_work"]
    )
    assert read.response == result.response and read.outcome.audit_call == result.outcome.audit_call
    return current, cold


async def select_next_primary_input(store, arguments):
    # Existing phase-owner input seam. No P2/P5 completion or automatic driver
    # is claimed; both provider effects still use real preparation/SDK/receipt.
    selected = arguments["selected"]
    frame = selected.stack.frames[0]
    assert frame.phase == "response_retained" and frame.provider_effect_key is not None
    next_frame = frame.model_copy(
        update={"phase": "action_ready", "provider_effect_key": None, "provider_input_sha256": None, "pending_response": None}
    )
    root = selected.stack.model_copy(update={"frames": (next_frame,)})
    running = arguments["inventory"].running
    with locked_session_transaction(store[0], str(store[4])) as conn:
        stored = append_complete_snapshot(
            conn,
            session_id=str(store[4]),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"fixture":"ordinary next primary input after actual native capture"}'),),
            handoff=False,
        )
    return await latest(arguments, stored)


async def twice(store, tmp_path, monkeypatch):
    arguments, runtime, calls = await prepared_runtime(store, tmp_path, monkeypatch)
    first = await publish(store, arguments, runtime)
    current, _cold = await append_actual_native_and_capture(store, arguments, runtime, first)
    # A second complete provider/capture phase keeps the same live custody.
    context = current["inventory"].running.session_operation_context
    assert await asyncio.to_thread(store[1].renew, context, lease_seconds=30) is context
    next_arguments = await select_next_primary_input(store, current)
    next_runtime = await restore_continuation_runtime(**next_arguments)
    assert next_arguments["selected"].stack.frames[0].provider_effect_key is None
    assert next_runtime.live.llm_messages[-1]["content"] == "Actual native reply 1"
    second = await publish(store, next_arguments, next_runtime)
    final, cold = await append_actual_native_and_capture(store, next_arguments, next_runtime, second)
    # Renew before the independent corruption/read oracle, never after loss.
    assert await asyncio.to_thread(store[1].renew, context, lease_seconds=30) is context
    assert len(calls) == 2 and first.effect_key != second.effect_key
    assert final["selected"].stack.frames[0].provider_effect_key == second.effect_key
    assert first.effect_key in {entry.effect_id for entry in final["selected"].stack.effect_frontier}
    assert [message["content"] for message in cold.live.llm_messages if "thinking_blocks" in message] == [
        "Actual native reply 1",
        "Actual native reply 2",
    ]
    return final, cold, calls, first, second


@pytest.mark.asyncio
async def test_historical_response_wrappers_keep_one_complete_current_proof_and_fresh_next_read(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.sessions import continuation_primary as primary
    from elspeth.web.sessions import continuation_selection as selection
    from elspeth.web.sessions.continuation import ContinuationCASConflict

    arguments, runtime, calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        result = await publish(operation_store, arguments, runtime)
        current = await latest(arguments, result.snapshot)
        selected = current["selected"]
        original_load = selection.load_snapshot_private_data_on_connection
        original_verify = primary._verify_committed_primary_response_data_on_connection
        loads, verified = [], []

        def load(conn, **kwargs):
            values = original_load(conn, **kwargs)
            if kwargs["selected"].checkpoint_id == selected.checkpoint_id:
                loads.append((conn, kwargs))
            return values

        def verify(conn, **kwargs):
            verified.append((conn, kwargs))
            return original_verify(conn, **kwargs)

        monkeypatch.setattr(selection, "load_snapshot_private_data_on_connection", load)
        monkeypatch.setattr(primary, "_verify_committed_primary_response_data_on_connection", verify)
        readers = (
            primary.read_committed_primary_response_on_connection,
            primary.read_committed_primary_response_for_settlement_on_connection,
        )
        before = full_rows(operation_store[0])
        for reader in readers:
            loads.clear()
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                assert reader(conn, running, selected=selected, reference=result.response.reference) == result.response
                assert len(loads) == 1 and loads[0][0] is conn
                assert verified[-1] == (
                    conn,
                    {
                        "session_id": running.claim.session_id,
                        "operation_id": running.claim.operation_id,
                        "selected": selected,
                        "reference": result.response.reference,
                    },
                )
        # The ID-only public DATA reader still owns its independent full prelude.
        loads.clear()
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            assert (
                primary.read_committed_primary_response_data_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    selected=selected,
                    reference=result.response.reference,
                )
                == result.response
            )
            assert len(loads) == 1 and loads[0][0] is conn
        assert len(verified) == 3 and full_rows(operation_store[0]) == before and len(calls) == 1

        changed_root = selected.stack.model_copy(
            update={"logical": selected.stack.logical.model_copy(update={"request_hash": "changed-current-request"})}
        )
        changed = SelectedContinuationState(selected.checkpoint_id, changed_root, selected.chunks)
        for reader in readers:
            for supplied in (changed, arguments["selected"]):
                with (
                    locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
                    pytest.raises(ContinuationCASConflict),
                ):
                    reader(conn, running, selected=supplied, reference=result.response.reference)
        assert len(verified) == 3 and full_rows(operation_store[0]) == before

        # Corrupt an unrequested selected state chunk in a later transaction;
        # the prior successful read must not lend clearance to this next call.
        state_ref = selected.stack.frames[0].state
        assert state_ref != result.response.reference
        state = next(chunk for chunk in selected.chunks if chunk.reference == state_ref)
        payload = (state.canonical + " ").encode("utf-8")
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            changed_rows = conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(operation_store[4]),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == f"value:{state_ref.codec}:{state_ref.digest}",
                )
                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
            assert changed_rows.rowcount == 1
        corrupted = full_rows(operation_store[0])
        for reader in readers:
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn, pytest.raises(AuditIntegrityError):
                reader(conn, running, selected=selected, reference=result.response.reference)
        assert len(verified) == 3 and full_rows(operation_store[0]) == corrupted and len(calls) == 1
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("stop", "expired"))
async def test_historical_response_ordinary_and_settlement_keep_actual_stop_and_expiry_distinction(
    operation_store, tmp_path, monkeypatch, cut
):
    from dataclasses import replace
    from datetime import timedelta

    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions import continuation_primary as primary
    from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_operation_segments_table as segments

    arguments, runtime, calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        result = await publish(operation_store, arguments, runtime)
        current = await latest(arguments, result.snapshot)
        selected = current["selected"]
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            assert (
                primary.read_committed_primary_response_on_connection(conn, running, selected=selected, reference=result.response.reference)
                == result.response
            )
            now = database_now(conn)
            if cut == "stop":
                changed = conn.execute(
                    update(jobs).where(jobs.c.operation_id == running.claim.operation_id).values(cancel_requested_at=now)
                )
            else:
                changed = conn.execute(
                    update(segments)
                    .where(
                        segments.c.operation_id == running.claim.operation_id,
                        segments.c.sequence == running.claim.segment_sequence,
                    )
                    .values(
                        started_at=now - timedelta(seconds=3),
                        cooperative_at=now - timedelta(seconds=2),
                        deadline_at=now - timedelta(seconds=1),
                    )
                )
            assert changed.rowcount == 1
        before = full_rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            with pytest.raises(ComposerOperationCancelledDuringTurn if cut == "stop" else ComposerOperationFenceLost):
                primary.read_committed_primary_response_on_connection(conn, running, selected=selected, reference=result.response.reference)
            assert (
                primary.read_committed_primary_response_for_settlement_on_connection(
                    conn, running, selected=selected, reference=result.response.reference
                )
                == result.response
            )
            wrong = replace(running, claim=replace(running.claim, claim_token="wrong-token"))
            with pytest.raises((AuditIntegrityError, ComposerOperationFenceLost)):
                primary.read_committed_primary_response_for_settlement_on_connection(
                    conn, wrong, selected=selected, reference=result.response.reference
                )
        assert full_rows(operation_store[0]) == before and len(calls) == 1
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_provider_publication_to_latest_capture_preserves_registered_native_source(operation_store, tmp_path, monkeypatch):
    arguments, runtime, calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        first = await publish(operation_store, arguments, runtime)
        current, cold = await append_actual_native_and_capture(operation_store, arguments, runtime, first)
        assert len(calls) == 1 and len(cold.live.recorder.llm_calls) == 1
        assert cold.live.recorder.llm_calls[0] == first.outcome.audit_call
        assert cold.bindings.mutation_ceiling == runtime.bindings.mutation_ceiling
        assert current["selected"].stack.frames[0].composition_total == arguments["selected"].stack.frames[0].composition_total
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("old_frontier", "old_ledger", "old_descriptor", "old_option", "old_physical"))
async def test_two_actual_native_providers_replay_exact_old_source_then_refuse_its_corruption(
    operation_store, tmp_path, monkeypatch, corruption
):
    arguments, _cold, calls, first, second = await twice(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            old = conn.execute(select(effects).where(effects.c.effect_key == first.effect_key)).one()
            if corruption == "old_frontier":
                selected = arguments["selected"]
                root = selected.stack.model_copy(
                    update={
                        "effect_frontier": tuple(entry for entry in selected.stack.effect_frontier if entry.effect_id != first.effect_key)
                    }
                )
                stored = append_complete_snapshot(
                    conn,
                    session_id=str(operation_store[4]),
                    operation_id=running.claim.operation_id,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.stack.cursor,
                    snapshot_json=encode_continuation_snapshot(root),
                    event_batch=(ContinuationEvent("checkpoint", '{"fixture":"remove only old committed frontier after real positive"}'),),
                    handoff=False,
                )
            elif corruption == "old_physical":
                changed = conn.execute(
                    update(effects)
                    .where(effects.c.effect_key == first.effect_key)
                    .values(provider_attempt_id=second.outcome.audit_call.call_id)
                )
                assert changed.rowcount == 1
            elif corruption == "old_ledger":
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == old.provider_attempt_id)).one()
                row = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
                changed = conn.execute(update(usage).where(usage.c.entry_id == row.entry_id).values(prompt_tokens=row.prompt_tokens + 1))
                assert changed.rowcount == 1
            else:
                key = f"provider-input:{old.input_sha256}"
                value = json.loads(read_chunk(conn, session_id=str(operation_store[4]), operation_id=running.claim.operation_id, key=key))
                if corruption == "old_option":
                    previous = value["options"].get("max_tokens")
                    assert previous is None or type(previous) is int
                    value["options"]["max_tokens"] = 1 if previous is None else previous + 1
                else:
                    value["messages"][-1]["content"] += " changed only the original paid source"
                payload = canonical_json(value).encode("utf-8")
                changed = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(operation_store[4]),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == key,
                    )
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
                assert changed.rowcount == 1
        if corruption == "old_frontier":
            arguments = await latest(arguments, stored)
        # The current second result remains the selected locator. We refuse the
        # old response's exact source; choosing a latest row would miss this.
        assert arguments["selected"].stack.frames[0].provider_effect_key == second.effect_key
        before = full_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError):
            await restore_continuation_runtime(**arguments)
        assert full_rows(operation_store[0]) == before and len(calls) == 2
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_v4_publication_rechecks_selected_wire_binding_after_second_sdk(operation_store, tmp_path, monkeypatch):
    arguments, runtime, calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        first = await publish(operation_store, arguments, runtime)
        current, _cold = await append_actual_native_and_capture(operation_store, arguments, runtime, first)
        current = await select_next_primary_input(operation_store, current)
        next_runtime = await restore_continuation_runtime(**current)
        actual_completion = litellm.acompletion
        before = []
        responses = []
        returned = []

        def publication_rows():
            with operation_store[0].connect() as conn:
                return (
                    full_rows(operation_store[0]),
                    tuple(conn.execute(select(holds).order_by(*holds.primary_key.columns)).all()),
                    tuple(conn.execute(select(audit_messages).order_by(*audit_messages.primary_key.columns)).all()),
                )

        async def corrupt_selected_after_sdk(**kwargs):
            response = await actual_completion(**kwargs)
            assert len(calls) == 2
            responses.append(response)
            selected = current["authority"].load_continuation_snapshot(running)
            frame = decode_continuation_snapshot(selected.payload_json).frames[0]
            assert frame.phase == "awaiting_response" and frame.pending_response is None
            key = f"value:{frame.messages.codec}:{frame.messages.digest}"
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                effect = conn.execute(select(effects).where(effects.c.disposition == "dispatched")).one()
                assert effect.effect_key == frame.provider_effect_key and effect.effect_key != first.effect_key
                assert effect.result_chunk_key is None and effect.receipt_key is None
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one()
                hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == attempt.attempt_id)).one()
                assert hold.state == "active" and attempt.settled_at is None and attempt.ledger_entry_id is None
                value = json.loads(read_chunk(conn, session_id=str(operation_store[4]), operation_id=running.claim.operation_id, key=key))
                position = value["current_indexes"][-1]
                message = json.loads(value["messages_json"][position])
                message["content"] += " changed only selected input after SDK"
                value["messages_json"][position] = canonical_json(message)
                payload = canonical_json(value).encode("utf-8")
                changed = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(operation_store[4]),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == key,
                    )
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
                assert changed.rowcount == 1
            before.append(publication_rows())
            return response

        monkeypatch.setattr(litellm, "acompletion", corrupt_selected_after_sdk)
        # Updating SQL's own checksum does not change the selected reference.
        # This exercises fresh binding validation, before wire projection equality.
        with pytest.raises(AuditIntegrityError, match=r"^private chunk bytes differ from their binding$") as raised:
            returned.append(await publish(operation_store, current, next_runtime))
        assert type(raised.value) is AuditIntegrityError and returned == []
        assert len(calls) == 2 and len(responses) == 1 and len(before) == 1
        assert publication_rows() == before[0]
        with operation_store[0].connect() as conn:
            unresolved = conn.execute(select(effects).where(effects.c.disposition == "dispatched")).one()
            assert unresolved.result_chunk_key is None and unresolved.receipt_key is None
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == unresolved.provider_attempt_id)).one()
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == attempt.attempt_id)).one()
            assert attempt.settled_at is None and attempt.ledger_entry_id is None and hold.state == "active"
            committed = conn.execute(select(effects).where(effects.c.effect_key == first.effect_key)).one()
            assert committed.disposition == "committed" and committed.receipt_key == first.outcome.audit_call.call_id
            assert committed.result_chunk_key == f"value:{first.response.reference.codec}:{first.response.reference.digest}"
            ledger = conn.execute(select(usage)).one()
            first_attempt = conn.execute(select(physical).where(physical.c.attempt_id == committed.provider_attempt_id)).one()
            assert first_attempt.ledger_entry_id == ledger.entry_id and first_attempt.settled_at is not None
        current["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_two_original_native_sources_survive_actual_fresh_claim_without_new_sdk(operation_store, tmp_path, monkeypatch):
    arguments, old_runtime, calls, first, second = await twice(operation_store, tmp_path, monkeypatch)
    first_running = arguments["inventory"].running
    arguments["required_work"].coordinator.assert_completed()
    operation_store[1].release(first_running.session_operation_context)
    current = new_owners(operation_store)
    _engine, repository, authority, sessions, sid = current
    assert (
        repository.recover_composer_segment(
            session_id=sid,
            operation_id=first_running.claim.operation_id,
            expected_generation=first_running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    try:
        selected, inventory = await current_selected(current, running)
        service, _actors = current_service(current)
        work = inventory_work(running)
        fresh = await restore_continuation_runtime(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=work,
            provider_owner=ProviderInvocationOwner(service=sessions, required_work=work),
        )
        assert fresh.bindings.session_operation_context is context and context.fence != first_running.session_operation_context.fence
        assert selected.stack.frames[0].provider_effect_key == second.effect_key
        assert [message["content"] for message in fresh.live.llm_messages if "thinking_blocks" in message] == [
            "Actual native reply 1",
            "Actual native reply 2",
        ]
        assert fresh.live.recorder.llm_calls == old_runtime.live.recorder.llm_calls
        assert {call.call_id for call in fresh.live.recorder.llm_calls} == {
            first.outcome.audit_call.call_id,
            second.outcome.audit_call.call_id,
        }
        assert len(calls) == 2
        assert fresh.live.continuation_cache_history == old_runtime.live.continuation_cache_history
        assert fresh.attachments == old_runtime.attachments
        work.coordinator.assert_completed()
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_actual_publication_with_another_admitted_recorder_refuses_stale_runtime_capture(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.audit import BufferingRecorder

    arguments, runtime, calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        alternate, drained = BufferingRecorder.for_continuation(runtime.live.recorder.snapshot_for_continuation(drained=runtime.drained))
        with prompt_guard_user_scope(REQUEST):
            result = await run_continuation_primary(
                service=arguments["service"],
                authority=arguments["authority"],
                selected=arguments["selected"],
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=alternate,
                drained=drained,
                required_work=arguments["required_work"],
                provider_owner=arguments["provider_owner"],
                timeout=5,
            )
        current = await latest(arguments, result.snapshot)
        read = await read_continuation_primary(
            authority=current["authority"], selected=current["selected"], required_work=current["required_work"]
        )
        assert read.response == result.response and len(alternate.llm_calls) == 1 and runtime.live.recorder.llm_calls == ()
        before = full_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="current SQL recorder prefix"):
            await capture_continuation_runtime_owners(runtime, selected=current["selected"], inventory=current["inventory"])
        assert full_rows(operation_store[0]) == before and len(calls) == 1
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
