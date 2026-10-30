"""Real paid TITLE SUCCESS → one current atomic title/ready writer."""

import asyncio
from contextlib import suppress
from dataclasses import replace
from hashlib import sha256
from threading import Event
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web import async_workers
from elspeth.web.composer import continuation_title_application as application
from elspeth.web.composer import continuation_title_provider as provider
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_title_values import decode_title_provider_result
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.continuation_retention_memo import current_retention_stage_memo
from elspeth.web.required_work import RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions import continuation_title_application as owner
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationCASConflict
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import metadata
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.protocol import SessionRecord
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.continuation_memo import RetentionMemoTrace
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_title_provider import actual_title_update, observer, raw_reply, sql_rows, title_case

operation_store = operations.operation_store


async def paid_title(store, tmp_path, monkeypatch, content="Original Café Session Title"):
    running, service, work, prepared, guard_requests = await title_case(store, tmp_path, monkeypatch)
    calls = observer(monkeypatch)
    sdk = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            store[0],
            running.session_operation_context,
            guard_requests,
            role="composer",
            user_prompt="Preserve café and original context",
        )
        sdk.append(kwargs)
        return raw_reply(kwargs["model"], content)

    monkeypatch.setattr(litellm, "acompletion", completion)
    original = await provider.run_prepared_title_provider(
        prepared=prepared, invocation_owner=ProviderInvocationOwner(service=store[3], required_work=work)
    )
    current = await provider.read_current_title_result(service=service, running=running, required_work=work)
    assert original is not None and current.settled and len(calls) == len(sdk) == 1
    assert original.audit_call is calls[0] and current.settlement.result == original.result
    return running, service, work, current, original, sdk, calls


async def write_request(store, work, request):
    sql, projection = work.reserve_pair(RequiredWorkSource.TITLE_ACCOUNTING_SQL, RequiredWorkSource.TITLE_ACCOUNTING_PROJECTION)
    try:
        result = await store[3].apply_continuation_title_application(request, required_work=sql)
    except BaseException:
        if sql.complete:
            projection.complete_without_submission()
        raise
    projection.begin_projection()
    projection.complete_owned()
    return result


async def reached_or_failure(task, reached):
    waiter = asyncio.create_task(reached.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            raise AssertionError("Actual TITLE owner returned before its selected fixture boundary")
        if waiter not in done:
            raise TimeoutError("Actual TITLE owner did not reach its selected fixture boundary")
    finally:
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,disposition",
    [
        pytest.param("Original Café Session Title", "accepted", id="accepted"),
        pytest.param("Rejected/Title Candidate", "accepted", id="slash"),
        pytest.param(None, "empty_response", id="none"),
        pytest.param("", "rejected", id="empty"),
        pytest.param("Rejected! Title Candidate", "rejected", id="invalid"),
    ],
)
async def test_paid_title_application_preserves_original_result_and_applies_exact_title_once(
    operation_store, tmp_path, monkeypatch, content, disposition, request
):
    running, service, work, settled, original, sdk, calls = await paid_title(operation_store, tmp_path, monkeypatch, content)
    memo_trace = RetentionMemoTrace(monkeypatch, request)
    rejections = []
    real_rejection_metric = application._record_auto_title_rejection

    def observe_rejection(*args):
        rejections.append(args)
        real_rejection_metric(*args)

    monkeypatch.setattr(application, "_record_auto_title_rejection", observe_rejection)
    try:
        await actual_title_update(operation_store, running, work, "Renamed Before Title Application")
        before = decode_continuation_snapshot(settled.selected.payload_json)
        published = await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert published.applied and published.decision.disposition == disposition
        after = decode_continuation_snapshot(published.selected.payload_json)
        frame = after.frames[0]
        assert frame.ingress_phase == "ready" and frame.provider_effect_key is frame.provider_input_sha256 is None
        assert frame.title_response == before.frames[0].title_response and frame.title_attempt_id == before.frames[0].title_attempt_id
        assert after.model_copy(update={"cursor": before.cursor, "frames": before.frames}) == before
        assert published.selected.cursor == settled.selected.cursor + 1
        assert published.result == original.result and len(sdk) == len(calls) == 1
        decoded = decode_title_provider_result(published.result, env_ref_names=published.inventory.names)
        assert decoded.audit_call == calls[0] and decoded.completion.message.content == content
        assert (await operation_store[3].get_session(operation_store[4])).title == (
            content if disposition == "accepted" else "Renamed Before Title Application"
        )
        rows, ledger, _messages = sql_rows(operation_store)
        assert len(rows) == len(ledger) == 1 and rows[0].ledger_entry_id == ledger[0].entry_id
        assert published.event is not None
        assert rows[0].attempt_id == calls[0].call_id == published.event.attempt_id
        assert ledger[0].entry_id == published.event.ledger_entry_id
        assert published.event.decision == published.decision
        assert len(rejections) == (1 if disposition == "rejected" else 0)
        await actual_title_update(operation_store, running, work, "Later Legitimate Owner Rename")
        current_ready = await application.read_current_title_application(service=service, running=running, required_work=work)
        untouched = recovery_rows(operation_store[0])
        replay_request = owner.TitleApplicationRequest(
            running, current_ready.selected, current_ready.inventory, current_ready.result, current_ready.decision
        )
        await write_request(operation_store, work, replay_request)
        assert recovery_rows(operation_store[0]) == untouched
        replayed = await application.apply_current_title_success(service=service, selected=published.selected, required_work=work)
        assert replayed.applied and replayed.result == original.result and replayed.decision == published.decision
        assert recovery_rows(operation_store[0]) == untouched and len(sdk) == 1
        assert (await operation_store[3].get_session(operation_store[4])).title == "Later Legitimate Owner Rename"
        assert len(rejections) == (1 if disposition == "rejected" else 0)
        policy = decode_continuation_policy(after.admission_policy_json, after.policy_hash)
        structural = await load_selected_continuation(
            authority=operation_store[2], running=running, selected=published.selected, required_work=work
        )
        assert next_continuation_action_policy(selected=structural, policy=policy) == policy.actions.session_sql
        work.coordinator.assert_completed()
        memo_trace.finish(stages=1)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_retention_postwrite_read_error_clears_memo_and_preserves_committed_ready(
    operation_store, tmp_path, monkeypatch, request
):
    running, service, work, settled, original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    memo_trace = RetentionMemoTrace(monkeypatch, request)
    real_read = application.read_current_title_application
    marker = RuntimeError("original post-write TITLE observation failure")
    read_results = []
    committed_rows = []

    async def fail_after_real_read(**kwargs):
        result = await real_read(**kwargs)
        if current_retention_stage_memo() is not None:
            assert result.applied and result.result == original.result
            read_results.append(result)
            committed_rows.append(recovery_rows(operation_store[0]))
            raise marker
        return result

    monkeypatch.setattr(application, "read_current_title_application", fail_after_real_read)
    try:
        with pytest.raises(RuntimeError) as caught:
            await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert caught.value is marker and len(read_results) == 1
        assert recovery_rows(operation_store[0]) == committed_rows[0] and len(sdk) == 1
        work.coordinator.assert_completed()
        memo_trace.assert_closed(stages=1)
        retry_work = inventory_work(running)
        replay = await application.apply_current_title_success(service=service, selected=read_results[0].selected, required_work=retry_work)
        assert replay.applied and replay.selected == read_results[0].selected
        assert replay.result == read_results[0].result and replay.decision == read_results[0].decision
        assert replay.inventory is not read_results[0].inventory
        assert recovery_rows(operation_store[0]) == committed_rows[0]
        assert len(sdk) == 1
        retry_work.coordinator.assert_completed()
        memo_trace.finish(stages=1)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("before_append", "after_append"))
async def test_title_and_ready_rollback_together_after_actual_title_setter(operation_store, tmp_path, monkeypatch, stage):
    running, service, work, settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    original_append = owner.append_complete_snapshot
    marker = RuntimeError("original title/ready atomic suffix failure")
    observed = []

    def fail(conn, **kwargs):
        actual_title = conn.execute(select(sessions.c.title).where(sessions.c.id == str(operation_store[4]))).scalar_one()
        assert actual_title == "Original Café Session Title"
        observed.append(stage)
        if stage == "after_append":
            original_append(conn, **kwargs)
        raise marker

    monkeypatch.setattr(owner, "append_complete_snapshot", fail)
    try:
        before = recovery_rows(operation_store[0])
        with pytest.raises(RuntimeError) as caught:
            await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert caught.value is marker or caught.value.__cause__ is marker
        assert observed == [stage] and recovery_rows(operation_store[0]) == before
        assert len(sdk) == 1
        work.coordinator.assert_completed()
        monkeypatch.setattr(owner, "append_complete_snapshot", original_append)
        retry_work = inventory_work(running)
        assert (await application.apply_current_title_success(service=service, selected=settled.selected, required_work=retry_work)).applied
        assert len(sdk) == 1 and len(sql_rows(operation_store)[1]) == 1
        retry_work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("candidate", "drop_accepted", "result"))
async def test_title_application_cannot_choose_or_discard_actual_paid_candidate(operation_store, tmp_path, monkeypatch, tamper):
    running, service, work, settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    try:
        positive = await application.read_current_title_application(service=service, running=running, required_work=work)
        assert not positive.applied and positive.selected == settled.selected and positive.decision.disposition == "accepted"
        request = owner.TitleApplicationRequest(running, positive.selected, positive.inventory, positive.result, positive.decision)
        if tamper == "candidate":
            request = replace(request, decision=positive.decision.model_copy(update={"candidate": "Caller Chose Another Title"}))
        elif tamper == "drop_accepted":
            request = replace(
                request, decision=owner.TitleApplicationDecision(disposition="empty_response", candidate=None, rejection_class=None)
            )
        else:
            request = replace(request, result=replace(positive.result, canonical=positive.result.canonical + " "))
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="actual result or candidate disposition"):
            await write_request(operation_store, work, request)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_application_current_business_authority_refuses_cancelled_owed_cut(operation_store, tmp_path, monkeypatch):
    running, service, work, settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    try:
        assert not (await application.read_current_title_application(service=service, running=running, required_work=work)).applied
        operation_store[2].request_cancel(
            session_id=operation_store[4], operation_id=running.claim.operation_id, cancelled_failure=operations.cancelled
        )
        before = recovery_rows(operation_store[0])
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await application.read_current_title_application(service=service, running=running, required_work=work)
        assert recovery_rows(operation_store[0]) == before
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        assert (await provider.read_current_title_result(service=service, running=running, required_work=work)).settled
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


def complete_rows_on_connection(conn):
    return tuple(
        (table.name, tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())) for table in metadata.sorted_tables
    )


@pytest.mark.asyncio
async def test_actual_sql_title_admission_rejects_caller_candidate_after_real_positive(operation_store, tmp_path, monkeypatch):
    running, service, work, _settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    try:
        current = await application.read_current_title_application(service=service, running=running, required_work=work)
        request = owner.TitleApplicationRequest(running, current.selected, current.inventory, current.result, current.decision)
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            before = complete_rows_on_connection(conn)
            admitted = owner.admit_title_application_on_connection(conn, operation_store[3], running.session_operation_context, request)
            assert admitted.current.result == current.result
            admitted.active = False
            assert complete_rows_on_connection(conn) == before
            changed = replace(request, decision=current.decision.model_copy(update={"candidate": "Caller Chose Another Title"}))
            with pytest.raises(AuditIntegrityError, match="actual result or candidate disposition"):
                owner.admit_title_application_on_connection(conn, operation_store[3], running.session_operation_context, changed)
            assert complete_rows_on_connection(conn) == before
        assert len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_ready_finisher_requires_the_real_original_title_write(operation_store, tmp_path, monkeypatch):
    running, service, work, _settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    try:
        current = await application.read_current_title_application(service=service, running=running, required_work=work)
        request = owner.TitleApplicationRequest(running, current.selected, current.inventory, current.result, current.decision)
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            admitted = owner.admit_title_application_on_connection(conn, operation_store[3], running.session_operation_context, request)
            before = complete_rows_on_connection(conn)
            try:
                with pytest.raises(AuditIntegrityError, match="actual original title-write/no-change result"):
                    owner.complete_title_application_on_connection(conn, running.session_operation_context, admitted)
                assert complete_rows_on_connection(conn) == before
            finally:
                admitted.active = False
        assert len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_application_current_actor_refusal_keeps_complete_sql(operation_store, tmp_path, monkeypatch):
    running, service, work, settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    try:
        assert not (await application.read_current_title_application(service=service, running=running, required_work=work)).applied
        with operation_store[0].begin() as conn:
            ensure_test_identity(conn, identity_id="other-current-owner")
            conn.execute(update(sessions).where(sessions.c.id == str(operation_store[4])).values(user_id="other-current-owner"))
        before = recovery_rows(operation_store[0])
        with pytest.raises(operations.ComposerOperationFenceLost):
            await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_ready_two_actual_writers_have_one_atomic_commit(operation_store, tmp_path, monkeypatch):
    running, service, work, _settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    first_work, second_work = inventory_work(running), inventory_work(running)
    try:
        current = await application.read_current_title_application(service=service, running=running, required_work=work)
        request = owner.TitleApplicationRequest(running, current.selected, current.inventory, current.result, current.decision)
        outcomes = await asyncio.gather(
            write_request(operation_store, first_work, request),
            write_request(operation_store, second_work, request),
            return_exceptions=True,
        )
        assert sum(type(value) is SessionRecord for value in outcomes) == 1
        assert sum(isinstance(value, ContinuationCASConflict) for value in outcomes) == 1
        ready = await application.read_current_title_application(service=service, running=running, required_work=work)
        assert ready.applied and ready.selected.cursor == current.selected.cursor + 1
        assert len(sdk) == 1 and len(sql_rows(operation_store)[1]) == 1
        for binding in (work, first_work, second_work):
            binding.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


def replace_sql_chunk(conn, key, text, *, session_id, operation_id):
    payload = text.encode("utf-8")
    assert (
        conn.execute(
            update(chunks)
            .where(chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
            .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
        ).rowcount
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("creator", "root", "ledger"))
async def test_current_ready_read_rejects_coherent_source_changes_after_real_positive(operation_store, tmp_path, monkeypatch, tamper):
    running, service, work, settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    try:
        ready = await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert ready.applied and ready.event is not None
        positive = await application.read_current_title_application(service=service, running=running, required_work=work)
        assert positive == ready
        sid, operation = str(operation_store[4]), running.claim.operation_id
        with operation_store[0].begin() as conn:
            if tamper == "creator":
                event_row = conn.execute(
                    select(events).where(
                        events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == ready.selected.cursor
                    )
                ).one()
                changed = ready.event.model_copy(update={"creator_generation": ready.event.creator_generation + 1})
                replace_sql_chunk(
                    conn,
                    event_row.payload_chunk_key,
                    canonical_json(changed.model_dump(mode="json")),
                    session_id=sid,
                    operation_id=operation,
                )
            elif tamper == "root":
                row = conn.execute(
                    select(checkpoints).where(
                        checkpoints.c.session_id == sid,
                        checkpoints.c.operation_id == operation,
                        checkpoints.c.checkpoint_id == ready.selected.checkpoint_id,
                    )
                ).one()
                root = decode_continuation_snapshot(ready.selected.payload_json)
                changed_request_id = str(uuid4())
                changed = root.model_copy(
                    update={
                        "logical": root.logical.model_copy(update={"original_user_message_id": changed_request_id}),
                        "mutation_ceiling": root.mutation_ceiling.model_copy(update={"request_id": changed_request_id}),
                    }
                )
                coherent = encode_continuation_snapshot(changed)
                assert decode_continuation_snapshot(coherent) == changed
                replace_sql_chunk(conn, row.snapshot_chunk_key, coherent, session_id=sid, operation_id=operation)
            else:
                conn.execute(update(usage).where(usage.c.entry_id == ready.event.ledger_entry_id).values(prompt_tokens=8))
        before = recovery_rows(operation_store[0])
        expected = {
            "creator": "complete application creator/event",
            "root": "complete atomic forward transition",
            "ledger": "completed ledger changed",
        }[tamper]
        with pytest.raises(AuditIntegrityError, match=expected):
            await application.read_current_title_application(service=service, running=running, required_work=work)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_ready", (False, True))
async def test_actual_fresh_claim_applies_or_replays_paid_title_without_second_sdk(operation_store, tmp_path, monkeypatch, already_ready):
    running, service, work, settled, original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    current_running = None
    released = False
    try:
        if already_ready:
            assert (await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)).applied
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)
        released = True
        current_store = new_owners(operation_store)
        _engine, repository, authority, _sessions, sid = current_store
        assert repository.recover_composer_segment(
            session_id=sid,
            operation_id=running.claim.operation_id,
            expected_generation=running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        current_running = ComposerOperationRunning(claim=claim, session_operation_context=context)
        fresh_work = inventory_work(current_running)
        before = recovery_rows(current_store[0])
        current = await application.read_current_title_application(service=service, running=current_running, required_work=fresh_work)
        assert recovery_rows(current_store[0]) == before and current.applied is already_ready
        await actual_title_update(current_store, current_running, fresh_work, "Rename Under Fresh Current Writer")
        untouched = recovery_rows(current_store[0])
        result = await application.apply_current_title_success(service=service, selected=current.selected, required_work=fresh_work)
        assert result.applied and result.result == original.result and len(sdk) == 1
        assert (await current_store[3].get_session(sid)).title == (
            "Rename Under Fresh Current Writer" if already_ready else "Original Café Session Title"
        )
        if already_ready:
            assert recovery_rows(current_store[0]) == untouched
        assert len(sql_rows(current_store)[1]) == 1
        fresh_work.coordinator.assert_completed()
        stale_work = inventory_work(running)
        before = recovery_rows(current_store[0])
        with pytest.raises(operations.ComposerOperationFenceLost):
            await application.read_current_title_application(service=service, running=running, required_work=stale_work)
        assert recovery_rows(current_store[0]) == before
        stale_work.coordinator.assert_completed()
    finally:
        if current_running is not None:
            current_store[1].release(current_running.session_operation_context)
        if not released:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_real_pending_title_accounting_refuses_application_without_writes(operation_store, tmp_path, monkeypatch):
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    reached, resume = asyncio.Event(), asyncio.Event()
    original_finish = type(operation_store[3]).finish_provider_attempt
    sdk = []

    async def pause(self, **kwargs):
        reached.set()
        await resume.wait()
        return await original_finish(self, **kwargs)

    async def completion(**kwargs):
        sdk.append(kwargs)
        return raw_reply(kwargs["model"])

    monkeypatch.setattr(type(operation_store[3]), "finish_provider_attempt", pause)
    monkeypatch.setattr(litellm, "acompletion", completion)
    task = asyncio.create_task(
        provider.run_prepared_title_provider(
            prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
        )
    )
    joined = False
    body_error = None
    try:
        await reached_or_failure(task, reached)
        current = await provider.read_current_title_result(service=service, running=running, required_work=work)
        assert not current.settled and len(sdk) == 1 and not sql_rows(operation_store)[1]
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="complete settled-success cut"):
            await application.read_current_title_application(service=service, running=running, required_work=work)
        assert recovery_rows(operation_store[0]) == before
        resume.set()
        await task
        joined = True
        settled = await provider.read_current_title_result(service=service, running=running, required_work=work)
        assert settled.settled
        assert (await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)).applied
        assert len(sdk) == 1 and len(sql_rows(operation_store)[1]) == 1
        work.coordinator.assert_completed()
    except BaseException as error:
        body_error = error
        raise
    finally:
        resume.set()
        try:
            if not joined:
                try:
                    await task
                except BaseException as child_error:
                    if body_error is not None and child_error is not body_error:
                        raise BaseExceptionGroup("Pending TITLE fixture and actual owner outcomes", [body_error, child_error]) from None
                    raise
        finally:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("worker_before_commit", "committed_before_return"))
async def test_title_application_cancellation_joins_actual_commit_and_replays_without_write(
    operation_store, tmp_path, monkeypatch, boundary
):
    running, service, work, settled, original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    reached, resume = asyncio.Event(), asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    original_append = owner.append_complete_snapshot
    original_apply = type(operation_store[3]).apply_continuation_title_application

    def pause_append(conn, **kwargs):
        result = original_append(conn, **kwargs)
        loop.call_soon_threadsafe(reached.set)
        if not release.wait(timeout=10):
            raise AssertionError("Actual TITLE application worker was not released")
        return result

    async def pause_return(self, *args, **kwargs):
        result = await original_apply(self, *args, **kwargs)
        reached.set()
        await resume.wait()
        return result

    if boundary == "worker_before_commit":
        monkeypatch.setattr(owner, "append_complete_snapshot", pause_append)
    else:
        monkeypatch.setattr(type(operation_store[3]), "apply_continuation_title_application", pause_return)
    task = asyncio.create_task(application.apply_current_title_success(service=service, selected=settled.selected, required_work=work))
    joined = False
    body_error = None
    try:
        await reached_or_failure(task, reached)
        task.cancel("original caller cancelled TITLE application")
        release.set()
        resume.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        joined = True
        assert caught.value.args == ("original caller cancelled TITLE application",)
        work.coordinator.assert_completed()
        monkeypatch.setattr(owner, "append_complete_snapshot", original_append)
        monkeypatch.setattr(type(operation_store[3]), "apply_continuation_title_application", original_apply)
        fresh_work = inventory_work(running)
        ready = await application.read_current_title_application(service=service, running=running, required_work=fresh_work)
        assert ready.applied and ready.result == original.result and len(sdk) == 1
        assert (await operation_store[3].get_session(operation_store[4])).title == "Original Café Session Title"
        before = recovery_rows(operation_store[0])
        replay = await application.apply_current_title_success(service=service, selected=ready.selected, required_work=fresh_work)
        assert replay.applied and recovery_rows(operation_store[0]) == before and len(sdk) == 1
        assert len(sql_rows(operation_store)[1]) == 1
        fresh_work.coordinator.assert_completed()
    except BaseException as error:
        body_error = error
        raise
    finally:
        release.set()
        resume.set()
        try:
            if not joined:
                try:
                    await task
                except BaseException as child_error:
                    if body_error is not None and child_error is not body_error:
                        raise BaseExceptionGroup(
                            "TITLE cancellation fixture and actual owner outcomes", [body_error, child_error]
                        ) from None
                    raise
        finally:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_original_codec_refuses_logical_request_without_matching_ceiling(operation_store, tmp_path, monkeypatch):
    running, service, work, settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    try:
        ready = await application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert ready.applied
        root = decode_continuation_snapshot(ready.selected.payload_json)
        inconsistent = root.model_copy(update={"logical": root.logical.model_copy(update={"original_user_message_id": str(uuid4())})})
        encoded = encode_continuation_snapshot(inconsistent)
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="Continuation mutation ceiling changed its original request"):
            decode_continuation_snapshot(encoded)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_malformed_owned_title_decision_closes_unsubmitted_ticket_without_sql(operation_store, tmp_path, monkeypatch):
    running, service, work, _settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    projection = None
    try:
        current = await application.read_current_title_application(service=service, running=running, required_work=work)
        request = owner.TitleApplicationRequest(running, current.selected, current.inventory, current.result, current.decision)
        malformed = replace(request, decision=None)
        submissions = []
        original_submit = async_workers._submit_shared

        async def observe_submission(*args, **kwargs):
            submissions.append(args[1])
            return await original_submit(*args, **kwargs)

        monkeypatch.setattr(async_workers, "_submit_shared", observe_submission)
        calibration = await application.read_current_title_application(service=service, running=running, required_work=work)
        assert calibration == current and submissions and all(ticket.complete for ticket in submissions)
        submissions.clear()
        sql, projection = work.reserve_pair(RequiredWorkSource.TITLE_ACCOUNTING_SQL, RequiredWorkSource.TITLE_ACCOUNTING_PROJECTION)
        before = recovery_rows(operation_store[0])
        with pytest.raises(AttributeError, match="candidate") as caught:
            await operation_store[3].apply_continuation_title_application(malformed, required_work=sql)
        assert caught.value.__cause__ is None
        assert submissions == [] and recovery_rows(operation_store[0]) == before and len(sdk) == 1
        # Original v135 reaches precisely this assertion with complete=False.
        # The production preflight closes only the known unsubmitted ticket;
        # it does not accept/normalize the malformed decision or run a writer.
        assert sql.complete
        projection.complete_without_submission()
        work.coordinator.assert_completed()
    finally:
        if projection is not None and not projection.complete:
            projection.complete_without_submission()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ("request", "source", "fence"))
async def test_title_preflight_preserves_original_nominal_source_and_fence_refusals(operation_store, tmp_path, monkeypatch, refusal):
    running, service, work, _settled, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
    current_running = None
    released = False
    projection = None
    try:
        actual_running, actual_work = running, work
        if refusal == "fence":
            work.coordinator.assert_completed()
            operation_store[1].release(running.session_operation_context)
            released = True
            current_store = new_owners(operation_store)
            _engine, repository, authority, _sessions, sid = current_store
            assert repository.recover_composer_segment(
                session_id=sid,
                operation_id=running.claim.operation_id,
                expected_generation=running.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
            (claim,) = authority.claim_segment(limit=1)
            context = repository.start_composer_segment(
                claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
            )
            current_running = ComposerOperationRunning(claim=claim, session_operation_context=context)
            actual_running, actual_work = current_running, inventory_work(current_running)
        current = await application.read_current_title_application(service=service, running=actual_running, required_work=actual_work)
        assert not current.applied
        request = owner.TitleApplicationRequest(actual_running, current.selected, current.inventory, current.result, current.decision)
        submissions = []
        original_submit = async_workers._submit_shared

        async def observe_submission(*args, **kwargs):
            submissions.append(args[1])
            return await original_submit(*args, **kwargs)

        monkeypatch.setattr(async_workers, "_submit_shared", observe_submission)
        assert (
            await application.read_current_title_application(service=service, running=actual_running, required_work=actual_work) == current
        )
        assert submissions and all(ticket.complete for ticket in submissions)
        submissions.clear()
        if refusal == "source":
            sql, projection = actual_work.reserve_pair(
                RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION
            )
        else:
            # The fence case intentionally retains the original genuine owner's
            # receipt while the request uses the real recovered current owner.
            receipt_work = work if refusal == "fence" else actual_work
            sql, projection = receipt_work.reserve_pair(
                RequiredWorkSource.TITLE_ACCOUNTING_SQL, RequiredWorkSource.TITLE_ACCOUNTING_PROJECTION
            )
        completions = []
        original_complete = RequiredWorkTicket.complete_without_submission

        def observe_completion(self, error=None):
            if self is sql:
                completions.append(error)
            return original_complete(self, error)

        monkeypatch.setattr(RequiredWorkTicket, "complete_without_submission", observe_completion)
        expected = {
            "request": "^TITLE application requires its actual current request$",
            "source": "^required SQL receipt source/session mismatch$",
            "fence": "^required SQL receipt fence mismatch$",
        }[refusal]
        before = recovery_rows(operation_store[0])
        supplied = object() if refusal == "request" else request
        with pytest.raises(AuditIntegrityError, match=expected) as caught:
            await operation_store[3].apply_continuation_title_application(supplied, required_work=sql)
        assert len(completions) == 1 and completions[0] is caught.value
        assert caught.value.__cause__ is None and sql.complete and submissions == []
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        projection.complete_without_submission()
        actual_work.coordinator.assert_completed()
        work.coordinator.assert_completed()
    finally:
        if projection is not None and not projection.complete:
            projection.complete_without_submission()
        if current_running is not None:
            current_store[1].release(current_running.session_operation_context)
        if not released:
            operation_store[1].release(running.session_operation_context)
