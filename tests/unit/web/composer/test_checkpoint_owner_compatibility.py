"""Legacy fenced checkpoints and explicit required checkpoints share real SQL."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.turn_audit import persist_turn_audit
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
    make_required_work_key,
)
from elspeth.web.sessions.models import chat_messages_table
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_actual_checkpoint_preserves_legacy_and_required_ownership(tmp_path, required):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Checkpoint", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = (
        RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4())))
        if required
        else None
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN) if coordinator is not None else None
    try:
        outcome = await persist_turn_audit(
            sessions_service=service,
            redaction_telemetry=NoopRedactionTelemetry(),
            tool_outcomes=(),
            decoded_args_by_call_id={},
            assistant_message=_AdmittedAssistantMessage("Exact checkpoint prose"),
            raw_assistant_content=None,
            assistant_tool_calls=(),
            crash_pending=False,
            session_id=str(session.id),
            session_operation_context=context,
            current_state_id=None,
            persisted_tool_call_turn=False,
            persisted_assistant_message_id=None,
            persisted_assistant_content=None,
            assistant_row_uses_current_dispatch=True,
            required_work=binding,
        )
        with engine.connect() as conn:
            rows = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).mappings().all()
        assert len(rows) == 1
        assert rows[0]["role"] == "assistant" and rows[0]["content"] == "Exact checkpoint prose"
        assert outcome.persisted_assistant_message_id == rows[0]["id"]
        assert outcome.persisted_assistant_content == "Exact checkpoint prose"
        if coordinator is not None:
            assert coordinator.all_completed
            assert {ticket.key for ticket in coordinator.tickets} == {
                make_required_work_key(coordinator.authority, RequiredWorkSource.COMPOSE_CHECKPOINT_SQL),
                make_required_work_key(coordinator.authority, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION),
            }
    finally:
        authority.release(context)


@pytest.mark.asyncio
async def test_null_binding_never_bypasses_exact_checkpoint_context(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="No checkpoint bypass", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    with pytest.raises(AuditIntegrityError, match="exact session operation authority"):
        await persist_turn_audit(
            sessions_service=service,
            redaction_telemetry=NoopRedactionTelemetry(),
            tool_outcomes=(),
            decoded_args_by_call_id={},
            assistant_message=_AdmittedAssistantMessage("Forbidden checkpoint"),
            raw_assistant_content=None,
            assistant_tool_calls=(),
            crash_pending=False,
            session_id=str(session.id),
            session_operation_context=None,
            current_state_id=None,
            persisted_tool_call_turn=False,
            persisted_assistant_message_id=None,
            persisted_assistant_content=None,
            assistant_row_uses_current_dispatch=True,
        )
    with engine.connect() as conn:
        assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["binding", "provider_owner"])
async def test_direct_compose_rejects_half_required_pair_before_provider_or_sql(tmp_path, monkeypatch, missing):
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.composer.service import ComposerServiceImpl
    from tests.unit.web.composer.test_service import _empty_state, _make_settings, _mock_catalog

    engine, authority, sessions = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Mixed owner", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    owner = ProviderInvocationOwner(service=sessions, required_work=binding)
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(), settings=_make_settings(), sessions_service=sessions, session_engine=engine
    )
    called = []

    async def forbidden_dispatch(*args, **kwargs):
        called.append(True)
        raise AssertionError("mixed owner reached physical dispatch")

    monkeypatch.setattr("litellm.acompletion", forbidden_dispatch)
    try:
        with pytest.raises(AuditIntegrityError, match="supplied with its turn binding"):
            await composer.compose(
                "Hello",
                [],
                _empty_state(),
                session_id=str(session.id),
                session_operation_context=context,
                required_work=None if missing == "binding" else binding,
                provider_owner=None if missing == "provider_owner" else owner,
            )
        assert called == []
        assert coordinator.tickets == ()
        with engine.connect() as conn:
            assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).all() == []
    finally:
        authority.release(context)


@pytest.mark.asyncio
async def test_actual_pending_checkpoint_keeps_projection_and_release_unresolved(tmp_path, monkeypatch):
    import asyncio
    import threading

    from elspeth.web import async_workers
    from elspeth.web.required_work import RequiredWorkIncomplete

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Pending checkpoint", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    entered, release = threading.Event(), threading.Event()

    original_persist = service.persist_compose_turn
    delivered = []
    original_retain = async_workers._retain_cancellation
    task = None

    def park(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_persist(*args, **kwargs)

    def observe_original(cancellations, cancellation):
        if asyncio.current_task() is task and all(cancellation is not original for original in delivered):
            delivered.append(cancellation)
        original_retain(cancellations, cancellation)

    monkeypatch.setattr(service, "persist_compose_turn", park)
    monkeypatch.setattr(async_workers, "_retain_cancellation", observe_original)
    try:
        task = asyncio.create_task(
            persist_turn_audit(
                sessions_service=service,
                redaction_telemetry=NoopRedactionTelemetry(),
                tool_outcomes=(),
                decoded_args_by_call_id={},
                assistant_message=_AdmittedAssistantMessage("Joined checkpoint"),
                raw_assistant_content=None,
                assistant_tool_calls=(),
                crash_pending=False,
                session_id=str(session.id),
                session_operation_context=context,
                current_state_id=None,
                persisted_tool_call_turn=False,
                persisted_assistant_message_id=None,
                persisted_assistant_content=None,
                assistant_row_uses_current_dispatch=True,
                required_work=binding,
            )
        )
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel("original checkpoint cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        assert len(coordinator.tickets) == 2
        assert all(not ticket.complete for ticket in coordinator.tickets)
        with pytest.raises(RequiredWorkIncomplete):
            coordinator.assert_completed()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original checkpoint cancellation") as caught:
            await task
        assert len(delivered) == 1 and caught.value is delivered[0]
        assert coordinator.all_completed
        with engine.connect() as conn:
            rows = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).mappings().all()
        assert len(rows) == 1 and rows[0]["content"] == "Joined checkpoint"
    finally:
        release.set()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        authority.release(context)


@pytest.mark.asyncio
async def test_foreign_checkpoint_binding_refuses_before_registration_or_sql(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    sessions = [
        authority.create_session_with_initial_fence(
            user_id="alice", title="Exact scope", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
        )
        for _ in range(2)
    ]
    contexts = [
        authority.acquire(session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60)
        for session in sessions
    ]
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, contexts[1], invocation_id=str(uuid4()))
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    try:
        with pytest.raises(AuditIntegrityError, match="another exact fence"):
            await persist_turn_audit(
                sessions_service=service,
                redaction_telemetry=NoopRedactionTelemetry(),
                tool_outcomes=(),
                decoded_args_by_call_id={},
                assistant_message=_AdmittedAssistantMessage("Foreign checkpoint"),
                raw_assistant_content=None,
                assistant_tool_calls=(),
                crash_pending=False,
                session_id=str(sessions[0].id),
                session_operation_context=contexts[0],
                current_state_id=None,
                persisted_tool_call_turn=False,
                persisted_assistant_message_id=None,
                persisted_assistant_content=None,
                assistant_row_uses_current_dispatch=True,
                required_work=binding,
            )
        assert coordinator.tickets == ()
        with engine.connect() as conn:
            assert conn.execute(select(chat_messages_table)).all() == []
    finally:
        for context in contexts:
            authority.release(context)


@pytest.mark.asyncio
async def test_required_checkpoint_sql_error_retains_original_and_failed_turn(tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Failed checkpoint", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    original = OperationalError("controlled checkpoint SQL", {}, RuntimeError("controlled checkpoint cause"))
    entered = []

    def fail(*args, **kwargs):
        entered.append(True)
        raise original

    monkeypatch.setattr(service, "_insert_chat_message", fail)
    try:
        with pytest.raises(AuditIntegrityError) as caught:
            await persist_turn_audit(
                sessions_service=service,
                redaction_telemetry=NoopRedactionTelemetry(),
                tool_outcomes=(),
                decoded_args_by_call_id={},
                assistant_message=_AdmittedAssistantMessage("Rolled back checkpoint"),
                raw_assistant_content=None,
                assistant_tool_calls=(),
                crash_pending=False,
                session_id=str(session.id),
                session_operation_context=context,
                current_state_id=None,
                persisted_tool_call_turn=False,
                persisted_assistant_message_id=None,
                persisted_assistant_content=None,
                assistant_row_uses_current_dispatch=True,
                required_work=binding,
            )
        assert entered == [True]
        assert caught.value.__cause__ is original
        assert caught.value.failed_turn is not None
        assert caught.value.failed_turn.assistant_message_id is None
        assert caught.value.failed_turn.tool_responses_persisted == 0
        assert coordinator.all_completed
        assert any(receipt.original_root is caught.value for receipt in coordinator.failure_receipts())
        with engine.connect() as conn:
            assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).all() == []
    finally:
        authority.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_generation", [False, True])
async def test_checkpoint_no_return_retains_projection_until_actual_generation_closure(tmp_path, monkeypatch, expire_generation):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from elspeth.web import async_workers
    from elspeth.web.required_work import RequiredWorkIncomplete
    from tests.fixtures.required_executor import RecordingRequiredGenerationRecovery
    from tests.unit.web.test_required_executor_custody import QueueThenRaiseExecutor

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice",
        title="Unknown checkpoint submission",
        auth_provider_type="local",
        owner_instance_id="owner",
        lease_seconds=60,
    )
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="owner",
        lease_seconds=60,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    executor = QueueThenRaiseExecutor(max_workers=1)
    entered, release = threading.Event(), threading.Event()
    blocker = ThreadPoolExecutor.submit(executor, lambda: (entered.set(), release.wait(5)))
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.001)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    recorder = RecordingRequiredGenerationRecovery()
    draining, unavailable = threading.Event(), threading.Event()
    async_workers.configure_required_executor_recovery(
        drain_seconds=0.05 if expire_generation else 10,
        instance_draining=draining,
        generation_unavailable=unavailable,
        recovery_callback=recorder,
    )
    invoked = []
    original_persist = service.persist_compose_turn

    def observe_sql(*args, **kwargs):
        invoked.append(True)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(service, "persist_compose_turn", observe_sql)
    task = asyncio.create_task(
        persist_turn_audit(
            sessions_service=service,
            redaction_telemetry=NoopRedactionTelemetry(),
            tool_outcomes=(),
            decoded_args_by_call_id={},
            assistant_message=_AdmittedAssistantMessage("Never invoked"),
            raw_assistant_content=None,
            assistant_tool_calls=(),
            crash_pending=False,
            session_id=str(session.id),
            session_operation_context=context,
            current_state_id=None,
            persisted_tool_call_turn=False,
            persisted_assistant_message_id=None,
            persisted_assistant_content=None,
            assistant_row_uses_current_dispatch=True,
            required_work=binding,
        )
    )
    generation = None
    try:
        async with asyncio.timeout(5):
            while async_workers._GENERATION_CUSTODIAN is None or async_workers._GENERATION_CUSTODIAN.state != "quarantined":
                await asyncio.sleep(0.001)
            generation = async_workers._GENERATION_CUSTODIAN
            if expire_generation:
                while not recorder.observations:
                    await asyncio.sleep(0.001)
        assert not task.done() and not generation.joined.is_set()
        assert len(coordinator.tickets) == 2
        assert all(not ticket.complete for ticket in coordinator.tickets)
        with pytest.raises(RequiredWorkIncomplete):
            coordinator.assert_completed()
        assert invoked == [] and not blocker.done()
        if expire_generation:
            assert draining.is_set() and unavailable.is_set()
        release.set()
        with pytest.raises(RuntimeError, match="submit failed after queueing") as caught:
            await task
        async with asyncio.timeout(5):
            while not generation.recovery_finished.is_set():
                await asyncio.sleep(0.001)
        assert generation.joined.is_set() and blocker.done()
        assert coordinator.all_completed and invoked == []
        sql_ticket = next(
            ticket
            for ticket in coordinator.tickets
            if ticket.key == make_required_work_key(coordinator.authority, RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
        )
        assert sql_ticket.errors == ()
        receipts = sql_ticket.receipts()
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.key.originating_key == sql_ticket.key
        assert isinstance(receipt.original_root, BaseExceptionGroup)
        assert any(error is caught.value for error in receipt.original_root.exceptions)
        markers = [error for error in receipt.original_root.exceptions if error is not caught.value]
        assert len(markers) == 1
        assert markers[0].__cause__ is caught.value
        with engine.connect() as conn:
            assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).all() == []
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        if generation is not None:
            async with asyncio.timeout(5):
                while not generation.recovery_finished.is_set():
                    await asyncio.sleep(0.001)
        await async_workers.shutdown_async_workers()
        authority.release(context)
