"""Real P4 writer, current names producer and checked application-input reads."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_owed_p4 as owed
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_owed_p4 import materialize_owed_p4_env_inventory, read_selected_owed_p4_result
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent, append_complete_snapshot
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result
from elspeth.web.sessions.models import chat_messages_table, sessions_table
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import session_operation_fences_table as fences
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_p4_owner import persist, recovered, rows, selected_p4

operation_store = test_composer_operation_authority.operation_store


async def committed_case(store, *, graph=False, crash_pending=False, unavailable=False, revoked=False, monkeypatch=None):
    engine, _repository, authority, service, sid = store
    running, prepared, commit = selected_p4(store, graph=graph, crash_pending=crash_pending)
    if revoked:
        with engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(trust_mode="explicit_approve"))
    if unavailable:
        assert monkeypatch is not None
        failure = OperationalError("controlled unavailable", {}, RuntimeError("storage unavailable"))

        def refuse_insert(*args, **kwargs):
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr(service, "_insert_chat_message", refuse_insert)
            audit = await asyncio.to_thread(persist, service, running, prepared, commit)
    else:
        audit = await asyncio.to_thread(persist, service, running, prepared, commit)
    stored = authority.load_continuation_snapshot(running)
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    work.coordinator.assert_completed()
    return running, prepared, commit, audit, selected


def configured_advisor(store, monkeypatch):
    engine, _repository, _authority, sessions, _sid = store
    monkeypatch.setenv("OWED_CURRENT_NAME", "offline-test-value")
    resolver = ScopedSecretResolver(
        WebSecretService(
            user_store=UserSecretStore(engine=engine, master_key="inventory-test-master-key-32chars!"),
            server_store=ServerSecretStore(allowlist=("OWED_CURRENT_NAME",)),
        ),
        "local",
    )

    def refuse_resolution(*args, **kwargs):
        raise AssertionError("owed inventory resolved a secret")

    monkeypatch.setattr(resolver, "resolve", refuse_resolution)
    monkeypatch.setattr(resolver, "resolve_scoped", refuse_resolution)
    return inventory_advisor(sessions, secret_service=resolver)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("ordinary", "cancel", "segment", "both"))
@pytest.mark.parametrize("graph", (False, True))
async def test_actual_owed_result_reads_after_cancel_or_segment_without_repeating_writer(operation_store, monkeypatch, cut, graph):
    engine, repository, authority, _service, sid = operation_store
    running, _prepared, commit, audit, selected = await committed_case(operation_store, graph=graph)
    try:
        expected = recovered(authority, running, commit)
        with engine.begin() as conn:
            now = database_now(conn)
            if cut in ("cancel", "both"):
                conn.execute(update(jobs).where(jobs.c.session_id == str(sid)).values(cancel_requested_at=now))
            if cut in ("segment", "both"):
                conn.execute(
                    update(segments)
                    .where(segments.c.session_id == str(sid))
                    .values(
                        started_at=now - timedelta(seconds=3),
                        cooperative_at=now - timedelta(seconds=2),
                        deadline_at=now - timedelta(seconds=1),
                    )
                )
        advisor = configured_advisor(operation_store, monkeypatch)
        work = inventory_work(running)
        if cut != "ordinary":
            with pytest.raises((ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost)):
                await materialize_continuation_env_inventory(advisor=advisor, authority=authority, selected=selected, required_work=work)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        assert inventory.running is running and inventory.names == frozenset({"OWED_CURRENT_NAME"})
        originals = []
        read = owed.read_p4_result_on_connection

        def observe(conn, owner, **kwargs):
            assert conn.in_transaction() and owner is running
            assert kwargs["env_ref_names"] is inventory.names
            result = read(conn, owner, **kwargs)
            originals.append(result)
            return result

        monkeypatch.setattr(owed, "read_p4_result_on_connection", observe)
        before = rows(engine)
        result = await read_selected_owed_p4_result(
            authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
        )
        assert type(result) is ReadmittedP4Result and result is originals[0] and result == expected
        assert result.outcome.assistant_id == audit.assistant_id
        assert result.outcome.published_assistant_content == audit.published_assistant_content
        assert result.outcome.checkpoint_disposition == audit.checkpoint_disposition
        assert rows(engine) == before
        work.coordinator.assert_completed()
        assert all(not ticket.permits_process_drain for ticket in work.coordinator.tickets)
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("crash_committed", "crash_unavailable", "revoked"))
async def test_actual_failure_and_revocation_outcomes_are_retained_as_data(operation_store, monkeypatch, kind):
    engine, repository, authority, sessions, _sid = operation_store
    running, _prepared, commit, audit, selected = await committed_case(
        operation_store,
        graph=kind == "revoked",
        crash_pending=kind != "revoked",
        unavailable=kind == "crash_unavailable",
        revoked=kind == "revoked",
        monkeypatch=monkeypatch,
    )
    try:
        expected = recovered(authority, running, commit)
        work = inventory_work(running)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        before = rows(engine)
        result = await read_selected_owed_p4_result(
            authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
        )
        assert result == expected and rows(engine) == before
        assert result.outcome.unwind_audit_failed == audit.unwind_audit_failed
        assert result.outcome.checkpoint_disposition == audit.checkpoint_disposition
        if kind == "crash_unavailable":
            assert result.receipt.chat_message_ids == result.receipt.inserted_state_ids == ()
            assert result.outcome.assistant_id is None and result.historical_effect is None
        elif kind == "revoked":
            assert result.outcome.authority_revocation_json is not None
            assert result.outcome.published_assistant_content == "" and result.historical_effect is None
        else:
            assert result.receipt.invocation_ids == ("owned-p4-call",)
        work.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("running_identity", "snapshot", "lease", "claim", "raw_content"))
async def test_final_owed_read_rechecks_inventory_live_authority_and_actual_rows(operation_store, change):
    engine, repository, authority, sessions, sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        work = inventory_work(running)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        assert await read_selected_owed_p4_result(
            authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
        ) == recovered(authority, running, commit)
        if change == "running_identity":
            inventory = replace(inventory, running=replace(running))
        elif change == "snapshot":
            inventory = replace(inventory, selection=replace(inventory.selection, snapshot_json=inventory.selection.snapshot_json + " "))
        elif change == "claim":
            wrong = replace(running, claim=replace(running.claim, claim_token="wrong-token"))
            work = replace(work, running=wrong)
            inventory = replace(inventory, running=wrong)
        else:
            with engine.begin() as conn:
                if change == "lease":
                    conn.execute(
                        update(fences)
                        .where(fences.c.session_id == str(sid))
                        .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                    )
                else:
                    conn.execute(
                        update(chat_messages_table)
                        .where(chat_messages_table.c.session_id == str(sid), chat_messages_table.c.role == "assistant")
                        .values(raw_content="changed retained raw content")
                    )
        before = rows(engine)
        with pytest.raises((AuditIntegrityError, ComposerOperationFenceLost)):
            await read_selected_owed_p4_result(
                authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
            )
        assert rows(engine) == before
        work.coordinator.assert_completed()
    finally:
        if change == "lease":
            with pytest.raises(SessionOperationFenceLost) as caught:
                repository.release(running.session_operation_context)
            assert caught.value.reason is FenceLossReason.LEASE_EXPIRED
        else:
            repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_names_lookup_is_bracketed_by_actual_pending_result_selection(operation_store, monkeypatch):
    engine, repository, authority, sessions, sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        advisor = configured_advisor(operation_store, monkeypatch)
        work = inventory_work(running)
        positive = await materialize_owed_p4_env_inventory(
            advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        assert positive.names == frozenset({"OWED_CURRENT_NAME"})
        actual = sessions.get_session
        lookups = []

        async def observe(session_id):
            result = await actual(session_id)
            lookups.append(result)
            with engine.begin() as conn:
                append_complete_snapshot(
                    conn,
                    session_id=str(sid),
                    operation_id=running.claim.operation_id,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.stack.cursor,
                    snapshot_json=positive.selection.snapshot_json,
                    event_batch=(ContinuationEvent("checkpoint", '{"owed_inventory_advanced":true}'),),
                    handoff=False,
                )
            return result

        monkeypatch.setattr(sessions, "get_session", observe)
        with pytest.raises(ContinuationCASConflict):
            await materialize_owed_p4_env_inventory(
                advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
            )
        assert len(lookups) == 1
        work.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pending_result_effect_binding_is_checked_before_metadata(operation_store, monkeypatch):
    engine, repository, authority, sessions, sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        advisor = configured_advisor(operation_store, monkeypatch)
        work = inventory_work(running)
        await materialize_owed_p4_env_inventory(
            advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        with engine.begin() as conn:
            conn.execute(
                update(effects)
                .where(effects.c.session_id == str(sid), effects.c.effect_key == commit.effect_key)
                .values(result_chunk_key=f"snapshot:{selected.checkpoint_id}")
            )
        lookups = []
        actual = sessions.get_session

        async def observe(session_id):
            lookups.append(session_id)
            return await actual(session_id)

        monkeypatch.setattr(sessions, "get_session", observe)
        with pytest.raises(AuditIntegrityError, match="committed result binding"):
            await materialize_owed_p4_env_inventory(
                advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
            )
        assert lookups == []
        work.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("inventory", "result"))
async def test_owed_read_admission_cancel_keeps_original_and_closes_projection(operation_store, monkeypatch, cut):
    from elspeth.web import async_workers

    _engine, repository, authority, sessions, _sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        advisor = inventory_advisor(sessions)
        work = inventory_work(running)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        actual = async_workers._acquire_admission
        entered = asyncio.Event()
        originals = []

        async def observe():
            entered.set()
            try:
                await actual()
            except asyncio.CancelledError as original:
                originals.append(original)
                raise

        monkeypatch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
        monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
        monkeypatch.setattr(async_workers, "_acquire_admission", observe)
        pending = (
            materialize_owed_p4_env_inventory(
                advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
            )
            if cut == "inventory"
            else read_selected_owed_p4_result(
                authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
            )
        )
        task = asyncio.create_task(pending)
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            task.cancel("owed read admission cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert len(originals) == 1 and caught.value is originals[0]
        work.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("absent", "other_kind"))
async def test_owed_names_refuse_unselected_result_families_before_lookup(operation_store, monkeypatch, change):
    engine, repository, authority, sessions, sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        advisor = configured_advisor(operation_store, monkeypatch)
        work = inventory_work(running)
        await materialize_owed_p4_env_inventory(
            advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        pending = (
            ()
            if change == "absent"
            else tuple(
                item.model_copy(update={"kind": "read"}) if item.effect_key == commit.effect_key else item
                for item in selected.stack.pending_owner_results
            )
        )
        following = selected.stack.model_copy(update={"pending_owner_results": pending})
        with engine.begin() as conn:
            stored = append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encode_continuation_snapshot(following),
                event_batch=(ContinuationEvent("checkpoint", '{"changed_pending_family":true}'),),
                handoff=False,
            )
        selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
        lookups = []
        actual = sessions.get_session

        async def observe(session_id):
            lookups.append(session_id)
            return await actual(session_id)

        monkeypatch.setattr(sessions, "get_session", observe)
        with pytest.raises(AuditIntegrityError, match="pending persisted result"):
            await materialize_owed_p4_env_inventory(
                advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
            )
        assert lookups == []
        work.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_owed_inventory_requires_live_lease_before_names_lookup(operation_store, monkeypatch):
    engine, repository, authority, sessions, sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        advisor = configured_advisor(operation_store, monkeypatch)
        work = inventory_work(running)
        await materialize_owed_p4_env_inventory(
            advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        with engine.begin() as conn:
            conn.execute(
                update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
            )
        calls = []
        actual = sessions.get_session

        async def observe(session_id):
            calls.append(session_id)
            return await actual(session_id)

        monkeypatch.setattr(sessions, "get_session", observe)
        with pytest.raises(ComposerOperationFenceLost):
            await materialize_owed_p4_env_inventory(
                advisor=advisor, authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
            )
        assert calls == []
        work.coordinator.assert_completed()
    finally:
        with pytest.raises(SessionOperationFenceLost) as caught:
            repository.release(running.session_operation_context)
        assert caught.value.reason is FenceLossReason.LEASE_EXPIRED


@pytest.mark.asyncio
async def test_final_owed_read_binds_full_supplied_selected_root(operation_store):
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_selection import SelectedContinuationState

    _engine, repository, authority, sessions, _sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        work = inventory_work(running)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        assert await read_selected_owed_p4_result(
            authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
        ) == recovered(authority, running, commit)
        changed_logical = selected.stack.logical.model_copy(update={"request_hash": "f" * 64})
        altered = selected.stack.model_copy(update={"logical": changed_logical})
        # Run the real complete value admission and nominal cohort constructor.
        valid = decode_continuation_snapshot(encode_continuation_snapshot(altered))
        substituted = SelectedContinuationState(selected.checkpoint_id, valid, selected.chunks)
        assert substituted.stack.cursor == selected.stack.cursor and substituted.chunks == selected.chunks
        with pytest.raises(AuditIntegrityError, match="supplied complete selected root"):
            await read_selected_owed_p4_result(
                authority=authority, selected=substituted, required_work=work, inventory=inventory, effect_key=commit.effect_key
            )
        work.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("result", "accumulator", "response"))
async def test_final_owed_read_classifies_each_coherent_storage_utf8_failure(operation_store, channel):
    from hashlib import sha256

    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    engine, repository, authority, sessions, sid = operation_store
    running, _prepared, commit, _audit, selected = await committed_case(operation_store)
    try:
        work = inventory_work(running)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work, effect_key=commit.effect_key
        )
        assert await read_selected_owed_p4_result(
            authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
        ) == recovered(authority, running, commit)
        frame = selected.stack.frames[-1]
        assert type(frame) is ToolBatchFrame
        if channel == "result":
            reference = next(item.result for item in selected.stack.pending_owner_results if item.effect_key == commit.effect_key)
        elif channel == "accumulator":
            reference = frame.accumulator
        else:
            reference = frame.response
        payload = bytes((255,))
        with engine.begin() as conn:
            conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(sid),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                )
                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
        with pytest.raises(AuditIntegrityError, match="strict UTF-8") as caught:
            await read_selected_owed_p4_result(
                authority=authority, selected=selected, required_work=work, inventory=inventory, effect_key=commit.effect_key
            )
        assert type(caught.value.__cause__) is UnicodeDecodeError
        assert caught.value.__cause__.object == payload and caught.value.__cause__.encoding == "utf-8"
        work.coordinator.assert_completed()
        assert any(item.original_root is caught.value for item in work.coordinator.failure_receipts())
    finally:
        repository.release(running.session_operation_context)
