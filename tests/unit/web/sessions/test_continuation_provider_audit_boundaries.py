"""Original-checkpoint SQL controls for Stop, released fences and private DATA.

Reuse the real continuation store/admission/checkpoint owners. The provider
record is the project's existing owned private-record fixture; these are
focused SQL proofs, not additional full app/SDK runtime qualification.
"""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.provider_audit_source import capture_provider_audit_source, provider_attempt_audit_drafts_for_source
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation_provider_audit import continuation_provider_audit_drafts_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.composer.test_continuation_physical_outcomes import private_record
from tests.unit.web.coordination import test_composer_operation_authority as operation_cases
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows

operation_store = operation_cases.operation_store


async def _checkpoint(store):
    engine, repository, authority, service, _sid = store
    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    admit_continuation(store)
    running = start_continuation(repository, authority)
    work = inventory_work(running)
    try:
        attempt = await service.begin_provider_attempt(
            session_operation_context=running.session_operation_context,
            source="composer",
            required_work=work.coordinator.reserve(RequiredWorkSource.PROVIDER_ADMISSION_SQL),
        )
        call = replace(private_record(), call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at)
        await service.finish_provider_attempt(
            session_operation_context=running.session_operation_context,
            call=call,
            required_work=work.coordinator.reserve(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL),
        )
    except BaseException:
        repository.release(running.session_operation_context)
        raise
    finally:
        work.coordinator.assert_completed()
    return running, call


async def _proof(store, owner, call, *, scope_owner=None):
    service = store[3]
    sid = str(store[4])
    work = inventory_work(owner if scope_owner is None else scope_owner)

    def read():
        with service._session_process_locked_begin(sid) as conn, service._session_write_lock(conn, sid):
            service._assert_session_write_lock_held(conn, sid, caller="original-checkpoint-boundary-test")
            return continuation_provider_audit_drafts_on_connection(conn, owner, calls=(call,))

    try:
        return await _registered(work=work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=tuple)
    finally:
        work.coordinator.assert_completed()


async def _handover(store, first):
    receipt = await asyncio.to_thread(store[1].suspend_composer_segment, first)
    current_store = new_owners(store)
    current = await asyncio.to_thread(start_continuation, current_store[1], current_store[2])
    assert (receipt.sequence, receipt.previous_generation, receipt.generation) == (
        first.claim.segment_sequence,
        first.claim.ownership_generation,
        first.claim.ownership_generation + 1,
    )
    assert receipt.released_context is first.session_operation_context
    assert (current.claim.segment_sequence, current.claim.ownership_generation) == (receipt.sequence, receipt.generation)
    return current_store, current


@pytest.mark.asyncio
async def test_durable_stop_refuses_original_checkpoint_reproof_without_publication(operation_store):
    running, call = await _checkpoint(operation_store)
    repository, authority = operation_store[1:3]
    try:
        before = recovery_rows(operation_store[0])
        drafts = await _proof(operation_store, running, call)
        assert drafts == provider_attempt_audit_drafts_for_source(call, capture_provider_audit_source(running.session_operation_context))
        assert recovery_rows(operation_store[0]) == before
        stopped = await asyncio.to_thread(
            authority.request_cancel, session_id=running.claim.session_id, operation_id=running.claim.operation_id
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        after_stop = recovery_rows(operation_store[0])
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await _proof(operation_store, running, call)
        assert recovery_rows(operation_store[0]) == after_stop
        durable = authority.get_for_turn(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        assert durable is not None and durable.cancel_requested_at == stopped.cancel_requested_at
        assert durable.status == "running"
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_handover_release_cannot_authorize_checkpoint_omission(operation_store):
    first, call = await _checkpoint(operation_store)
    current_store = None
    current = None
    released = False
    try:
        assert await _proof(operation_store, first, call)
        current_store, current = await _handover(operation_store, first)
        released = True
        before = recovery_rows(operation_store[0])
        # A genuine current read scope does not turn an actual old/released
        # running object into current write authority.
        with pytest.raises(ComposerOperationFenceLost) as refused:
            await _proof(current_store, first, call, scope_owner=current)
        assert type(refused.value) is ComposerOperationFenceLost
        assert (refused.value.session_id, refused.value.operation_id) == (first.claim.session_id, first.claim.operation_id)
        assert recovery_rows(operation_store[0]) == before
        assert await _proof(current_store, current, call) == ()
        assert recovery_rows(operation_store[0]) == before
    finally:
        if current_store is not None and current is not None:
            current_store[1].release(current.session_operation_context)
        if not released:
            operation_store[1].release(first.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("received_response", "provider_failure"))
@pytest.mark.parametrize("mutation", ("missing", "coherent-payload", "coherent-epoch"))
async def test_historical_omission_reproves_private_response_and_failure_after_positive(operation_store, channel, mutation):
    first, call = await _checkpoint(operation_store)
    current_store = None
    current = None
    released = False
    try:
        source = capture_provider_audit_source(first.session_operation_context)
        drafts = provider_attempt_audit_drafts_for_source(call, source)
        private_drafts = [draft for draft in drafts if draft.tool_calls is not None and len(draft.tool_calls) == 1]
        assert {draft.tool_calls[0]["evidence"]["status"] for draft in private_drafts} == {
            "provider_request",
            "received_response",
            "provider_failure",
        }
        assert await _proof(operation_store, first, call) == drafts
        current_store, current = await _handover(operation_store, first)
        released = True
        before = recovery_rows(operation_store[0])
        assert await _proof(current_store, current, call) == ()
        assert recovery_rows(operation_store[0]) == before
        (target,) = [draft for draft in private_drafts if draft.tool_calls[0]["evidence"]["status"] == channel]
        with operation_store[0].begin() as conn:
            rows = conn.execute(select(messages).where(messages.c.session_id == str(current.claim.session_id))).all()
            matches = [row for row in rows if deep_thaw(row.tool_calls) == deep_thaw(target.tool_calls)]
            assert len(matches) == 1
            (row,) = matches
            # The same selector must reject the public pair, controlling its
            # discriminator before relying on the negative mutation.
            assert all(deep_thaw(other.tool_calls) != deep_thaw(target.tool_calls) for other in rows if len(other.tool_calls or ()) == 2)
            if mutation == "missing":
                changed = None
            else:
                changed = deep_thaw(target.tool_calls)
                envelope = changed[0]
                value = envelope["evidence"]
                if mutation == "coherent-payload":
                    assert value["result"]["content"] is not None
                    replacement = canonical_json({"changed": "private fixture payload"})
                    value["result"]["content"] = replacement
                    value["result"]["retained_sha256"] = sha256(replacement.encode("utf-8")).hexdigest()
                else:
                    envelope["operation_epoch"] += 1000
                    value["operation_epoch"] = envelope["operation_epoch"]
                envelope["evidence_sha256"] = sha256(canonical_json(value).encode("utf-8")).hexdigest()
                parsed = ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=str(current.claim.session_id))
                assert parsed.status == channel
            assert conn.execute(update(messages).where(messages.c.id == row.id).values(tool_calls=changed)).rowcount == 1
            assert deep_thaw(conn.execute(select(messages.c.tool_calls).where(messages.c.id == row.id)).scalar_one()) == changed
        corrupted = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="checkpoint private"):
            await _proof(current_store, current, call)
        assert recovery_rows(operation_store[0]) == corrupted
    finally:
        if current_store is not None and current is not None:
            current_store[1].release(current.session_operation_context)
        if not released:
            operation_store[1].release(first.session_operation_context)
