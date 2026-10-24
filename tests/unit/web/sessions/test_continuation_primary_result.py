"""Actual primary SDK/audit/ledger publication and cold SQL-only source reads.

The external SDK and guard HTTP are controlled. Session admission, SQL,
current message producers, quota, original audit and settlement are real.
"""

import asyncio
import json
from dataclasses import replace

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence, recorder_from_evidence_json
from elspeth.web.composer.continuation_evidence_application_data import (
    admit_journal_values,
    append_primary_call_to_evidence,
    require_evidence_application_links,
)
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
from elspeth.web.composer.continuation_p4_application_restore import apply_selected_p4_to_cold_evidence
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_primary import read_continuation_primary, run_continuation_primary
from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash, continuation_provider_input_json
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    PreparedContinuationProvider,
    append_complete_snapshot,
    private_chunk_key,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST, producer_case

operation_store = test_composer_operation_authority.operation_store


def test_actual_primary_input_descriptor_preserves_existing_hash_and_transport_exclusions():
    kwargs = {
        "model": "test/model",
        "messages": [{"role": "user", "content": "Retain café exactly"}],
        "num_retries": 0,
        "max_retries": 0,
        "seed": 0,
        "temperature": None,
        "api_base": "https://endpoint.example.test",
        "api_key": "synthetic-transport-only-value",  # secret-scan: allow-this-line
        "extra_headers": {"Authorization": "synthetic-header-only-value"},
        "timeout": 3,
    }
    # Literal original pure hash payload, independent of the factored helper.
    expected = {
        "schema": "composer.provider-input.v1",
        "messages": kwargs["messages"],
        "tools_present": False,
        "tools": None,
        "options": {
            "model": "test/model",
            "num_retries": 0,
            "max_retries": 0,
            "seed": 0,
            "temperature": None,
            "api_base": "https://endpoint.example.test",
        },
    }
    text = continuation_provider_input_json(kwargs)
    assert text == canonical_json(expected)
    assert continuation_provider_input_hash(kwargs) == stable_hash(expected) == stable_hash(json.loads(text))
    assert "synthetic-transport-only-value" not in text and "synthetic-header-only-value" not in text
    changed = {**kwargs, "seed": 1}
    assert continuation_provider_input_hash(changed) != continuation_provider_input_hash(kwargs)


def stored_rows(engine):
    with engine.connect() as conn:
        return tuple(
            tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
            for table in (messages, effects, physical, usage, jobs, chunks, checkpoints, events)
        )


async def primary_input(store, tmp_path, monkeypatch):
    with store[0].begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    kwargs, _evidence, recorder, snapshot, _iteration, _actors = await producer_case(store)
    produced = await load_current_message_producers(**kwargs, retained_shelf=snapshot.transcript)
    assert produced.messages is not None
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    kwargs["service"]._provider_gateway = ProviderGateway(
        model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    return kwargs, produced, recorder, requests


async def post_p4_primary_input(store, tmp_path, monkeypatch):
    """Actual persisted/applied joint graph, at the next ordinary phase input."""
    kwargs, _produced, _recorder, requests = await primary_input(store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    restored = await restore_turn_evidence(**kwargs)
    kwargs["required_work"].coordinator.assert_completed()
    renewed = await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=30)
    assert renewed is running.session_operation_context
    running, pending, owed, joint_snapshot, _commit = await committed_evidence_case(
        store, graph=True, joint=True, current=(running, kwargs["selected"], kwargs["inventory"], restored)
    )
    renewed = await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=30)
    assert renewed is running.session_operation_context
    work = inventory_work(running)
    application = await apply_selected_p4_to_cold_evidence(authority=store[2], selected=pending, inventory=owed, required_work=work)
    work.coordinator.assert_completed()
    renewed = await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=30)
    assert renewed is running.session_operation_context
    head = application.stored.snapshot
    root = decode_continuation_snapshot(head.payload_json)
    compose = root.frames[0]
    assert type(compose) is ComposeFrame and application.restored.applications is not None
    # This provider-owner fixture begins after real P4 application. Only the
    # ordinary phase input is selected here; no writer/receipt/history is faked.
    ready = root.model_copy(update={"frames": (compose.model_copy(update={"phase": "action_ready"}),)})
    with locked_session_transaction(store[0], str(store[4])) as conn:
        head = append_complete_snapshot(
            conn,
            session_id=str(store[4]),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=head.checkpoint_id,
            expected_cursor=head.cursor,
            snapshot_json=encode_continuation_snapshot(ready),
            event_batch=(ContinuationEvent("checkpoint", '{"fixture":"ordinary next primary input after actual P4 application"}'),),
            handoff=False,
        )
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=store[2], running=running, selected=head, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(store[3]), authority=store[2], selected=selected, required_work=work
    )
    work.coordinator.assert_completed()
    kwargs = {**kwargs, "selected": selected, "inventory": inventory, "required_work": inventory_work(running)}
    shelf = decode_message_shelf(next(chunk for chunk in selected.chunks if chunk.reference == ready.frames[0].transcript))
    produced = await load_current_message_producers(**kwargs, retained_shelf=shelf)
    assert produced.messages is not None
    return (
        {**kwargs, "prior_joint_snapshot": joint_snapshot},
        produced,
        application.restored.recorder,
        requests,
        application.restored.drained,
    )


async def publish_actual(store, tmp_path, monkeypatch, *, failure=None, post_p4=False):
    if post_p4:
        kwargs, produced, recorder, requests, drained = await post_p4_primary_input(store, tmp_path, monkeypatch)
    else:
        kwargs, produced, recorder, requests = await primary_input(store, tmp_path, monkeypatch)
        drained = RecorderDrainCursors()
    running = kwargs["inventory"].running
    engine = store[0]
    physical_calls = []

    async def completion(**sdk_kwargs):
        _assert_required_guard_admitted(engine, running.session_operation_context, requests, role="composer", user_prompt=REQUEST)
        physical_calls.append(sdk_kwargs)
        # The intent must have committed before the actual SDK entry.
        with engine.connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.owner == "provider")).one()
        assert effect.disposition == "dispatched" and effect.result_chunk_key is None
        if failure is not None:
            raise failure
        return ModelResponse(
            model="test/model",
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "Actual café reply", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    work = kwargs["required_work"]
    provider = ProviderInvocationOwner(service=store[3], required_work=work)
    args = {
        "service": kwargs["service"],
        "authority": kwargs["authority"],
        "selected": kwargs["selected"],
        "producers": produced.producers,
        "messages": list(produced.messages),
        "tools": [],
        "recorder": recorder,
        "drained": drained,
        "required_work": work,
        "provider_owner": provider,
        "timeout": 5,
    }
    with prompt_guard_user_scope(REQUEST):
        if failure is None:
            result = await run_continuation_primary(**args)
        else:
            with pytest.raises(type(failure)) as caught:
                await run_continuation_primary(**args)
            assert caught.value is failure
            result = None
    work.coordinator.assert_completed()
    return kwargs, produced, recorder, physical_calls, result


@pytest.mark.asyncio
async def test_primary_publishes_actual_original_response_audit_ledger_and_complete_recorder_once(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, recorder, physical_calls, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        assert len(physical_calls) == len(recorder.llm_calls) == 1
        assert result.outcome.audit_call is recorder.llm_calls[0] is result.buffer.llm_calls[0]
        assert result.outcome.completion.message.content == "Actual café reply"
        assert result.buffer.withheld_replies and result.buffer.drained == RecorderDrainCursors()
        frame = decode_continuation_snapshot(result.snapshot.payload_json).frames[-1]
        assert frame.phase == "response_retained" and frame.pending_response == result.response.reference
        with operation_store[0].connect() as conn:
            effect = conn.execute(select(effects)).one()
            attempt = conn.execute(select(physical)).one()
            ledger = conn.execute(select(usage)).one()
        assert effect.disposition == "committed" and effect.provider_attempt_id == result.outcome.audit_call.call_id
        assert attempt.ledger_entry_id == ledger.entry_id and ledger.prompt_tokens == 7 and ledger.completion_tokens == 3
        assert effect.physical_generation == running.claim.ownership_generation
        assert result.snapshot == kwargs["authority"].load_continuation_snapshot(running)
        before = stored_rows(operation_store[0])
        head = result.snapshot
        work = inventory_work(running)
        selected = await load_selected_continuation(authority=kwargs["authority"], running=running, selected=head, required_work=work)
        read = await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        work.coordinator.assert_completed()
        assert read.response == result.response and read.recorder == result.recorder
        assert read.outcome.audit_call is read.buffer.llm_calls[0]
        assert stored_rows(operation_store[0]) == before and len(physical_calls) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_publication_preserves_recorder_prefix_and_rolls_back_all_result_bytes(operation_store, tmp_path, monkeypatch):
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation import load_snapshot
    from elspeth.web.sessions.continuation_primary import publish_primary_provider_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction

    checked = []

    def observe(self, binding, *, selected, outcome, recorder, env_ref_names=frozenset()):
        with locked_session_transaction(self._engine, str(binding.running.claim.session_id)) as conn:

            def actual_rows():
                return tuple(tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all()) for table in (effects, chunks))

            before = actual_rows()
            positive = conn.begin_nested()
            original = publish_primary_provider_on_connection(
                conn, binding, selected=selected, outcome=outcome, recorder=recorder, env_ref_names=env_ref_names
            )
            assert original.outcome is outcome and original.buffer is recorder
            assert original.snapshot == load_snapshot(
                conn, session_id=str(binding.running.claim.session_id), operation_id=binding.running.claim.operation_id
            )
            positive.rollback()
            assert actual_rows() == before
            assert recorder.withheld_replies
            changed = replace(recorder, withheld_replies=())
            negative = conn.begin_nested()
            with pytest.raises(AuditIntegrityError, match="complete original recorder prefix"):
                publish_primary_provider_on_connection(
                    conn, binding, selected=selected, outcome=outcome, recorder=changed, env_ref_names=env_ref_names
                )
            negative.rollback()
            assert actual_rows() == before
            checked.append(outcome.audit_call)
            return publish_primary_provider_on_connection(
                conn, binding, selected=selected, outcome=outcome, recorder=recorder, env_ref_names=env_ref_names
            )

    monkeypatch.setattr(ComposerAsyncOperationAuthority, "publish_continuation_primary", observe)
    kwargs, _produced, recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    try:
        assert result is not None and checked == [result.outcome.audit_call]
        assert len(sdk) == len(recorder.llm_calls) == 1
    finally:
        operation_store[1].release(kwargs["inventory"].running.session_operation_context)


@pytest.mark.asyncio
async def test_committed_primary_survives_real_release_recovery_new_claim_and_never_reenters_sdk(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    first = kwargs["inventory"].running
    assert result is not None
    fresh = new_owners(operation_store)
    engine, repository, authority, _sessions, sid = fresh
    operation_store[1].release(first.session_operation_context)
    second = None
    try:
        assert (
            repository.recover_composer_segment(
                session_id=sid,
                operation_id=first.claim.operation_id,
                expected_generation=first.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
            is True
        )
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        second = ComposerOperationRunning(claim, context)
        assert claim.ownership_generation == first.claim.ownership_generation + 1
        assert context.fence.operation_epoch > first.session_operation_context.fence.operation_epoch
        work = inventory_work(second)
        head = authority.load_continuation_snapshot(second)
        selected = await load_selected_continuation(authority=authority, running=second, selected=head, required_work=work)
        policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
        with engine.connect() as conn:
            next_action = conn.execute(
                select(segments.c.next_action_json).where(
                    segments.c.session_id == str(sid),
                    segments.c.operation_id == claim.operation_id,
                    segments.c.sequence == claim.segment_sequence,
                )
            ).scalar_one()
        assert next_action == policy.actions.session_sql.model_dump_json()
        before = stored_rows(engine)
        original = await read_continuation_primary(authority=authority, selected=selected, required_work=work)
        work.coordinator.assert_completed()
        assert original.response == result.response and original.recorder == result.recorder
        assert original.outcome.audit_call == result.outcome.audit_call and original.outcome.audit_call is original.buffer.llm_calls[0]
        assert len(sdk) == 1 and stored_rows(engine) == before
        with pytest.raises((ComposerOperationFenceLost, SessionOperationFenceLost)):
            authority.load_continuation_snapshot(first)
    finally:
        if second is not None:
            repository.release(second.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("ledger", "source_audit", "selected_response", "physical_creator"))
async def test_primary_current_reader_refuses_one_corrupted_source_after_actual_positive(operation_store, tmp_path, monkeypatch, changed):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    authority = kwargs["authority"]
    try:
        assert result is not None
        head = result.snapshot
        work = inventory_work(running)
        selected = await load_selected_continuation(authority=authority, running=running, selected=head, required_work=work)
        work.coordinator.assert_completed()
        source = {
            "selected": selected,
            "checkpoint_id": head.checkpoint_id,
            "cursor": head.cursor,
            "effect_key": result.effect_key,
            "input_sha256": selected.stack.frames[-1].provider_input_sha256,
        }
        assert authority.read_continuation_primary(running, **source).response == result.response
        # Actual positive first. A savepoint controls only one source fact; the
        # ordinary owner is called on the same real transaction, then restored.
        from elspeth.web.sessions.continuation_primary import read_primary_provider_on_connection
        from elspeth.web.sessions.locking import locked_session_transaction

        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            savepoint = conn.begin_nested()
            if changed == "ledger":
                conn.execute(update(usage).values(prompt_tokens=8))
            elif changed == "source_audit":
                rows = conn.execute(select(messages)).all()
                actual = next(
                    row for row in rows if row.tool_calls and any(item.get("_kind") == "llm_call_audit" for item in row.tool_calls)
                )
                conn.execute(update(messages).where(messages.c.id == actual.id).values(tool_calls=[]))
            elif changed == "selected_response":
                conn.execute(update(effects).values(result_chunk_key=private_chunk_key(result.recorder)))
            else:
                # The private projection does not carry the lease token. This
                # isolates the actual physical creator join from that backstop.
                conn.execute(update(physical).values(lease_token=running.session_operation_context.fence.lease_token + "changed"))
            with pytest.raises(AuditIntegrityError):
                read_primary_provider_on_connection(conn, running, **source)
            savepoint.rollback()
            assert read_primary_provider_on_connection(conn, running, **source).response == result.response
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_physical_input_mismatch_refuses_before_sdk_after_actual_preparation_positive(operation_store, tmp_path, monkeypatch):
    original = ProviderGateway.prepare_continuation_primary
    original_outcome = ProviderGateway._call_llm_outcome_with_audit
    prepared = []

    def observe(self, messages, tools):
        actual = original(self, messages, tools)
        prepared.append(actual)
        return actual

    async def changed(self, *args, **kwargs):
        assert self._settings.composer_seed != 404
        self._settings = self._settings.model_copy(update={"composer_seed": 404})
        return await original_outcome(self, *args, **kwargs)

    monkeypatch.setattr(ProviderGateway, "prepare_continuation_primary", observe)
    monkeypatch.setattr(ProviderGateway, "_call_llm_outcome_with_audit", changed)
    # The mismatch is an actual original boundary failure, with no SDK audit
    # invented for an unentered physical call. Its durable intent stays finite.
    failure = AuditIntegrityError
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    sdk = []

    async def unexpected(**kwargs):
        sdk.append(kwargs)
        raise AssertionError("Mismatched actual wire reached the SDK")

    monkeypatch.setattr(litellm, "acompletion", unexpected)
    work = kwargs["required_work"]
    try:
        with prompt_guard_user_scope(REQUEST), pytest.raises(failure, match="actual prepared continuation input"):
            await run_continuation_primary(
                service=kwargs["service"],
                authority=kwargs["authority"],
                selected=kwargs["selected"],
                producers=produced.producers,
                messages=list(produced.messages),
                tools=[],
                recorder=recorder,
                drained=RecorderDrainCursors(),
                required_work=work,
                provider_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work),
                timeout=5,
            )
        assert len(prepared) == 1 and sdk == [] and recorder.llm_calls == ()
        with operation_store[0].connect() as conn:
            assert conn.execute(select(physical)).all() == []
            assert conn.execute(select(usage)).all() == []
            assert conn.execute(select(effects.c.disposition)).scalar_one() == "dispatched"
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_primary_input_refuses_changed_selected_message_after_actual_complete_positive(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_primary import _freeze_input

    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    args = {
        "service": kwargs["service"],
        "authority": kwargs["authority"],
        "selected": kwargs["selected"],
        "producers": produced.producers,
        "messages": list(produced.messages),
        "tools": [],
        "recorder": recorder,
        "drained": RecorderDrainCursors(),
    }
    try:
        actual = await asyncio.to_thread(_freeze_input, **args)
        assert tuple(actual.messages) == tuple(produced.messages)
        changed = [dict(message) for message in produced.messages]
        changed[-1]["content"] += " changed untrusted request"
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="complete current message producers"):
            await asyncio.to_thread(_freeze_input, **{**args, "messages": changed})
        assert stored_rows(operation_store[0]) == before
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", (False, True))
async def test_failed_or_cancelled_primary_remains_unresolved_and_cannot_retry(operation_store, tmp_path, monkeypatch, cancel):
    error = asyncio.CancelledError("actual SDK cancellation") if cancel else ValueError("actual SDK failure")
    kwargs, _produced, recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch, failure=error)
    running = kwargs["inventory"].running
    released = False
    try:
        assert result is None and len(sdk) == len(recorder.llm_calls) == 1
        head = kwargs["authority"].load_continuation_snapshot(running)
        frame = decode_continuation_snapshot(head.payload_json).frames[-1]
        work = inventory_work(running)
        selected = await load_selected_continuation(authority=kwargs["authority"], running=running, selected=head, required_work=work)
        work.coordinator.assert_completed()
        before = stored_rows(operation_store[0])
        with pytest.raises(ContinuationCASConflict, match="cannot retry"):
            kwargs["authority"].read_continuation_primary(
                running,
                selected=selected,
                checkpoint_id=head.checkpoint_id,
                cursor=head.cursor,
                effect_key=frame.provider_effect_key,
                input_sha256=frame.provider_input_sha256,
            )
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        operation_store[1].release(running.session_operation_context)
        released = True
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError):
            new_owners(operation_store)[1].recover_composer_segment(
                session_id=operation_store[4],
                operation_id=running.claim.operation_id,
                expected_generation=running.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
    finally:
        if not released:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_prepared_primary_intent_cannot_dispatch_twice_or_escape_its_actual_owner(operation_store, tmp_path, monkeypatch):
    kwargs, produced, _recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    authority = kwargs["authority"]
    running = kwargs["inventory"].running
    try:
        request = kwargs["service"]._provider_gateway.prepare_continuation_primary(list(produced.messages), [])
        selected = kwargs["selected"]
        prepared = authority.prepare_provider_action(
            running,
            family=0,
            input_sha256=request.input_sha256,
            snapshot_json=encode_continuation_snapshot(selected.stack),
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            primary_input_json=continuation_provider_input_json(request.kwargs),
        )
        dispatch = authority.begin_continuation_primary(prepared)
        # A new checkpoint cannot turn an old dispatch into a new SDK right.
        before = stored_rows(operation_store[0])
        with pytest.raises(ContinuationCASConflict):
            authority.begin_continuation_primary(PreparedContinuationProvider(prepared.binding, dispatch))
        assert stored_rows(operation_store[0]) == before
        foreign = replace(prepared.binding, running=replace(running, claim=replace(running.claim, attempt=running.claim.attempt + 1)))
        with pytest.raises(AuditIntegrityError):
            authority.begin_continuation_primary(PreparedContinuationProvider(foreign, dispatch))
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_prepared_input_refuses_coherently_rehashed_foreign_wire_after_positive(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.continuation_primary import prepare_primary_provider_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction

    kwargs, produced, _recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    selected = kwargs["selected"]
    request = kwargs["service"]._provider_gateway.prepare_continuation_primary(list(produced.messages), [])
    text = continuation_provider_input_json(request.kwargs)
    args = {
        "input_sha256": request.input_sha256,
        "input_json": text,
        "snapshot_json": encode_continuation_snapshot(selected.stack),
        "expected_checkpoint_id": selected.checkpoint_id,
        "expected_cursor": selected.stack.cursor,
    }
    try:
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            positive = conn.begin_nested()
            actual = prepare_primary_provider_on_connection(conn, running, **args)
            assert actual.input_sha256 == request.input_sha256
            positive.rollback()
            changed = json.loads(text)
            changed["messages"][-1]["content"] += " changed source"
            negative = conn.begin_nested()
            with pytest.raises(AuditIntegrityError, match="exact selected wire/cache projection"):
                prepare_primary_provider_on_connection(
                    conn,
                    running,
                    **{
                        **args,
                        "input_sha256": stable_hash(changed),
                        "input_json": canonical_json(changed),
                    },
                )
            negative.rollback()
            assert conn.execute(select(effects)).all() == []
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_next_actual_primary_extends_real_applied_p4_history_and_cold_current_graph(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_application_graph import ApplicationGraph, encode_application_graph
    from elspeth.web.composer.continuation_batch_data import RestoredBatchData

    def remove_appended_call(original: ApplicationGraph, appended: ApplicationGraph) -> ApplicationGraph:
        # Project the one actual new call away without minting live timing or
        # inferring original recorder aliases from equal field values.
        assert len(appended.recorder.llm_calls) == len(original.recorder.llm_calls) + 1
        prefix = replace(appended.recorder, llm_calls=appended.recorder.llm_calls[:-1])
        assert encode_recorder_snapshot(prefix) == encode_recorder_snapshot(original.recorder)
        shared = None
        for old, current in zip(original.cuts, appended.cuts, strict=True):
            for old_recorder, current_recorder in (
                (old.recorder_before, current.recorder_before),
                (old.recorder_after, current.recorder_after),
            ):
                if original.recorder is old_recorder:
                    assert encode_recorder_snapshot(prefix) == encode_recorder_snapshot(current_recorder)
                    if shared is not None:
                        assert shared is current_recorder
                    shared = current_recorder
        return replace(appended, recorder=prefix if shared is None else shared)

    kwargs, _produced, recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch, post_p4=True)
    running = kwargs["inventory"].running
    try:
        assert result is not None and len(sdk) == 1
        old_frame = kwargs["selected"].stack.frames[0]
        old_chunk = next(chunk for chunk in kwargs["selected"].chunks if chunk.reference == old_frame.evidence)
        old_evidence = decode_turn_evidence(old_chunk, env_ref_names=kwargs["inventory"].names)
        assert old_evidence.application_entry is not None and old_evidence.joint_graph_json is None
        before = require_evidence_application_links(old_evidence, env_ref_names=kwargs["inventory"].names)
        assert before is not None and before.cuts and before.selected.batch.phase == "complete"
        work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=kwargs["authority"], running=running, selected=result.snapshot, required_work=work
        )
        inventory = await materialize_continuation_env_inventory(
            advisor=inventory_advisor(operation_store[3]), authority=kwargs["authority"], selected=selected, required_work=work
        )
        # This ordinary reader recomputes every actual prior SQL receipt. The
        # new provider call must coexist with that complete source admission.
        cold = await restore_turn_evidence(
            authority=kwargs["authority"], selected=selected, inventory=inventory, required_work=work, service=kwargs["service"]
        )
        read = await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        work.coordinator.assert_completed()
        new_frame = selected.stack.frames[0]
        new_evidence = decode_turn_evidence(
            next(chunk for chunk in selected.chunks if chunk.reference == new_frame.evidence), env_ref_names=inventory.names
        )
        projected = append_primary_call_to_evidence(
            old_evidence, call=result.outcome.audit_call, recorder=result.buffer, env_ref_names=inventory.names
        )
        assert encode_turn_evidence(projected, env_ref_names=inventory.names) == encode_turn_evidence(
            new_evidence, env_ref_names=inventory.names
        )
        copied_call = replace(result.outcome.audit_call)
        assert copied_call == result.outcome.audit_call and copied_call is not result.outcome.audit_call
        with pytest.raises(AuditIntegrityError, match="exact original recorder prefix"):
            append_primary_call_to_evidence(old_evidence, call=copied_call, recorder=result.buffer, env_ref_names=inventory.names)
        assert new_evidence.application_entry is not None and new_evidence.joint_graph_json is None
        assert new_evidence.application_entry.applications == old_evidence.application_entry.applications
        assert new_evidence.historical_application_count == old_evidence.historical_application_count
        assert new_evidence.effect_locators == old_evidence.effect_locators
        admit_journal_values(new_evidence.application_entry, env_ref_names=inventory.names)
        after = require_evidence_application_links(new_evidence, env_ref_names=inventory.names)
        assert after is not None and len(after.cuts) == len(before.cuts)
        assert after.effects is after.cuts[-1].effects_after
        assert after.selected.accumulator is after.cuts[-1].data.accumulator
        assert after.selected.calls is after.cuts[-1].data.calls
        historical = remove_appended_call(before, after)
        assert encode_application_graph(before, env_ref_names=inventory.names) == encode_application_graph(
            historical, env_ref_names=inventory.names
        )
        # The pure complete owner must also distinguish equal but nonshared
        # tuple roots: equality alone would miss this historical alias drift.
        equal_calls = (*historical.selected.calls,)
        assert equal_calls == historical.selected.calls and equal_calls is not historical.selected.calls
        split_alias = replace(
            historical, selected=RestoredBatchData(historical.selected.batch, historical.selected.accumulator, equal_calls)
        )
        assert encode_application_graph(split_alias, env_ref_names=inventory.names) != encode_application_graph(
            before, env_ref_names=inventory.names
        )
        for prior, retained in zip(before.cuts, after.cuts, strict=True):
            assert encode_recorder_snapshot(prior.recorder_before) == encode_recorder_snapshot(retained.recorder_before)
            assert encode_recorder_snapshot(prior.recorder_after) == encode_recorder_snapshot(retained.recorder_after)
            for old, current in zip(retained.recorder_after.invocations, after.recorder.invocations, strict=False):
                assert current is old
            for old, current in zip(retained.recorder_after.visible_tool_events, after.recorder.visible_tool_events, strict=False):
                assert current.event is old.event and current.invocation_index == old.invocation_index
        assert after.recorder.drained == before.recorder.drained
        assert after.recorder.llm_calls[:-1] == before.recorder.llm_calls
        assert len(after.recorder.llm_calls) == len(before.recorder.llm_calls) + 1
        assert after.recorder.llm_calls[-1] == result.outcome.audit_call
        assert result.outcome.audit_call is recorder.llm_calls[-1]
        assert encode_recorder_snapshot(after.recorder) == result.recorder == read.recorder
        assert cold.evidence._portable_p4_graph is not None
        assert cold.evidence.effects is cold.evidence._portable_p4_graph.effects
        assert cold.recorder.invocations[0] is cold.evidence._portable_p4_graph.recorder.invocations[0]
        assert cold.recorder.llm_calls[-1] == result.outcome.audit_call
        # Exercise the paired pure joint source using the actual pre-application
        # snapshot. This grants no SDK/right to invoke at that pending cut.
        joint = kwargs["prior_joint_snapshot"]
        assert joint.joint_graph_json is not None and joint.application_entry is None
        original_joint = require_evidence_application_links(joint, env_ref_names=inventory.names)
        assert original_joint is not None and not original_joint.cuts
        original_buffer = recorder_from_evidence_json(joint.recorder_json)
        joint_buffer = replace(original_buffer, llm_calls=(*original_buffer.llm_calls, result.outcome.audit_call))
        extended_joint = append_primary_call_to_evidence(
            joint, call=result.outcome.audit_call, recorder=joint_buffer, env_ref_names=inventory.names
        )
        new_joint = require_evidence_application_links(extended_joint, env_ref_names=inventory.names)
        assert extended_joint.application_entry is None and extended_joint.joint_graph_json is not None
        assert new_joint is not None and not new_joint.cuts and new_joint.selected.batch.phase == "p4_pending"
        assert encode_application_graph(original_joint, env_ref_names=inventory.names) == encode_application_graph(
            remove_appended_call(original_joint, new_joint), env_ref_names=inventory.names
        )
        assert encode_recorder_snapshot(new_joint.recorder) == encode_recorder_snapshot(joint_buffer)
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ("shared_chunk", "descriptor"))
async def test_owned_primary_utf8_failure_keeps_original_cause_after_real_positive(operation_store, tmp_path, monkeypatch, source_kind):
    from hashlib import sha256

    from elspeth.web.sessions.continuation import _require_current_continuation_running
    from elspeth.web.sessions.continuation_primary import _chunk

    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=kwargs["authority"], running=running, selected=result.snapshot, required_work=work
        )
        assert (
            await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        ).response == result.response
        work.coordinator.assert_completed()
        frame = selected.stack.frames[-1]
        key = private_chunk_key(result.response) if source_kind == "shared_chunk" else f"provider-input:{frame.provider_input_sha256}"
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            if source_kind == "shared_chunk":
                assert _chunk(conn, running, result.response.reference) == result.response
            change = conn.begin_nested()
            payload = b"\xff"
            conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(operation_store[4]),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == key,
                )
                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
            # The same current metadata and key remain; only complete UTF-8
            # content is corrupt. Own-row hashes are deliberately coherent.
            before = tuple(
                tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
                for table in (jobs, chunks, checkpoints, events, effects, physical, usage)
            )
            with pytest.raises(AuditIntegrityError) as caught:
                if source_kind == "shared_chunk":
                    _chunk(conn, running, result.response.reference)
                else:
                    from elspeth.web.sessions.continuation_primary import read_primary_provider_on_connection

                    read_primary_provider_on_connection(
                        conn,
                        running,
                        selected=selected,
                        checkpoint_id=selected.checkpoint_id,
                        cursor=selected.stack.cursor,
                        effect_key=frame.provider_effect_key,
                        input_sha256=frame.provider_input_sha256,
                    )
            assert type(caught.value) is AuditIntegrityError
            assert type(caught.value.__cause__) is UnicodeDecodeError and caught.value.__cause__.object == payload
            assert (
                tuple(
                    tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
                    for table in (jobs, chunks, checkpoints, events, effects, physical, usage)
                )
                == before
            )
            change.rollback()
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ("logical", "state_chunk"))
async def test_primary_cold_read_binds_complete_supplied_selection_after_real_positive(operation_store, tmp_path, monkeypatch, drift):
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.sessions.continuation_selection import required_private_chunks

    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=kwargs["authority"], running=running, selected=result.snapshot, required_work=work
        )
        assert (
            await read_continuation_primary(authority=kwargs["authority"], selected=selected, required_work=work)
        ).response == result.response
        root = selected.stack
        values = selected.chunks
        if drift == "logical":
            root = root.model_copy(update={"logical": root.logical.model_copy(update={"request_hash": "another-valid-owned-request-hash"})})
        else:
            frame = root.frames[0]
            state = next(chunk for chunk in values if chunk.reference == frame.state)
            changed_state = json.loads(state.canonical)
            changed_state["metadata"]["description"] += " changed selected source"
            chunk = encode_private_chunk(codec="composer.state.v1", canonical=canonical_json(changed_state))
            root = root.model_copy(update={"frames": (frame.model_copy(update={"state": chunk.reference}),)})
            by_ref = {item.reference: item for item in (*values, chunk)}
            values = tuple(by_ref[ref] for ref in required_private_chunks(root))
        supplied = SelectedContinuationState(selected.checkpoint_id, root, values)
        assert supplied.stack.cursor == selected.stack.cursor and supplied != selected
        before = stored_rows(operation_store[0])
        with pytest.raises(ContinuationCASConflict):
            await read_continuation_primary(authority=kwargs["authority"], selected=supplied, required_work=work)
        work.coordinator.assert_completed()
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)
