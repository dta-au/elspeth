from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.web import async_workers
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.coordination import composer_operation_authority as authority_module
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
)
from elspeth.web.sessions._persist_payload import RedactedToolRow, StatePayload
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
)
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web.coordination.test_composer_operation_authority import (
    operation_store as operation_store,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    start,
)
from tests.unit.web.sessions.session_test_authority import private_evidence_for_test_turn

__all__ = ["operation_store"]


async def thread_event(event):
    assert await asyncio.to_thread(event.wait, 10), "Actual selected worker seam was not reached"


def reachable(error):
    """Retain objects, not merely exception classes or strings."""
    pending, result, seen = [error], [], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        result.append(item)
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        if item.__cause__ is not None:
            pending.append(item.__cause__)
        if item.__context__ is not None:
            pending.append(item.__context__)
    return result


async def captured(awaitable):
    try:
        return await awaitable
    except BaseException as error:
        return error


def coordinator_for(running):
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    ticket = coordinator.reserve(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, transition_ordinal=1, semantic_ordinal=1)
    return coordinator, ticket


def p4_input(sid, running, ticket):
    from elspeth.core.canonical import canonical_json
    from elspeth.web.composer.redaction import redact_tool_call_response
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from elspeth.web.composer.tools._common import _failure_result

    context = running.session_operation_context
    request_id = "synthetic-storage-request"
    arguments = "{}"
    content = '{"success":true,"data":{"status":"SYNTHETIC_STORAGE_INPUT"}}'
    visible = VisibleToolEvent.capture(
        context=context,
        request_id=request_id,
        tool_call_id="storage-tool-1",
        tool_name="set_metadata",
        received_arguments=arguments,
        effective_arguments=arguments,
        selected_result=content,
        display_arguments=arguments,
        display_result=content,
    )
    row = RedactedToolRow(
        "storage-tool-1",
        content,
        StatePayload(CompositionStateData(metadata_={"name": "Storage candidate"})),
        visible,
    )
    base = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    failure = _failure_result(
        base, "Automatic mutation authority was revoked.", error_code="auto_mutation_revoked", with_state_validation=False
    )
    refusal = canonical_json(redact_tool_call_response("set_metadata", failure.to_dict(), telemetry=NoopRedactionTelemetry()))
    values = {
        "session_id": str(sid),
        "assistant_content": "Synthetic storage candidate",
        "raw_content": "Synthetic storage candidate",
        "redacted_assistant_tool_calls": (
            {
                "id": "storage-tool-1",
                "type": "function",
                "function": {"name": "set_metadata", "arguments": arguments},
            },
        ),
        "redacted_tool_rows": (row,),
        "revoked_tool_rows": (
            replace(row, content=refusal, composition_state_payload=None, visible_event=visible.with_display_result(refusal)),
        ),
        "parent_composition_state_id": None,
        "expected_current_state_id": None,
        "writer_principal": "compose_loop",
        "plugin_crash_pending": False,
        "session_operation_context": context,
        "required_work": ticket,
        "mutation_ceiling": ComposerMutationCeiling(str(sid), request_id, "auto_commit"),
        "authority_revocation": None,
    }
    values["private_tool_evidence"] = private_evidence_for_test_turn(values)
    return values


async def ready(store):
    _engine, _repository, _authority, service, sid = store
    await service.update_composer_preferences(sid, trust_mode="auto_commit", density_default="high", actor="user:alice")
    _record, running = start(store)
    coordinator, ticket = coordinator_for(running)
    return running, coordinator, ticket, p4_input(sid, running, ticket)


def read_rows(engine, sid):
    with engine.connect() as conn:
        chat = (
            conn.execute(
                select(chat_messages_table).where(chat_messages_table.c.session_id == str(sid)).order_by(chat_messages_table.c.sequence_no)
            )
            .mappings()
            .all()
        )
        states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(sid))).mappings().all()
        proposals = (
            conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == str(sid))).mappings().all()
        )
    return chat, states, proposals


def assert_committed_cohort(engine, sid, values, outcome):
    chat, states, proposals = read_rows(engine, sid)
    assert proposals == []
    assert [row["role"] for row in chat] == ["assistant", "tool", "audit"]
    assert len(states) == 1
    assert outcome.checkpoint_disposition == "committed"
    assert outcome.current_state_id == states[0]["id"]
    assert outcome.checkpoint is not None
    assert str(outcome.checkpoint.state.id) == states[0]["id"]
    assert chat[0]["id"] == outcome.assistant_id
    assert chat[1]["parent_assistant_id"] == chat[0]["id"]
    assert chat[1]["composition_state_id"] == states[0]["id"]
    assert chat[1]["content"] == values["redacted_tool_rows"][0].content
    assert outcome.published_tool_rows == values["redacted_tool_rows"]
    assert outcome.published_assistant_content == values["assistant_content"]
    assert outcome.authority_revocation is None
    # The writer persists the private envelope in tool_calls, not content.
    envelopes = chat[2]["tool_calls"]
    assert len(envelopes) == 1
    decoded = ComposerDiagnosticEvidence.from_envelope(
        envelopes[0],
        expected_session_id=str(sid),
        expected_operation_id=values["session_operation_context"].fence.operation_id,
        expected_tool_call_id="storage-tool-1",
    )
    assert decoded == values["private_tool_evidence"][0]


@pytest.mark.asyncio
async def test_p4_cancel_before_worker_admission_retains_original_and_has_no_sql(
    operation_store,
    monkeypatch,
):
    engine, repository, _authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    entered = asyncio.Event()
    never_release = asyncio.Event()
    original_cancellations = []
    sync_calls = []
    original_sync = service.persist_compose_turn

    async def held_admission():
        entered.set()
        try:
            await never_release.wait()
        except asyncio.CancelledError as original:
            original_cancellations.append(original)
            raise

    def observe_sync(**kwargs):
        sync_calls.append(kwargs)
        return original_sync(**kwargs)

    monkeypatch.setattr(async_workers, "_acquire_admission", held_admission)
    monkeypatch.setattr(service, "persist_compose_turn", observe_sync)
    task = asyncio.create_task(service.persist_compose_turn_async(**values))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel("cancel-before-physical-submission")
        result = await captured(task)
        assert len(original_cancellations) == 1
        assert result is original_cancellations[0]
        assert result.args == ("cancel-before-physical-submission",)
        assert sync_calls == []
        assert ticket.complete
        assert ticket.errors == (result,)
        coordinator.assert_completed()
        assert read_rows(engine, sid) == ([], [], [])
    finally:
        never_release.set()
        await asyncio.gather(task, return_exceptions=True)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_ordinary_p4_cancel_while_physically_queued_never_starts_sql(
    operation_store,
    monkeypatch,
):
    engine, repository, _authority, service, sid = operation_store
    await service.update_composer_preferences(sid, trust_mode="auto_commit", density_default="high", actor="user:alice")
    _record, running = start(operation_store)
    values = p4_input(sid, running, None)
    occupied, release, submitted = threading.Event(), threading.Event(), threading.Event()
    invocations = []
    original_sync = service.persist_compose_turn

    class WitnessExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            submitted.set()
            return future

    def occupy():
        occupied.set()
        assert release.wait(10)

    def observe_sync(**kwargs):
        invocations.append(kwargs)
        return original_sync(**kwargs)

    monkeypatch.setattr(service, "persist_compose_turn", observe_sync)
    try:
        with WitnessExecutor(max_workers=1) as executor:
            blocker = executor.submit(occupy)
            await thread_event(occupied)
            submitted.clear()
            monkeypatch.setattr(async_workers, "_get_shared_executor", lambda: executor)
            task = asyncio.create_task(service.persist_compose_turn_async(**values))
            try:
                await thread_event(submitted)
                assert invocations == []
                task.cancel("ordinary-queued-p4")
                result = await captured(task)
                assert isinstance(result, asyncio.CancelledError)
                assert not blocker.done()
                assert invocations == []
                assert read_rows(engine, sid) == ([], [], [])
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            blocker.result(timeout=10)
        # The executor is drained: a late queued write cannot escape the oracle.
        assert invocations == []
        assert read_rows(engine, sid) == ([], [], [])
    finally:
        release.set()
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_required_p4_queued_cancellation_keeps_actual_sql_and_original_objects(operation_store, monkeypatch):
    engine, repository, _authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    occupied, release, submitted = threading.Event(), threading.Event(), threading.Event()
    cancellations, calls, outcomes = [], [], []
    observed = [asyncio.Event() for _ in range(3)]
    original_sync = service.persist_compose_turn
    original_retain = async_workers._retain_cancellation
    original_finish = async_workers.run_required_sql_finish_once

    class WitnessExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            submitted.set()
            return future

    def occupy():
        occupied.set()
        assert release.wait(10)

    def actual_sql(**kwargs):
        calls.append(kwargs)
        return original_sync(**kwargs)

    def retain(items, original):
        original_retain(items, original)
        if all(original is not prior for prior in cancellations):
            cancellations.append(original)
            observed[len(cancellations) - 1].set()

    async def finish(actual_ticket, func, *args, **kwargs):
        outcome = await original_finish(actual_ticket, func, *args, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(service, "persist_compose_turn", actual_sql)
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain)
    monkeypatch.setattr(async_workers, "run_required_sql_finish_once", finish)
    try:
        with WitnessExecutor(max_workers=1) as executor:
            blocker = executor.submit(occupy)
            await thread_event(occupied)
            submitted.clear()
            monkeypatch.setattr(async_workers, "_get_shared_executor", lambda: executor)
            task = asyncio.create_task(service.persist_compose_turn_async(**values))
            try:
                await thread_event(submitted)
                for index in range(3):
                    task.cancel(f"required-queued-p4-{index}")
                    await asyncio.wait_for(observed[index].wait(), 10)
                    assert calls == [] and not task.done() and not ticket.complete
                    assert not blocker.done()
                    assert read_rows(engine, sid) == ([], [], [])
                release.set()
                result = await captured(task)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            blocker.result(timeout=10)
        assert isinstance(result, BaseExceptionGroup)
        assert all(isinstance(error, asyncio.CancelledError) for error in result.exceptions)
        assert len(cancellations) == len({id(error) for error in cancellations}) == 3
        assert [error.args for error in cancellations] == [(f"required-queued-p4-{index}",) for index in range(3)]
        assert all(any(leaf is original for leaf in reachable(result)) for original in cancellations)
        assert len(calls) == len(outcomes) == 1
        handoff = outcomes[0]
        assert type(handoff) is RequiredSQLReturned
        assert len(handoff.deferred_cancellations) == 3
        assert all(actual is original for actual, original in zip(handoff.deferred_cancellations, cancellations, strict=True))
        assert_committed_cohort(engine, sid, values, handoff.value)
        assert ticket.complete
        coordinator.assert_completed()
    finally:
        release.set()
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_sql", [False, True])
async def test_started_p4_joins_repeated_original_cancellations_and_actual_sql(
    operation_store,
    monkeypatch,
    fail_sql,
):
    engine, repository, _authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    cancellations = []
    observed = [asyncio.Event() for _ in range(3)]
    physical_outcomes = []
    physical_calls = []
    fault = OperationalError("controlled P4 graph insert", {}, RuntimeError("original driver failure"))
    original_insert = service._insert_composition_checkpoint
    original_retain = async_workers._retain_cancellation
    original_finish = async_workers.run_required_sql_finish_once

    def held_insert(*args, **kwargs):
        physical_calls.append(kwargs)
        entered.set()
        assert release.wait(10)
        try:
            if fail_sql:
                raise fault
            return original_insert(*args, **kwargs)
        finally:
            finished.set()

    def retain(items, original):
        original_retain(items, original)
        if all(original is not prior for prior in cancellations):
            cancellations.append(original)
            observed[len(cancellations) - 1].set()

    async def observe_finish(actual_ticket, func, *args, **kwargs):
        outcome = await original_finish(actual_ticket, func, *args, **kwargs)
        physical_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(service, "_insert_composition_checkpoint", held_insert)
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain)
    monkeypatch.setattr(async_workers, "run_required_sql_finish_once", observe_finish)
    task = asyncio.create_task(service.persist_compose_turn_async(**values))
    try:
        await thread_event(entered)
        for index in range(3):
            task.cancel(f"original-p4-cancellation-{index}")
            await asyncio.wait_for(observed[index].wait(), 10)
            assert not task.done()
            assert not ticket.complete
            assert not finished.is_set()
        release.set()
        result = await captured(task)
        assert isinstance(result, BaseException)
        assert finished.is_set()
        assert len(physical_calls) == len(physical_outcomes) == 1
        assert len(cancellations) == len({id(error) for error in cancellations}) == 3
        assert [error.args for error in cancellations] == [(f"original-p4-cancellation-{index}",) for index in range(3)]
        retained = reachable(result)
        assert all(any(leaf is original for leaf in retained) for original in cancellations)
        handoff = physical_outcomes[0]
        assert len(handoff.deferred_cancellations) == 3
        assert all(actual is original for actual, original in zip(handoff.deferred_cancellations, cancellations, strict=True))
        assert ticket.complete
        coordinator.assert_completed()
        if fail_sql:
            assert type(handoff) is RequiredSQLRaised
            assert any(leaf is fault for leaf in reachable(handoff.error))
            assert any(leaf is fault for leaf in retained)
            assert read_rows(engine, sid) == ([], [], [])
        else:
            assert type(handoff) is RequiredSQLReturned
            assert_committed_cohort(engine, sid, values, handoff.value)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["stop", "p4"])
async def test_durable_stop_and_p4_obey_both_session_lock_orders(
    operation_store,
    monkeypatch,
    first,
):
    engine, repository, authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    stop_attempted, stop_locked = threading.Event(), threading.Event()
    p4_attempted, p4_locked = threading.Event(), threading.Event()
    release_first = threading.Event()
    original_stop_transaction = authority_module.locked_session_transaction
    original_p4_begin = service._session_process_locked_begin
    original_p4_lock = service._session_write_lock
    order = []

    @contextmanager
    def stop_transaction(*args, **kwargs):
        stop_attempted.set()
        with original_stop_transaction(*args, **kwargs) as conn:
            stop_locked.set()
            order.append("stop")
            if first == "stop":
                assert release_first.wait(10)
            yield conn

    @contextmanager
    def p4_begin(*args, **kwargs):
        p4_attempted.set()
        with original_p4_begin(*args, **kwargs) as conn:
            yield conn

    @contextmanager
    def p4_lock(*args, **kwargs):
        with original_p4_lock(*args, **kwargs):
            p4_locked.set()
            order.append("p4")
            if first == "p4":
                assert release_first.wait(10)
            yield

    def stop():
        return authority.request_cancel(
            session_id=sid,
            operation_id=running.claim.operation_id,
            cancelled_failure=lambda request_id: pytest.fail("A running operation must not take the queued terminal shortcut"),
        )

    monkeypatch.setattr(authority_module, "locked_session_transaction", stop_transaction)
    monkeypatch.setattr(service, "_session_process_locked_begin", p4_begin)
    monkeypatch.setattr(service, "_session_write_lock", p4_lock)
    tasks = []
    try:
        if first == "stop":
            stop_task = asyncio.create_task(asyncio.to_thread(stop))
            tasks.append(stop_task)
            await thread_event(stop_locked)
            p4_task = asyncio.create_task(service.persist_compose_turn_async(**values))
            tasks.append(p4_task)
            await thread_event(p4_attempted)
            assert not p4_locked.is_set()
        else:
            p4_task = asyncio.create_task(service.persist_compose_turn_async(**values))
            tasks.append(p4_task)
            await thread_event(p4_locked)
            stop_task = asyncio.create_task(asyncio.to_thread(stop))
            tasks.append(stop_task)
            await thread_event(stop_attempted)
            assert not stop_locked.is_set()
        assert not p4_task.done() and not stop_task.done()
        release_first.set()
        result = await captured(p4_task)
        stopped = await stop_task
        assert stopped is not None and stopped.cancel_requested_at is not None
        assert order == (["stop", "p4"] if first == "stop" else ["p4", "stop"])
        assert ticket.complete
        coordinator.assert_completed()
        if first == "stop":
            assert isinstance(result, BaseException)
            assert any(isinstance(error, ComposerOperationCancelledDuringTurn) for error in reachable(result))
            assert read_rows(engine, sid) == ([], [], [])
        else:
            assert not isinstance(result, BaseException)
            assert_committed_cohort(engine, sid, values, result)
        current = authority.get(session_id=sid, operation_id=running.claim.operation_id)
        assert current is not None
        assert current.cancel_requested_at == stopped.cancel_requested_at
    finally:
        release_first.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["preferences", "p4"])
async def test_preferences_and_p4_obey_both_session_lock_orders(operation_store, monkeypatch, first):
    import elspeth.web.sessions.service as session_module

    engine, repository, _authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    attempted = {name: threading.Event() for name in ("preferences", "p4")}
    locked = {name: threading.Event() for name in attempted}
    release = threading.Event()
    local = threading.local()
    order = []
    original_blob_lock = session_module._blob_custody_session_lock
    original_write_lock = service._session_write_lock
    original_begin = service._session_process_locked_begin
    original_p4 = service.persist_compose_turn

    @contextmanager
    def preference_custody(*args, **kwargs):
        local.role = "preferences"
        attempted["preferences"].set()
        try:
            with original_blob_lock(*args, **kwargs):
                yield
        finally:
            del local.role

    def p4(**kwargs):
        local.role = "p4"
        try:
            return original_p4(**kwargs)
        finally:
            del local.role

    @contextmanager
    def process_begin(*args, **kwargs):
        attempted[local.role].set()
        with original_begin(*args, **kwargs) as connection:
            yield connection

    @contextmanager
    def write_lock(*args, **kwargs):
        role = local.role
        with original_write_lock(*args, **kwargs):
            locked[role].set()
            order.append(role)
            if role == first:
                assert release.wait(10)
            yield

    async def preferences():
        return await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="high", actor="user:alice")

    monkeypatch.setattr(session_module, "_blob_custody_session_lock", preference_custody)
    monkeypatch.setattr(service, "persist_compose_turn", p4)
    monkeypatch.setattr(service, "_session_process_locked_begin", process_begin)
    monkeypatch.setattr(service, "_session_write_lock", write_lock)
    tasks = {}
    try:
        tasks[first] = asyncio.create_task(preferences() if first == "preferences" else service.persist_compose_turn_async(**values))
        await thread_event(locked[first])
        second = "p4" if first == "preferences" else "preferences"
        tasks[second] = asyncio.create_task(preferences() if second == "preferences" else service.persist_compose_turn_async(**values))
        # SQLite can block at the process lock before SQL lock admission.
        await thread_event(attempted[second])
        assert not tasks[second].done()
        assert not locked[second].is_set()
        release.set()
        outcome = await tasks["p4"]
        await tasks["preferences"]
        assert order == [first, second]
        coordinator.assert_completed()
        assert ticket.complete
        assert (await service.get_composer_preferences(sid)).trust_mode == "explicit_approve"
        if first == "p4":
            assert_committed_cohort(engine, sid, values, outcome)
        else:
            chat, states, proposals = read_rows(engine, sid)
            assert states == proposals == []
            assert outcome.checkpoint_disposition == "empty"
            assert outcome.current_state_id is None and outcome.checkpoint is None
            assert outcome.authority_revocation.ceiling is values["mutation_ceiling"]
            assert outcome.authority_revocation.current_trust_mode == "explicit_approve"
            assert outcome.published_tool_rows == values["revoked_tool_rows"]
            assert outcome.published_assistant_content == ""
            assert [row["role"] for row in chat] == ["assistant", "tool", "audit"]
            assert chat[0]["content"] == "" and chat[0]["raw_content"] == values["raw_content"]
            assert chat[1]["content"] == values["revoked_tool_rows"][0].content
            assert chat[1]["composition_state_id"] is None
            assert chat[1]["parent_assistant_id"] == outcome.assistant_id
            actual_private = ComposerDiagnosticEvidence.from_envelope(
                chat[2]["tool_calls"][0],
                expected_session_id=str(sid),
                expected_operation_id=running.session_operation_context.fence.operation_id,
                expected_tool_call_id="storage-tool-1",
            )
            assert actual_private == values["private_tool_evidence"][0]
    finally:
        release.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        repository.release(running.session_operation_context)


def blob_custody_child(database_url, data_dir, context, ceiling, blob_id, action, entered, release, results):
    from dataclasses import replace
    from pathlib import Path
    from uuid import UUID

    from elspeth.contracts.composer_mutation import ComposerAutoMutationRefused
    from elspeth.contracts.enums import CreationModality
    from elspeth.web.blobs import replacement as replacement_module
    from elspeth.web.blobs import service as blob_module
    from elspeth.web.blobs.replacement import BlobReplacementCoordinator
    from elspeth.web.blobs.service import BlobServiceImpl, _persist_blob_content, content_hash
    from elspeth.web.coordination.repository import PostgresSessionOperationRepository
    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
    from elspeth.web.sessions.engine import create_session_engine
    from elspeth.web.sessions.locking import _blob_custody_session_lock

    engine = create_session_engine(database_url)
    repository = (
        PostgresSessionOperationRepository(engine) if engine.dialect.name == "postgresql" else SQLiteLocalSessionOperationAuthority(engine)
    )
    original_write = blob_module._atomic_write_blob
    original_replace_write = replacement_module._atomic_write_blob
    original_delete = BlobServiceImpl._commit_blob_deletion_plan

    def pause():
        entered.set()
        assert release.wait(15), "Parent did not release actual filesystem custody"

    def write(*args, **kwargs):
        pause()
        return original_write(*args, **kwargs)

    def replace_write(*args, **kwargs):
        pause()
        return original_replace_write(*args, **kwargs)

    def delete(self, **kwargs):
        pause()
        return original_delete(self, **kwargs)

    blob_module._atomic_write_blob = write
    replacement_module._atomic_write_blob = replace_write
    BlobServiceImpl._commit_blob_deletion_plan = delete
    try:
        if action == "create":
            _persist_blob_content(
                engine=engine,
                data_dir=Path(data_dir),
                max_storage_per_session=1_000_000,
                blob_id=UUID(blob_id),
                session_id=context.fence.session_id,
                filename="custody.csv",
                content=b"value\n43\n",
                mime_type="text/csv",
                created_by="user",
                source_description=None,
                creation_modality=CreationModality.VERBATIM,
                created_from_message_id=None,
                creating_model_identifier=None,
                creating_model_version=None,
                creating_provider=None,
                creating_composer_skill_hash=None,
                creating_arguments_hash=None,
                idempotent=False,
                session_operation_context=context,
                session_operation_authority=repository,
                auto_mutation=ceiling,
            )
        elif action == "replace":
            expected = repository.mutate(context, lambda tx: tx.blobs.read_blob(blob_id=UUID(blob_id)))
            content = b"value\n43\n"
            BlobReplacementCoordinator(engine=engine, data_dir=Path(data_dir), session_operation_authority=repository).replace_blob(
                expected=expected,
                replacement=replace(expected, size_bytes=len(content), content_hash=content_hash(content)),
                content=content,
                context=context,
                max_storage_per_session=1_000_000,
                accepting_proposal_id=None,
                auto_mutation=ceiling,
            )
        else:
            assert action == "delete"
            blobs = BlobServiceImpl(engine, Path(data_dir), session_operation_authority=repository)
            with _blob_custody_session_lock(engine, context.fence.session_id):
                blobs._delete_blob_with_ledger(blob_id=UUID(blob_id), context=context, auto_mutation=ceiling)
    except ComposerAutoMutationRefused as refused:
        results.put(("refused", refused.revocation.current_trust_mode))
    except BaseException as error:
        results.put(("failed", type(error).__name__))
        raise
    else:
        results.put(("committed", None))
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "replace", "delete"])
@pytest.mark.parametrize("first", ["preferences", "blob"])
async def test_blob_custody_and_preferences_serialize_actual_filesystem_effects(
    operation_store,
    tmp_path,
    monkeypatch,
    action,
    first,
):
    import multiprocessing
    from pathlib import Path
    from uuid import uuid4

    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.sessions.models import blob_deletion_cleanups_table, blob_replacement_cleanups_table, blobs_table

    engine, repository, _authority, service, sid = operation_store
    await service.update_composer_preferences(sid, trust_mode="auto_commit", density_default="high", actor="user:alice")
    _record, running = start(operation_store)
    context = running.session_operation_context
    ceiling = ComposerMutationCeiling(str(sid), "storage-custody-request", "auto_commit")
    data_dir = tmp_path / "custody-data"
    blobs = BlobServiceImpl(engine, data_dir, session_operation_authority=repository)
    blob_id = uuid4()
    if action != "create":
        seeded = await blobs.create_blob(sid, "custody.csv", b"value\n42\n", "text/csv", session_operation_context=context)
        blob_id = seeded.id

    def snapshot():
        with engine.connect() as connection:
            tables = tuple(
                tuple(connection.execute(select(table).where(table.c.session_id == str(sid)).order_by(*table.primary_key.columns)).all())
                for table in (blobs_table, blob_deletion_cleanups_table, blob_replacement_cleanups_table)
            )
        directory = data_dir / "blobs" / str(sid)
        files = tuple((str(path.relative_to(directory)), path.read_bytes()) for path in sorted(directory.rglob("*")) if path.is_file())
        return tables, files

    before = snapshot()
    process_context = multiprocessing.get_context("spawn")
    entered, release = process_context.Event(), process_context.Event()
    results = process_context.Queue()
    preference_attempted = threading.Event()
    original_run_sync = service._run_sync

    async def observe_actual_worker(func, *args, **kwargs):
        def entered_worker():
            preference_attempted.set()
            return func(*args, **kwargs)

        return await original_run_sync(entered_worker)

    monkeypatch.setattr(service, "_run_sync", observe_actual_worker)
    preference = None
    process = process_context.Process(
        target=blob_custody_child,
        args=(
            engine.url.render_as_string(hide_password=False),
            str(data_dir),
            context,
            ceiling,
            str(blob_id),
            action,
            entered,
            release,
            results,
        ),
    )
    try:
        if first == "preferences":
            await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="high", actor="user:alice")
            release.set()
        process.start()
        if first == "blob":
            await thread_event(entered)
            preference = asyncio.create_task(
                service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="high", actor="user:alice")
            )
            await thread_event(preference_attempted)
            # The child holds only existing blob custody across physical I/O;
            # it holds no long session transaction while this predicate waits.
            assert not preference.done()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(preference), 0.2)
            assert (await service.get_composer_preferences(sid)).trust_mode == "auto_commit"
            release.set()
        await asyncio.to_thread(process.join, 20)
        assert not process.is_alive()
        assert process.exitcode == 0
        outcome = results.get(timeout=2)
        if preference is not None:
            await preference
        assert (await service.get_composer_preferences(sid)).trust_mode == "explicit_approve"
        after = snapshot()
        if first == "preferences":
            assert outcome == ("refused", "explicit_approve")
            assert not entered.is_set(), "Refused mutation entered its filesystem phase"
            assert after == before
        else:
            assert outcome == ("committed", None)
            assert after[0][1:] == ((), ())  # no abandoned replacement/deletion journals
            with engine.connect() as connection:
                actual = connection.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).mappings().one_or_none()
            if action == "delete":
                assert actual is None and after[1] == ()
            else:
                assert actual is not None and actual["status"] == "ready"
                assert Path(actual["storage_path"]).read_bytes() == b"value\n43\n"
                assert len(after[1]) == 1
    finally:
        release.set()
        if process.pid is not None:
            await asyncio.to_thread(process.join, 20)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        if preference is not None:
            await asyncio.gather(preference, return_exceptions=True)
        results.close()
        results.join_thread()
        repository.release(context)
