"""Durable operation admission, exact start and terminal negative controls."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.composer_operations import (
    ComposerOperationActiveError,
    ComposerOperationAssistantWrite,
    ComposerOperationCancelledDuringTurn,
    ComposerOperationConflictError,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationPreconditionRefused,
    ComposerOperationRecord,
    ComposerOperationRunning,
    composer_operation_request_hash,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.schemas import ChatMessageResponse, MessageWithStateResponse, RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@pytest.fixture
def operation_store(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'operations.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    repository = SQLiteLocalSessionOperationAuthority(engine)
    session = repository.create_session_with_initial_fence(
        user_id="alice", title="Durable", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
    service = SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.storage"),
        session_operation_authority=repository,
        owner_instance_id="test-owner",
    )
    yield engine, repository, authority, service, session.id
    engine.dispose()


async def _wait_for_thread_event(event: Event) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while not event.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("SQL thread did not reach the controlled seam")
        await asyncio.sleep(0.01)


def admit(authority, sid, request=None, *, actor="alice", provider="local", maximum=64, deadline_seconds=180.0):
    request = request or SendMessageRequest(operation_id=str(uuid4()), content="Build")
    kind = "compose_message" if isinstance(request, SendMessageRequest) else "compose_recompose"
    return authority.admit(
        session_id=sid,
        operation_id=request.operation_id,
        kind=kind,
        request_hash=composer_operation_request_hash(session_id=sid, kind=kind, request=request),
        actor_user_id=actor,
        request_id="correlation",
        base_state_id=request.state_id,
        request_json=request.model_dump_json(),
        deadline_seconds=deadline_seconds,
        max_nonterminal=maximum,
        auth_provider_type=provider,
    )


def start(store, request=None, *, deadline_seconds=180.0):
    _engine, repository, authority, _service, sid = store
    record, _ = admit(authority, sid, request, deadline_seconds=deadline_seconds)
    (claim,) = authority.claim_next(limit=1)
    context = repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    return record, ComposerOperationRunning(claim=claim, session_operation_context=context)


def cancelled(*, request_id):
    return ComposerOperationError(
        http_status=499,
        failure_code="request_cancelled",
        error_type="request_cancelled",
        body={"error_type": "request_cancelled", "detail": "The composer request was stopped.", "request_id": request_id},
        diagnostic_id=None,
    )


def failed():
    return ComposerOperationError(
        http_status=503,
        failure_code="worker_lost",
        error_type="composer_operation_worker_lost",
        body={"error_type": "composer_operation_worker_lost", "detail": "Lost"},
        diagnostic_id=None,
    )


def response(message, proposals, state):
    return MessageWithStateResponse(
        message=ChatMessageResponse(
            id=str(message.id),
            session_id=str(message.session_id),
            role="assistant",
            content=message.content,
            segments=[],
            created_at=message.created_at,
            sequence_no=message.sequence_no,
        ),
        state=None,
        proposals=[],
    )


def test_admission_replay_conflict_active_owner_and_provider(operation_store):
    _engine, _repo, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    first, fresh = admit(authority, sid, request)
    replay, second = admit(authority, sid, request)
    assert fresh and not second and first == replay and replay.request_json is None
    with pytest.raises(ComposerOperationConflictError):
        admit(authority, sid, request.model_copy(update={"content": "Changed"}))
    with pytest.raises(ComposerOperationActiveError):
        admit(authority, sid)
    for actor, provider in (("bob", "local"), ("alice", "oidc")):
        with pytest.raises(ComposerOperationPreconditionRefused) as refused:
            admit(authority, sid, actor=actor, provider=provider)
        assert refused.value.error.http_status == 404
    assert authority.count_nonterminal(limit=1) == 1


def test_concurrent_same_id_is_one_immutable_admission(operation_store):
    _engine, _repo, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: admit(authority, sid, request), range(4)))
    assert sum(fresh for _record, fresh in results) == 1
    assert len({record.operation_id for record, _fresh in results}) == 1


def test_claim_is_queue_only_and_running_never_requeued(operation_store):
    engine, _repo, authority, _service, sid = operation_store
    record, running = start(operation_store)
    assert authority.claim_next(limit=2) == ()
    with pytest.raises(ComposerOperationFenceLost):
        authority.renew_claim(running.claim)
    with pytest.raises(ComposerOperationFenceLost):
        authority.release_claim(running.claim)
    assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(status="queued"))


def test_start_refusal_rolls_back_fence_and_has_no_user_side_effect(operation_store):
    engine, repository, authority, _service, sid = operation_store
    request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=uuid4())
    admit(authority, sid, request)
    (claim,) = authority.claim_next(limit=1)
    with engine.connect() as conn:
        before = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
    with pytest.raises(ComposerOperationPreconditionRefused) as refused:
        repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    assert refused.value.error.body == {"detail": "No messages to recompose from"}
    with engine.connect() as conn:
        after = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
    assert before == after
    assert authority.get(session_id=sid, operation_id=claim.operation_id).status == "queued"


@pytest.mark.asyncio
async def test_cancel_refuses_business_but_sealed_failure_can_settle(operation_store):
    _engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        await service.update_session_title(sid, "late", session_operation_context=running.session_operation_context)
    # Captured nominal integrity evidence survives a committed Stop marker.
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    failure = project_composer_operation_error(AuditIntegrityError("private evidence"), request_id="correlation")
    terminal = await service.fail_composer_async_operation(running, failure=failure, authoritative_failure=True)
    assert terminal.status == "failed" and terminal.result_json == canonical_json(failure.model_dump(mode="json"))
    with pytest.raises(ComposerOperationFenceLost):
        repository.mutate(running.session_operation_context, lambda tx: tx.session.set_title(title="late", updated_at=tx.database_now))
    repository.release(running.session_operation_context)
    assert authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled) == terminal


@pytest.mark.asyncio
async def test_terminal_atomic_publication_and_immutability(operation_store):
    engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert terminal.status == "completed" and terminal.session_operation_epoch == running.session_operation_context.fence.operation_epoch
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    with pytest.raises(ComposerOperationFenceLost):
        await service.update_session_title(sid, "late", session_operation_context=running.session_operation_context)
    for mutation in (update(jobs).values(result_sha256="0" * 64), delete(jobs)):
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(mutation.where(jobs.c.operation_id == record.operation_id))
    repository.compare_and_swap(running.session_operation_context)
    with pytest.raises(ComposerOperationFenceLost):
        repository.mutate(running.session_operation_context, lambda transaction: None)
    repository.release(running.session_operation_context)
    # Lifecycle cascade is permitted, independent deletes are refused.
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(sid)))
    assert authority.get(session_id=sid, operation_id=record.operation_id) is None


@pytest.mark.asyncio
async def test_cancelled_terminal_awaiter_joins_actual_sql_and_returns_committed_terminal(operation_store, monkeypatch):
    _engine, _repo, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    entered, release = Event(), Event()
    original = service._insert_chat_message

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_insert_chat_message", delayed)
    task = asyncio.create_task(
        service.complete_composer_async_operation(
            running,
            assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
            assistant_record=None,
            audit_cohort=(),
            audit_composition_state_id=None,
            build_response=response,
        )
    )
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    terminal = await task
    assert terminal.status == "completed"
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal


@pytest.mark.parametrize(
    "values",
    (
        {"claim_token": None},
        {"claim_owner_instance_id": None},
        {"session_operation_id": None},
        {"session_operation_lease_token": None},
        {"session_operation_epoch": None},
        {"started_at": None},
        {"request_json": None},
        {"claim_expires_at": "non-null"},
        {"attempt": 0},
    ),
)
def test_running_bundle_rejects_nullability_and_fence_holes(operation_store, values):
    engine, _repo, _authority, _service, _sid = operation_store
    record, _running = start(operation_store)
    if values == {"claim_expires_at": "non-null"}:
        with engine.connect() as conn:
            values = {"claim_expires_at": database_now(conn)}
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(**values))


def test_queued_cancel_settles_without_a_claim_and_retains_immutable_body(operation_store):
    engine, _repo, authority, _service, sid = operation_store
    record, _fresh = admit(authority, sid)
    terminal = authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    assert terminal.status == "failed" and terminal.failure_code == "request_cancelled"
    assert terminal.settled_by == "request_cancel" and terminal.session_operation_epoch is None
    assert terminal.request_json is None and authority.claim_next(limit=1) == ()
    with engine.connect() as conn:
        names = {entry["name"] for entry in inspect(conn).get_foreign_keys("composer_async_operations")}
        assert "fk_composer_operations_user_message_session" in names
        assert "fk_composer_operations_base_state_session" in names


@pytest.mark.asyncio
async def test_recompose_tool_prefix_is_eligible_bare_narration_is_not(operation_store):
    _engine, repository, authority, service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    user = await service.add_message(sid, "user", "Build", writer_principal="route_user_message", session_operation_context=context)
    await service.add_message(
        sid,
        "assistant",
        "I started",
        writer_principal="compose_loop",
        session_operation_context=context,
        tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "set_metadata", "arguments": "{}"}}],
    )
    repository.release(context)
    request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=user.id)
    record, running = start(operation_store, request)
    assert authority.get_for_turn(session_id=sid, operation_id=record.operation_id).user_message_id == user.id
    await service.add_message(
        sid, "assistant", "Saved reply", writer_principal="compose_loop", session_operation_context=running.session_operation_context
    )
    await service.fail_composer_async_operation(running, failure=failed())
    repository.release(running.session_operation_context)
    new_request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=user.id)
    admit(authority, sid, new_request)
    (claim,) = authority.claim_next(limit=1)
    with pytest.raises(ComposerOperationPreconditionRefused) as refused:
        repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    assert refused.value.error.body["detail"]["error_type"] == "recompose_already_completed"


@pytest.mark.asyncio
async def test_terminal_projection_error_replays_committed_terminal(operation_store, monkeypatch):
    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original = service._run_composer_sql

    async def committed_then_lost(func, *args, **kwargs):
        result = await original(func, *args, **kwargs)
        if isinstance(result, ComposerOperationRecord) and result.status == "completed":
            raise RuntimeError("lost acknowledgment")
        return result

    monkeypatch.setattr(service, "_run_composer_sql", committed_then_lost)
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert terminal == authority.get(session_id=sid, operation_id=record.operation_id)


@pytest.mark.asyncio
async def test_direct_sql_child_cancel_waits_for_actual_thread_completion(operation_store, monkeypatch):
    from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    children = []
    original = service._run_composer_sql

    async def capture_child(func):
        children.append(asyncio.current_task())
        return await original(func)

    def delayed_sql():
        entered.set()
        assert release.wait(5)
        completed.set()
        return authority.get_for_turn(session_id=sid, operation_id=record.operation_id)

    monkeypatch.setattr(service, "_run_composer_sql", capture_child)
    task = asyncio.create_task(service._run_composer_terminal_sql(running, delayed_sql))
    await _wait_for_thread_event(entered)
    children[0].cancel()
    await asyncio.sleep(0.03)
    assert not completed.is_set() and not task.done()
    release.set()
    result = await task
    assert completed.is_set() and result.status == "running"
    assert not isinstance(result, ComposerTerminalSQLCompletionUnknown)


@pytest.mark.asyncio
async def test_failed_fresh_writer_read_preserves_unknown_terminal_custody(operation_store, monkeypatch):
    from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    calls = []

    def sql_failure():
        calls.append("sql")
        raise RuntimeError("before commit")

    def unavailable_read(self, **kwargs):
        raise RuntimeError("writer unavailable")

    monkeypatch.setattr(type(authority), "get_with_database_now", unavailable_read)
    with pytest.raises(ComposerTerminalSQLCompletionUnknown):
        await service._run_composer_terminal_sql(running, sql_failure, can_retry=lambda row, now: True)
    assert calls == ["sql"]
    monkeypatch.undo()
    assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"


@pytest.mark.asyncio
async def test_unchanged_state_retries_once_after_actual_sql_completion(operation_store):
    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    calls = []

    def sql_failure_then_result():
        calls.append("sql")
        if len(calls) == 1:
            raise RuntimeError("rollback before commit")
        return authority.get_for_turn(session_id=sid, operation_id=record.operation_id)

    result = await service._run_composer_terminal_sql(running, sql_failure_then_result, can_retry=lambda row, now: True)
    assert calls == ["sql", "sql"] and result.status == "running"


def test_admission_binding_conflict_precedes_missing_base_membership(operation_store):
    _engine, _repository, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    admit(authority, sid, request)
    conflicting = SendMessageRequest(operation_id=request.operation_id, content="Changed", state_id=uuid4())
    with pytest.raises(ComposerOperationConflictError):
        admit(authority, sid, conflicting)
    # A fresh missing base is rejected before persistence once capacity permits.
    other = _repository.create_session_with_initial_fence(
        user_id="alice", title="Other", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    fresh = SendMessageRequest(operation_id=str(uuid4()), content="Build", state_id=uuid4())
    with pytest.raises(ComposerOperationPreconditionRefused) as refusal:
        admit(authority, other.id, fresh)
    assert refusal.value.error.http_status == 404
    assert authority.get(session_id=other.id, operation_id=fresh.operation_id) is None


@pytest.mark.asyncio
async def test_changed_stop_state_prevents_exact_retry(operation_store):
    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    calls = []

    def sql_rollback_with_stop():
        calls.append("sql")
        authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
        raise RuntimeError("rolled back")

    with pytest.raises(RuntimeError, match="rolled back"):
        await service._run_composer_terminal_sql(
            running, sql_rollback_with_stop, can_retry=lambda row, now: row.cancel_requested_at is None
        )
    assert calls == ["sql"]
    terminal = await service.fail_composer_async_operation(running, failure=failed())
    assert terminal.failure_code == "request_cancelled"


@pytest.mark.asyncio
async def test_real_schema_cross_session_and_cross_operation_bindings_are_rejected(operation_store):
    from sqlalchemy import insert

    from elspeth.web.sessions.models import message_ingress_receipts_table
    from elspeth.web.sessions.protocol import CompositionStateData

    engine, repository, authority, service, sid = operation_store
    other = repository.create_session_with_initial_fence(
        user_id="alice", title="Foreign", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    foreign_context = repository.acquire(
        session_id=other.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    foreign_user = await service.add_message(
        other.id, "user", "Foreign", writer_principal="route_user_message", session_operation_context=foreign_context
    )
    state = await service.save_composition_state(
        other.id, CompositionStateData(), provenance="session_seed", session_operation_context=foreign_context
    )
    repository.release(foreign_context)
    state_id = state.id
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build", state_id=state_id)
    with pytest.raises(ComposerOperationPreconditionRefused) as refusal:
        admit(authority, sid, request)
    assert refusal.value.error.http_status == 404 and authority.get(session_id=sid, operation_id=request.operation_id) is None
    record, running = start(operation_store)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(user_message_id=str(foreign_user.id)))
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(actor_user_id="missing-identity"))
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    with engine.connect() as conn:
        receipt = dict(
            conn.execute(select(message_ingress_receipts_table).where(message_ingress_receipts_table.c.session_id == str(sid)))
            .mappings()
            .one()
        )
    await service.fail_composer_async_operation(running, failure=failed())
    repository.release(running.session_operation_context)
    second, _ = admit(authority, sid)
    receipt["operation_id"] = second.operation_id
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(insert(message_ingress_receipts_table).values(**receipt))
    receipt["session_id"] = str(other.id)
    receipt["operation_id"] = record.operation_id
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(insert(message_ingress_receipts_table).values(**receipt))
    with engine.connect() as conn:
        assert (
            len(conn.execute(select(message_ingress_receipts_table).where(message_ingress_receipts_table.c.session_id == str(sid))).all())
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ("assistant", "audit", "terminal"))
async def test_terminal_transaction_fault_windows_keep_one_bundle(operation_store, monkeypatch, seam):
    import elspeth.web.sessions.service as service_module
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    builds = []
    calls = []

    def build(message, proposals, current):
        builds.append(message.id)
        return response(message, proposals, current)

    if seam == "assistant":
        original = service._insert_chat_message

        def inject(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs["role"] == "assistant":
                calls.append("assistant")
                raise RuntimeError("after assistant before commit")
            return value

        monkeypatch.setattr(service, "_insert_chat_message", inject)
    elif seam == "audit":
        original = service._write_audit_cohort_on_connection

        def inject(*args, **kwargs):
            original(*args, **kwargs)
            calls.append("audit")
            raise RuntimeError("after audit before commit")

        monkeypatch.setattr(service, "_write_audit_cohort_on_connection", inject)
    else:
        original = service_module.settle_composer_operation_on_connection

        def inject(*args, **kwargs):
            value = original(*args, **kwargs)
            calls.append("terminal")
            if len(calls) == 1:
                raise RuntimeError("after terminal CAS before commit")
            return value

        monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", inject)
    operation = service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(AuditMessageDraft(role="audit", content="Evidence"),),
        audit_composition_state_id=None,
        build_response=build,
    )
    if seam == "terminal":
        terminal = await operation
        assert terminal.status == "completed" and len(calls) == 2 and len(builds) == 1
        assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
        expected_roles = ["user", "assistant", "audit"]
    else:
        with pytest.raises(RuntimeError, match="before commit"):
            await operation
        assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
        assert calls == [seam] and builds == []
        expected_roles = ["user"]
    with engine.connect() as conn:
        roles = list(
            conn.execute(
                select(chat_messages_table.c.role)
                .where(chat_messages_table.c.session_id == str(sid))
                .order_by(chat_messages_table.c.sequence_no)
            ).scalars()
        )
    assert roles == expected_roles


def test_distinct_ids_race_has_one_active_winner(operation_store):
    _engine, _repository, authority, _service, sid = operation_store
    requests = [SendMessageRequest(operation_id=str(uuid4()), content="Build") for _ in range(4)]

    def submit(request):
        try:
            record, fresh = admit(authority, sid, request)
        except ComposerOperationActiveError as exc:
            return exc.operation_id, False
        return record.operation_id, fresh

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, requests))
    assert sum(fresh for _operation_id, fresh in results) == 1
    assert len({operation_id for operation_id, _fresh in results}) == 1
    assert authority.count_nonterminal(limit=10) == 1


def test_stale_claim_cannot_release_or_renew_reclaimed_work(operation_store):
    from datetime import timedelta

    engine, _repository, authority, _service, sid = operation_store
    record, _fresh = admit(authority, sid)
    (first,) = authority.claim_next(limit=1)
    with engine.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.operation_id == record.operation_id)
            .values(claim_expires_at=database_now(conn) - timedelta(seconds=1))
        )
    with pytest.raises(ComposerOperationFenceLost):
        authority.renew_claim(first)
    (second,) = authority.claim_next(limit=1)
    assert second.attempt == first.attempt + 1 and second.claim_token != first.claim_token
    with pytest.raises(ComposerOperationFenceLost):
        authority.release_claim(first)
    assert authority.renew_claim(second).claim_token_present
    authority.release_claim(second)
    current = authority.get(session_id=sid, operation_id=record.operation_id)
    assert current.status == "queued" and not current.claim_token_present


def _admit_in_independent_process(database_url: str, session_id: str, operation_id: str) -> tuple[str, bool]:
    engine = create_session_engine(database_url)
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
    try:
        try:
            record, fresh = admit(authority, UUID(session_id), SendMessageRequest(operation_id=operation_id, content="Build"))
        except ComposerOperationActiveError as exc:
            return exc.operation_id, False
        return record.operation_id, fresh
    finally:
        engine.dispose()


@pytest.mark.parametrize("same_id", (True, False))
def test_independent_process_admission_converges_on_one_action(operation_store, same_id):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    engine, _repository, authority, _service, sid = operation_store
    first = str(uuid4())
    ids = [first, first if same_id else str(uuid4())]
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_admit_in_independent_process, [engine.url.render_as_string(hide_password=False)] * 2, [str(sid)] * 2, ids))
    assert sum(fresh for _operation_id, fresh in results) == 1
    assert len({operation_id for operation_id, _fresh in results}) == 1
    assert authority.count_nonterminal(limit=10) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_state", ("stop", "deadline"))
@pytest.mark.parametrize("original_integrity", (False, True))
async def test_stop_between_fresh_read_and_retry_lock_refuses_success(operation_store, monkeypatch, changed_state, original_integrity):
    import elspeth.web.sessions.service as service_module
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store, deadline_seconds=3.0 if changed_state == "deadline" else 180.0)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original_settle = service_module.settle_composer_operation_on_connection
    original_read = type(authority).get_with_database_now
    original_error = (
        AuditIntegrityError("before commit acknowledgment") if original_integrity else RuntimeError("before commit acknowledgment")
    )
    calls = []
    read_calls = []
    builds = []

    def lose_before_commit(*args, **kwargs):
        original_settle(*args, **kwargs)
        calls.append("terminal")
        raise original_error

    def stop_after_snapshot(self, **kwargs):
        result = original_read(self, **kwargs)
        if not read_calls:
            read_calls.append("stop")
            if changed_state == "stop":
                authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
            else:
                import time

                time.sleep(4.1)
        return result

    def build(message, proposals, current):
        builds.append(message.id)
        return response(message, proposals, current)

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(service_module, "settle_composer_operation_on_connection", lose_before_commit)
        fault_patch.setattr(type(authority), "get_with_database_now", stop_after_snapshot)
        with pytest.raises(AuditIntegrityError if original_integrity else RuntimeError, match="before commit acknowledgment") as captured:
            await service.complete_composer_async_operation(
                running,
                assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
                assistant_record=None,
                audit_cohort=(),
                audit_composition_state_id=None,
                build_response=build,
            )
        assert calls == ["terminal"] and len(builds) == 1 and read_calls == ["stop"]
    assert captured.value is original_error
    terminal = await service.fail_composer_async_operation(
        running, failure=project_composer_operation_error(captured.value, request_id=None), authoritative_failure=original_integrity
    )
    assert terminal.failure_code == (
        "http_error" if original_integrity else "request_cancelled" if changed_state == "stop" else "deadline_expired"
    )
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            "user"
        ]


@pytest.mark.asyncio
async def test_new_job_actor_and_base_foreign_keys_have_unmasked_negative_controls(operation_store):
    from sqlalchemy import insert

    from elspeth.web.sessions.protocol import CompositionStateData

    engine, repository, authority, service, sid = operation_store
    parent, _fresh = admit(authority, sid)
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    state = await service.save_composition_state(sid, CompositionStateData(), provenance="session_seed", session_operation_context=context)
    repository.release(context)
    with engine.connect() as conn:
        template = dict(conn.execute(select(jobs).where(jobs.c.operation_id == parent.operation_id)).mappings().one())
    for field, invalid in (("actor_user_id", "absent-actor"), ("base_state_id", str(state.id))):
        other = repository.create_session_with_initial_fence(
            user_id="alice", title="No job", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
        )
        values = dict(template)
        values["session_id"] = str(other.id)
        values[field] = invalid
        request = SendMessageRequest(
            operation_id=parent.operation_id, content="Build", state_id=state.id if field == "base_state_id" else None
        )
        values["request_json"] = request.model_dump_json()
        values["request_hash"] = composer_operation_request_hash(session_id=other.id, kind="compose_message", request=request)
        with engine.begin() as conn, pytest.raises(IntegrityError) as error:
            conn.execute(insert(jobs).values(**values))
        assert "foreign key" in str(error.value).lower()
        assert authority.get(session_id=other.id, operation_id=parent.operation_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("hash", "shape"))
async def test_terminal_read_rejects_tampering_after_named_guard_removed(operation_store, tamper):
    from elspeth.contracts.errors import AuditIntegrityError

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(result_sha256="0" * 64))
    with engine.begin() as conn:
        sql = "DROP TRIGGER trg_composer_async_operations_transition_guard"
        if conn.dialect.name == "postgresql":
            sql += " ON composer_async_operations"
        conn.exec_driver_sql(sql)
        values = {"result_sha256": "0" * 64}
        if tamper == "shape":
            encoded = canonical_json({"unexpected": True})
            values = {"result_json": encoded, "result_sha256": terminal.result_sha256}
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(**values))
    with pytest.raises(AuditIntegrityError):
        authority.get(session_id=sid, operation_id=record.operation_id)


@pytest.mark.asyncio
async def test_final_job_base_delete_is_restricted_but_session_cascade_succeeds(operation_store):
    from sqlalchemy import delete

    from elspeth.web.sessions.models import composition_states_table, sessions_table
    from elspeth.web.sessions.protocol import CompositionStateData

    engine, repository, authority, service, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    state = await service.save_composition_state(sid, CompositionStateData(), provenance="session_seed", session_operation_context=context)
    repository.release(context)
    record, fresh = admit(authority, sid, SendMessageRequest(operation_id=str(uuid4()), content="Build", state_id=state.id))
    assert fresh and record.base_state_id == state.id
    with engine.begin() as conn, pytest.raises(IntegrityError, match=r"(?i)foreign key"):
        conn.execute(delete(composition_states_table).where(composition_states_table.c.id == str(state.id)))
    with engine.connect() as conn:
        assert conn.execute(
            select(composition_states_table.c.id).where(composition_states_table.c.id == str(state.id))
        ).scalar_one() == str(state.id)
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(sid)))
    with engine.connect() as conn:
        assert conn.execute(select(jobs.c.operation_id).where(jobs.c.session_id == str(sid))).all() == []
        assert conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == str(sid))).all() == []


@pytest.mark.parametrize("changed_state", ("stop", "deadline"))
@pytest.mark.parametrize("original_integrity", (False, True))
def test_queued_settlement_rechecks_priority_under_owned_claim_lock(operation_store, changed_state, original_integrity):
    import time

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    _engine, _repository, authority, _service, sid = operation_store
    record, _fresh = admit(authority, sid, deadline_seconds=3.0 if changed_state == "deadline" else 180.0)
    (claim,) = authority.claim_next(limit=1)
    original = AuditIntegrityError("captured accounting evidence") if original_integrity else RuntimeError("captured start fault")
    failure = project_composer_operation_error(original, request_id="correlation")
    if changed_state == "stop":
        marked = authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
        assert marked.status == "queued" and marked.cancel_requested_at is not None
    else:
        time.sleep(4.1)
    terminal = authority.settle_unstarted(
        claim, session_id=sid, operation_id=record.operation_id, failure=failure, authoritative_failure=original_integrity
    )
    assert terminal.failure_code == (
        "http_error" if original_integrity else "request_cancelled" if changed_state == "stop" else "deadline_expired"
    )
    assert terminal.status == "failed" and terminal.settled_by == "settle_unstarted" and terminal.user_message_id is None
    assert authority.settle_unstarted(claim, session_id=sid, operation_id=record.operation_id, failure=failed()) == terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_window", ("before_driver_commit", "after_driver_commit"))
async def test_driver_commit_fault_reconciles_one_immutable_terminal(operation_store, monkeypatch, commit_window):
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    driver_commit = engine.dialect.do_commit
    commits = []
    builds = []

    def fault_at_driver(connection):
        commits.append(commit_window)
        if len(commits) == 1:
            if commit_window == "after_driver_commit":
                driver_commit(connection)
            raise RuntimeError("driver commit acknowledgment lost")
        driver_commit(connection)

    def build(message, proposals, current):
        builds.append(message.id)
        return response(message, proposals, current)

    monkeypatch.setattr(engine.dialect, "do_commit", fault_at_driver)
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(),
        audit_composition_state_id=None,
        build_response=build,
    )
    assert terminal.status == "completed" and len(builds) == 1
    assert len(commits) == (2 if commit_window == "before_driver_commit" else 1)
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            "user",
            "assistant",
        ]


async def _assert_operation_rows_absent(engine, sid):
    with engine.connect() as conn:
        assert conn.execute(select(jobs.c.operation_id).where(jobs.c.session_id == str(sid))).all() == []


@pytest.mark.asyncio
async def test_final_job_actor_delete_has_unmasked_restrict_control(operation_store):
    from sqlalchemy import delete, insert
    from tests.fixtures.identities import ensure_test_identity

    from elspeth.web.sessions.models import identities_table, sessions_table

    engine, repository, authority, _service, sid = operation_store
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="unbound-actor")
        assert conn.execute(delete(identities_table).where(identities_table.c.identity_id == "unbound-actor")).rowcount == 1
        ensure_test_identity(conn, identity_id="unbound-actor")
    record, _fresh = admit(authority, sid)
    with engine.connect() as conn:
        values = dict(conn.execute(select(jobs).where(jobs.c.operation_id == record.operation_id)).mappings().one())
    other = repository.create_session_with_initial_fence(
        user_id="alice", title="Actor control", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    values["session_id"] = str(other.id)
    values["actor_user_id"] = "unbound-actor"
    request = SendMessageRequest(operation_id=record.operation_id, content="Build")
    values["request_hash"] = composer_operation_request_hash(session_id=other.id, kind="compose_message", request=request)
    with engine.begin() as conn:
        conn.execute(insert(jobs).values(**values))
    with engine.begin() as conn, pytest.raises(IntegrityError, match=r"(?i)foreign key"):
        conn.execute(delete(identities_table).where(identities_table.c.identity_id == "unbound-actor"))
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(other.id)))
        assert conn.execute(delete(identities_table).where(identities_table.c.identity_id == "unbound-actor")).rowcount == 1
    await _assert_operation_rows_absent(engine, other.id)


@pytest.mark.asyncio
async def test_final_job_user_delete_guard_and_foreign_key_are_independent(operation_store):
    from sqlalchemy import delete

    from elspeth.web.sessions.models import chat_messages_table, sessions_table

    engine, _repository, _authority, service, sid = operation_store
    record, running = start(operation_store)
    ingress = await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(delete(chat_messages_table).where(chat_messages_table.c.id == str(ingress.message.id)))
    with engine.begin() as conn:
        sql = "DROP TRIGGER trg_chat_messages_no_delete"
        if conn.dialect.name == "postgresql":
            sql += " ON chat_messages"
        conn.exec_driver_sql(sql)
        ingress_sql = "DROP TRIGGER trg_message_ingress_receipts_no_delete"
        if conn.dialect.name == "postgresql":
            ingress_sql += " ON message_ingress_receipts"
        conn.exec_driver_sql(ingress_sql)
    with engine.begin() as conn, pytest.raises(IntegrityError, match=r"(?i)foreign key"):
        conn.execute(delete(chat_messages_table).where(chat_messages_table.c.id == str(ingress.message.id)))
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(sid)))
    await _assert_operation_rows_absent(engine, sid)


@pytest.mark.asyncio
async def test_exact_retry_preserves_entire_assistant_audit_and_response_bundle(operation_store, monkeypatch):
    import time

    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, _authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original_insert = service._insert_chat_message
    original_commit = engine.dialect.do_commit
    writes = []
    commits = []

    def capture_insert(conn, **kwargs):
        identifier = original_insert(conn, **kwargs)
        writes.append((identifier, kwargs["role"], kwargs["created_at"], kwargs["sequence_no"], kwargs["content"]))
        return identifier

    def lose_first_commit(connection):
        commits.append("commit")
        if len(commits) == 1:
            time.sleep(1.1)
            raise RuntimeError("first complete immutable bundle rolled back")
        original_commit(connection)

    monkeypatch.setattr(service, "_insert_chat_message", capture_insert)
    monkeypatch.setattr(engine.dialect, "do_commit", lose_first_commit)
    terminal = await service.complete_composer_async_operation(
        running,
        assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
        assistant_record=None,
        audit_cohort=(AuditMessageDraft(role="audit", content="evidence"),),
        audit_composition_state_id=None,
        build_response=response,
    )
    assert writes[:2] == writes[2:] and len(writes) == 4 and len(commits) == 2
    with engine.connect() as conn:
        stored = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == writes[0][0])).one()
    sealed = MessageWithStateResponse.model_validate_json(terminal.result_json, strict=True)
    assert sealed.message.created_at == service._row_to_chat_message_record(stored).created_at == writes[0][2]


@pytest.mark.asyncio
async def test_failed_terminal_has_one_exact_immutable_retry(operation_store, monkeypatch):
    import elspeth.web.sessions.service as service_module

    _engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    original_settle = service_module.settle_composer_operation_on_connection
    bundles = []

    def lose_first_ack(*args, **kwargs):
        bundles.append((kwargs["outcome"], kwargs["settled_at"]))
        result = original_settle(*args, **kwargs)
        if len(bundles) == 1:
            raise RuntimeError("failed terminal precommit acknowledgment")
        return result

    monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", lose_first_ack)
    terminal = await service.fail_composer_async_operation(running, failure=failed())
    assert terminal.status == "failed" and terminal.failure_code == "worker_lost"
    assert len(bundles) == 2 and bundles[0] == bundles[1]
    assert terminal.settled_at == bundles[0][1]
    assert authority.get(session_id=sid, operation_id=record.operation_id) == terminal


@pytest.mark.asyncio
async def test_required_ingress_cancellation_retains_actual_sql_until_binding_commit(operation_store, monkeypatch):
    from elspeth.web.sessions.models import chat_messages_table, message_ingress_receipts_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    entered, release = Event(), Event()
    original_insert = service._insert_chat_message

    def parked_insert(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(service, "_insert_chat_message", parked_insert)
    task = asyncio.create_task(
        service.add_message_with_transcript(
            sid,
            "user",
            "Build",
            operation_id=UUID(record.operation_id),
            requested_state_id=None,
            writer_principal="route_user_message",
            session_operation_context=running.session_operation_context,
            running=running,
        )
    )
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    current = authority.get(session_id=sid, operation_id=record.operation_id)
    assert current.status == "running" and current.user_message_id is not None
    with engine.connect() as conn:
        assert conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.session_id == str(sid))).scalars().all() == [
            str(current.user_message_id)
        ]
        assert conn.execute(
            select(message_ingress_receipts_table.c.user_message_id).where(message_ingress_receipts_table.c.session_id == str(sid))
        ).scalars().all() == [str(current.user_message_id)]


@pytest.mark.asyncio
async def test_failed_terminal_prewrite_fence_refusal_preserves_recovery_authority(operation_store):
    from elspeth.web.coordination.contracts import SessionOperationFenceLost

    _engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    repository.release(running.session_operation_context)
    with pytest.raises(SessionOperationFenceLost):
        await service.fail_composer_async_operation(running, failure=failed())
    current = authority.get(session_id=sid, operation_id=record.operation_id)
    assert current.status == "running" and current.result_json is None


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["audit", "assistant"])
async def test_required_audit_sql_failure_preserves_canonical_integrity_classification(operation_store, monkeypatch, seam):
    from sqlalchemy.exc import OperationalError

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    original_error = OperationalError("controlled required write", {}, RuntimeError("storage fault"))
    if seam == "audit":

        def refuse_audit(*args, **kwargs):
            raise original_error

        monkeypatch.setattr(service, "_write_audit_cohort_on_connection", refuse_audit)
    else:
        original_insert = service._insert_chat_message

        def refuse_assistant(*args, **kwargs):
            if kwargs["role"] == "assistant":
                raise original_error
            return original_insert(*args, **kwargs)

        monkeypatch.setattr(service, "_insert_chat_message", refuse_assistant)
    expected = AuditIntegrityError if seam == "audit" else OperationalError
    with pytest.raises(expected) as caught:
        await service.complete_composer_async_operation(
            running,
            assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
            assistant_record=None,
            audit_cohort=(AuditMessageDraft(role="audit", content="Evidence"),),
            audit_composition_state_id=None,
            build_response=response,
        )
    if seam == "audit":
        assert caught.value.__cause__ is original_error
    else:
        assert caught.value is original_error
    assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            "user"
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["intermediate", "cohort"])
async def test_required_intermediate_cancellation_joins_actual_sql(operation_store, monkeypatch, writer):
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.models import chat_messages_table

    engine, _repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    entered, release = Event(), Event()
    original_insert = service._insert_chat_message

    def parked_insert(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(service, "_insert_chat_message", parked_insert)
    if writer == "intermediate":
        work = service.persist_compose_turn_async(
            session_id=str(sid),
            assistant_content="Observed",
            redacted_assistant_tool_calls=(),
            redacted_tool_rows=(),
            parent_composition_state_id=None,
            expected_current_state_id=None,
            writer_principal="compose_loop",
            plugin_crash_pending=False,
            session_operation_context=running.session_operation_context,
        )
        expected_role = "assistant"
    else:
        work = service.add_messages_atomic(
            sid,
            (AuditMessageDraft(role="audit", content="Observed"),),
            writer_principal="compose_loop",
            session_operation_context=running.session_operation_context,
            audit_only=True,
        )
        expected_role = "audit"
    task = asyncio.create_task(work)
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with engine.connect() as conn:
        assert list(conn.execute(select(chat_messages_table.c.role).where(chat_messages_table.c.session_id == str(sid))).scalars()) == [
            expected_role
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["begin", "cancel", "settle"])
async def test_required_provider_sql_cancellation_joins_committed_evidence(operation_store, monkeypatch, phase):
    from datetime import UTC, datetime

    from elspeth.web.coordination.quota_authority import TokenUsageEntry
    from elspeth.web.sessions import service as service_module
    from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table

    engine, _repository, _authority, service, _sid = operation_store
    _record, running = start(operation_store)
    context = running.session_operation_context
    from tests.fixtures.identities import grant_test_pipeline_user

    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    attempt = None
    if phase != "begin":
        attempt = await service.begin_provider_attempt(session_operation_context=context, source="auto_title")
    entered, release = Event(), Event()
    calls = []
    if phase == "begin":
        owner, name = service, "_begin_provider_attempt_sync"
        original = service._begin_provider_attempt_sync
    elif phase == "cancel":
        owner, name = service_module, "cancel_undispatched_provider_attempt_on_connection"
        original = service_module.cancel_undispatched_provider_attempt_on_connection
    else:
        owner, name = service, "_settle_provider_attempt_sync"
        original = service._settle_provider_attempt_sync

    def parked(*args, **kwargs):
        calls.append(phase)
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, parked)
    if phase == "begin":
        work = service.begin_provider_attempt(session_operation_context=context, source="auto_title")
    elif phase == "cancel":
        work = service.cancel_undispatched_provider_attempt(
            session_operation_context=context, attempt_id=attempt.attempt_id, requested_model="test/title"
        )
    else:
        work = service.settle_provider_attempt(
            session_operation_context=context,
            attempt_id=attempt.attempt_id,
            entry=TokenUsageEntry(
                model="test/title",
                prompt_tokens=3,
                completion_tokens=2,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                call_id=attempt.attempt_id,
                recorded_at=datetime.now(UTC),
            ),
        )
    task = asyncio.create_task(work)
    await _wait_for_thread_event(entered)
    task.cancel()
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()
    with engine.connect() as conn:
        assert list(conn.execute(select(token_usage_ledger_table.c.entry_id)).scalars()) == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [phase]
    with engine.connect() as conn:
        attempts = conn.execute(select(quota_provider_attempts_table)).all()
        ledger = conn.execute(select(token_usage_ledger_table)).all()
    assert len(attempts) == 1
    if phase == "begin":
        assert attempts[0].settled_at is None and ledger == []
    else:
        assert attempts[0].settled_at is not None and len(ledger) == 1
        assert ledger[0].prompt_tokens == (0 if phase == "cancel" else 3)


@pytest.mark.asyncio
async def test_queued_provider_admission_cancellation_does_not_create_attempt(operation_store, monkeypatch):
    from tests.fixtures.identities import grant_test_pipeline_user

    from elspeth.web import async_workers
    from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table

    engine, _repository, _authority, service, _sid = operation_store
    _record, running = start(operation_store)
    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    occupied, release, submitted = Event(), Event(), Event()

    class WitnessExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            result = super().submit(fn, *args, **kwargs)
            submitted.set()
            return result

    def occupy():
        occupied.set()
        assert release.wait(timeout=5)

    with WitnessExecutor(max_workers=1) as executor:
        blocker = executor.submit(occupy)
        await _wait_for_thread_event(occupied)
        submitted.clear()
        monkeypatch.setattr(async_workers, "_get_shared_executor", lambda: executor)
        task = asyncio.create_task(
            service.begin_provider_attempt(session_operation_context=running.session_operation_context, source="auto_title")
        )
        try:
            await _wait_for_thread_event(submitted)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not blocker.done()
            with engine.connect() as conn:
                assert conn.execute(select(quota_provider_attempts_table)).all() == []
                assert conn.execute(select(token_usage_ledger_table)).all() == []
        finally:
            release.set()
        blocker.result(timeout=5)
    with engine.connect() as conn:
        assert conn.execute(select(quota_provider_attempts_table)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed"])
async def test_each_status_bundle_constraints_are_unmasked_and_null_closed(operation_store, status):
    engine, _repository, authority, service, sid = operation_store
    if status in ("queued", "failed"):
        record, _fresh = admit(authority, sid)
        if status == "failed":
            authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    else:
        record, running = start(operation_store)
        if status == "completed":
            await service.add_message_with_transcript(
                sid,
                "user",
                "Build",
                operation_id=UUID(record.operation_id),
                requested_state_id=None,
                writer_principal="route_user_message",
                session_operation_context=running.session_operation_context,
                running=running,
            )
            await service.complete_composer_async_operation(
                running,
                assistant=ComposerOperationAssistantWrite(message_id=uuid4(), content="Done", raw_content=None, composition_state_id=None),
                assistant_record=None,
                audit_cohort=(),
                audit_composition_state_id=None,
                build_response=response,
            )
    with engine.begin() as conn:
        sql = "DROP TRIGGER trg_composer_async_operations_transition_guard"
        if conn.dialect.name == "postgresql":
            sql += " ON composer_async_operations"
        conn.exec_driver_sql(sql)
        now = database_now(conn)
        assert conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(status=status)).rowcount == 1
        if status == "queued":
            holes = (
                {"request_json": None},
                {"claim_token": "partial"},
                {"started_at": now},
                {"session_operation_id": "partial"},
                {"result_json": "{}"},
                {"settled_at": now},
                {"failure_code": "worker_lost"},
            )
        elif status == "running":
            holes = (
                {"request_json": None},
                {"claim_token": None},
                {"claim_owner_instance_id": None},
                {"started_at": None},
                {"session_operation_id": None},
                {"session_operation_lease_token": None},
                {"session_operation_epoch": None},
                {"claim_expires_at": now},
                {"result_json": "{}"},
                {"attempt": 0},
            )
        elif status == "completed":
            holes = (
                {"request_json": "{}"},
                {"claim_token": "partial"},
                {"claim_expires_at": now},
                {"claim_owner_instance_id": None},
                {"started_at": None},
                {"result_json": None},
                {"result_sha256": None},
                {"settled_at": None},
                {"settled_by": None},
                {"result_schema": None},
                {"user_message_id": None},
                {"cancel_requested_at": now},
                {"failure_code": "worker_lost"},
                {"session_operation_id": None},
                {"session_operation_lease_token": None},
                {"session_operation_epoch": None},
            )
        else:
            holes = (
                {"request_json": "{}"},
                {"claim_token": "partial"},
                {"claim_expires_at": now},
                {"result_json": None},
                {"result_sha256": None},
                {"settled_at": None},
                {"failure_code": None},
                {"settled_by": None},
                {"result_schema": None},
                {"session_operation_id": "partial"},
                {"session_operation_lease_token": "partial"},
                {"session_operation_epoch": 1},
                {"started_at": now},
            )
        for values in holes:
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(**values))
        assert conn.execute(select(jobs.c.status).where(jobs.c.operation_id == record.operation_id)).scalar_one() == status


@pytest.mark.parametrize("recovery", ["compose", "foreign_kind", "foreign_owner"])
def test_fresh_recovery_requires_persisted_compose_and_owned_instance(operation_store, recovery):
    from elspeth.contracts.session_operation import SessionOperationContext

    _engine, repository, authority, _service, sid = operation_store
    record, running = start(operation_store)
    repository.release(running.session_operation_context)
    kind = SessionOperationKind.PROPOSAL if recovery == "foreign_kind" else SessionOperationKind.COMPOSE
    fresh = repository.acquire(session_id=sid, operation_kind=kind, owner_instance_id="test-owner", lease_seconds=30)
    assert fresh is not None
    context = SessionOperationContext(fence=fresh.fence, operation_kind=SessionOperationKind.COMPOSE)
    if recovery == "foreign_owner":
        authority = ComposerAsyncOperationAuthority(_engine, owner_instance_id="foreign-owner", claim_lease_seconds=30)
    try:
        if recovery == "compose":
            terminal = authority.settle_lost(
                session_operation_context=context,
                session_id=sid,
                operation_id=record.operation_id,
                failure=failed(),
                authoritative_failure=True,
            )
            assert terminal.status == "failed" and terminal.settled_by == "settle_lost"
        else:
            with pytest.raises(ComposerOperationFenceLost):
                authority.settle_lost(
                    session_operation_context=context,
                    session_id=sid,
                    operation_id=record.operation_id,
                    failure=failed(),
                    authoritative_failure=True,
                )
            assert authority.get(session_id=sid, operation_id=record.operation_id).status == "running"
    finally:
        repository.release(fresh)


@pytest.mark.asyncio
async def test_expired_job_refuses_business_write_at_database_clock(operation_store, monkeypatch):
    from datetime import timedelta

    from elspeth.web.coordination import composer_operation_authority as job_authority
    from elspeth.web.sessions.composer_operations import ComposerTurnDeadlineExpired

    engine, repository, _authority, service, sid = operation_store
    record, running = start(operation_store, deadline_seconds=1.0)
    with engine.connect() as conn:
        deadline = conn.execute(select(jobs.c.deadline_at).where(jobs.c.operation_id == record.operation_id)).scalar_one()
    from elspeth.web.sessions.time_normalization import restore_utc

    monkeypatch.setattr(job_authority, "database_now", lambda conn: restore_utc(deadline) + timedelta(microseconds=1))
    with pytest.raises(ComposerTurnDeadlineExpired) as refused:
        await service.update_session_title(sid, "after deadline", session_operation_context=running.session_operation_context)
    assert refused.value.operation_id == record.operation_id
    assert refused.value.session_id == sid
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(sid))).scalar_one() == "Durable"
    # Sealed failure remains available under the exact live fence.
    failure = ComposerOperationError(
        http_status=504,
        failure_code="deadline_expired",
        error_type="deadline_expired",
        body={"error_type": "deadline_expired", "detail": "The composer deadline expired."},
        diagnostic_id=None,
    )
    terminal = await service.fail_composer_async_operation(running, failure=failure)
    assert terminal.status == "failed"
    assert terminal.result_json == canonical_json(failure.model_dump(mode="json"))
    repository.release(running.session_operation_context)
