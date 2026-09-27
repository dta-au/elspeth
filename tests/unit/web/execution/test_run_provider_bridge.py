"""The pipeline worker owns its quota admission and settlement receipts."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any, Never
from unittest.mock import patch

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableAdmissionRefused
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.landscape.schema import calls_table
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.service import ExecutionServiceImpl, _run_token_usage_entries
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions import service as session_service_module
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.audit_hashing import fake_sha256
from tests.fixtures.identities import ensure_test_identity
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db
from tests.fixtures.mock_audit import mock_audit_authority
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.plugins.clients.test_audited_llm_client import FakeExecutionRepository, FakeOpenAIClient, provider_response


class _ForbiddenAsyncBridge:
    """Count an accidental legacy bridge call and fail at that boundary."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, coro: Coroutine[Any, Any, Any]) -> Never:
        self.calls += 1
        coro.close()
        raise AssertionError("run quota entered the async bridge")


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", ["before_commit", "after_commit", "rollback"])
async def test_run_admission_keeps_worker_and_lease_until_receipt(tmp_path, monkeypatch: pytest.MonkeyPatch, pause: str) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'run-bridge.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
        seed_token_policies(connection, identity_id="alice")
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run_bridge"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
        chargeable_admission_policy=ChargeableAdmissionPolicy(
            identity_token_quota_configured=True,
            secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
        ),
    )
    session = await service.create_session(user_id="alice", title="run bridge", auth_provider_type="local")
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as compose_lease:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_lease.context,
        )
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as execute_lease:
        run = await service.create_run(session.id, state.id, session_operation_context=execute_lease.context)
        entered = threading.Event()
        release = threading.Event()
        bridge_call = _ForbiddenAsyncBridge()
        bridge = SimpleNamespace(_session_service=service, _call_async=bridge_call)

        if pause in {"before_commit", "rollback"}:
            original_begin = session_service_module.begin_provider_attempt_on_connection

            def blocked_begin(*args, **kwargs):
                attempt = original_begin(*args, **kwargs)
                entered.set()
                if not release.wait(5):
                    raise AssertionError("precommit release never arrived")
                if pause == "rollback":
                    raise RuntimeError("forced admission rollback")
                return attempt

            monkeypatch.setattr(session_service_module, "begin_provider_attempt_on_connection", blocked_begin)
        else:
            original_begin_sync = service.begin_run_provider_attempt_sync

            def blocked_delivery(**kwargs):
                attempt = original_begin_sync(**kwargs)
                entered.set()
                if not release.wait(5):
                    raise AssertionError("postcommit release never arrived")
                return attempt

            monkeypatch.setattr(service, "begin_run_provider_attempt_sync", blocked_delivery)

        admitted = asyncio.create_task(run_sync_in_worker(ExecutionServiceImpl._admit_run_llm_call, bridge, run.id, execute_lease))
        try:
            entered_before_deadline = await run_sync_in_worker(entered.wait, 5)
            if not entered_before_deadline:
                await admitted
            assert entered_before_deadline
            assert not admitted.done()
            with engine.connect() as connection:
                before = connection.execute(select(quota_provider_attempts_table)).all()
            assert len(before) == (1 if pause == "after_commit" else 0)
            assert not execute_lease.closed
        finally:
            release.set()
        if pause == "rollback":
            with pytest.raises(RuntimeError, match="forced admission rollback"):
                await asyncio.wait_for(admitted, 5)
            with engine.connect() as connection:
                assert connection.execute(select(quota_provider_attempts_table)).all() == []
            assert bridge_call.calls == 0
        else:
            attempt_id = await asyncio.wait_for(admitted, 5)
            with engine.connect() as connection:
                after = connection.execute(select(quota_provider_attempts_table)).one()
            assert after.attempt_id == attempt_id
            assert after.source == "run" and after.run_id == str(run.id)
            assert after.settled_at is None
            assert bridge_call.calls == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_run_refusal_stops_dispatch_and_records_owner_quota(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'run-quota-refusal.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
        seed_token_policies(connection, identity_id="alice")
    refusals = []
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run_refusal"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
        chargeable_admission_policy=ChargeableAdmissionPolicy(
            identity_token_quota_configured=True,
            secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
        ),
        quota_exceeded_recorder=refusals.append,
    )
    session = await service.create_session(user_id="alice", title="run refusal", auth_provider_type="local")
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as compose_lease:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_lease.context,
        )
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as execute_lease:
        run = await service.create_run(session.id, state.id, session_operation_context=execute_lease.context)
        bridge_call = _ForbiddenAsyncBridge()
        bridge = SimpleNamespace(_session_service=service, _call_async=bridge_call)
        provider_entries: list[str] = []
        attempt_id = await run_sync_in_worker(ExecutionServiceImpl._admit_run_llm_call, bridge, run.id, execute_lease)
        provider_entries.append(attempt_id)
        actual_entry = TokenUsageEntry(
            model="test/model",
            prompt_tokens=1000,
            completion_tokens=0,
            cached_prompt_tokens=0,
            reasoning_tokens=0,
            call_id="landscape-call-1",
            recorded_at=datetime.now(UTC),
        )
        landscape = make_landscape_db()
        settled = threading.Event()
        release_settlement = threading.Event()
        settle_sync = service.settle_run_provider_attempt_sync

        def blocked_settlement(**kwargs):
            settle_sync(**kwargs)
            settled.set()
            if not release_settlement.wait(5):
                raise AssertionError("postcommit settlement receipt was not released")

        monkeypatch.setattr(service, "settle_run_provider_attempt_sync", blocked_settlement)
        with patch("elspeth.web.execution.service._run_token_usage_entries", return_value=(actual_entry,)):
            settlement = asyncio.create_task(
                run_sync_in_worker(
                    ExecutionServiceImpl._settle_run_llm_call,
                    bridge,
                    run.id,
                    execute_lease,
                    attempt_id,
                    actual_entry.call_id,
                    landscape_db=landscape,
                    landscape_run_id="landscape-run-1",
                )
            )
            try:
                assert await run_sync_in_worker(settled.wait, 5)
                assert not settlement.done() and not execute_lease.closed
                with engine.connect() as connection:
                    assert len(connection.execute(select(token_usage_ledger_table)).all()) == 1
            finally:
                release_settlement.set()
            await asyncio.wait_for(settlement, 5)
            await run_sync_in_worker(
                ExecutionServiceImpl._settle_run_llm_call,
                bridge,
                run.id,
                execute_lease,
                attempt_id,
                actual_entry.call_id,
                landscape_db=landscape,
                landscape_run_id="landscape-run-1",
            )
            with pytest.raises(AuditIntegrityError, match="contradicts"):
                await run_sync_in_worker(
                    service.settle_run_provider_attempt_sync,
                    session_operation_context=execute_lease.context,
                    attempt_id=attempt_id,
                    entry=TokenUsageEntry(
                        model="test/model",
                        prompt_tokens=999,
                        completion_tokens=0,
                        cached_prompt_tokens=0,
                        reasoning_tokens=0,
                        call_id="landscape-call-1",
                        recorded_at=actual_entry.recorded_at,
                    ),
                )
        with pytest.raises(ChargeableAdmissionRefused) as caught:
            await run_sync_in_worker(ExecutionServiceImpl._admit_run_llm_call, bridge, run.id, execute_lease)
        assert caught.value.decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
        assert provider_entries == [attempt_id]
        assert len(refusals) == 1
        assert refusals[0].identity_id == "alice" and refusals[0].operation == "run"
        with engine.connect() as connection:
            attempts = connection.execute(select(quota_provider_attempts_table)).all()
            ledger = connection.execute(select(token_usage_ledger_table)).all()
        assert len(attempts) == len(ledger) == 1
        assert attempts[0].settled_at is not None
        assert ledger[0].run_id == str(run.id) and ledger[0].prompt_tokens == 1000
        assert bridge_call.calls == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_committed_run_admission_lost_receipt_is_integrity_failure_without_sdk_entry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'run-admission-uncertain.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run_admission_uncertain"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
    )
    session = await service.create_session(user_id="alice", title="uncertain receipt", auth_provider_type="local")
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as compose_lease:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_lease.context,
        )
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as execute_lease:
        run = await service.create_run(session.id, state.id, session_operation_context=execute_lease.context)
        original = service._session_process_locked_begin

        @contextmanager
        def lost_receipt(session_id):
            with original(session_id) as connection:
                yield connection
            raise OperationalError("COMMIT", None, RuntimeError("injected lost commit acknowledgement"))

        monkeypatch.setattr(service, "_session_process_locked_begin", lost_receipt)
        bridge_call = _ForbiddenAsyncBridge()
        bridge = SimpleNamespace(_session_service=service, _call_async=bridge_call)
        provider = FakeOpenAIClient(response=provider_response())
        execution = FakeExecutionRepository()

        def unexpected_settlement(_attempt_id: str, _call_id: str) -> Never:
            raise AssertionError("no settlement without provider call")

        client = AuditedLLMClient(
            execution=execution,
            state_id="state-1",
            run_id="run-1",
            telemetry_emit=lambda event: None,
            underlying_client=provider,
            llm_call_governance=LLMCallGovernance(
                partial(ExecutionServiceImpl._admit_run_llm_call, bridge, run.id, execute_lease),
                unexpected_settlement,
            ),
            **mock_audit_authority(),
        )
        with pytest.raises(AuditIntegrityError, match=r"run provider admission.*uncertain") as caught:
            await run_sync_in_worker(client.chat_completion, model="gpt-4", messages=[ChatMessage(role="user", content="hello")])
        assert isinstance(caught.value.__cause__, OperationalError)
        with engine.connect() as connection:
            attempt = connection.execute(select(quota_provider_attempts_table)).one()
            ledger = connection.execute(select(token_usage_ledger_table)).all()
        assert attempt.settled_at is None and ledger == []
        assert provider.create_calls == [] and execution.recorded_calls == []
        assert bridge_call.calls == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_run_settlement_lost_receipt_replays_exact_real_landscape_call(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'run-settlement-uncertain.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run_settlement_uncertain"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
    )
    session = await service.create_session(user_id="alice", title="settlement receipt", auth_provider_type="local")
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as compose_lease:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_lease.context,
        )
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as execute_lease:
        run = await service.create_run(session.id, state.id, session_operation_context=execute_lease.context)
        bridge_call = _ForbiddenAsyncBridge()
        bridge = SimpleNamespace(_session_service=service, _call_async=bridge_call)
        attempt_id = await run_sync_in_worker(ExecutionServiceImpl._admit_run_llm_call, bridge, run.id, execute_lease)

        landscape = make_landscape_db()
        factory = make_factory(landscape)
        landscape_run_id = "landscape-run-settlement"
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=landscape_run_id)
        coordination = leader_coordination_token(factory, landscape_run_id)
        schema = SchemaConfig.from_dict({"mode": "observed"})
        source = factory.data_flow.register_node(
            coordination_token=coordination,
            plugin_name="inline_blob",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            schema_config=schema,
        )
        llm = factory.data_flow.register_node(
            coordination_token=coordination,
            plugin_name="llm",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={"model": "provider/real-model"},
            schema_config=schema,
        )
        _row, token = factory.data_flow.create_row_with_token(
            coordination_token=coordination,
            source_node_id=source.node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={"text": "prompt"},
        )
        state_record = factory.execution.record_completed_node_state(
            token_id=token.token_id,
            node_id=llm.node_id,
            coordination_token=coordination,
            step_index=1,
            input_data={"text": "prompt"},
            output_data={"answer": "ok"},
            duration_ms=1.0,
        )
        call_id = "real-landscape-llm-call"
        recorded_at = datetime.now(UTC)
        with landscape.write_connection() as connection:
            connection.execute(
                calls_table.insert().values(
                    call_id=call_id,
                    state_id=state_record.state_id,
                    call_index=0,
                    call_type=CallType.LLM.value,
                    status=CallStatus.SUCCESS.value,
                    request_hash=fake_sha256("real-landscape-llm-call-request"),
                    created_at=recorded_at,
                    prompt_tokens=7,
                    completion_tokens=4,
                )
            )
        assert _run_token_usage_entries(landscape, landscape_run_id=landscape_run_id) == (
            TokenUsageEntry(
                model="provider/real-model",
                prompt_tokens=7,
                completion_tokens=4,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                call_id=call_id,
                recorded_at=recorded_at,
            ),
        )
        settle_on_connection = session_service_module.settle_provider_attempt_on_connection

        def rollback_settlement(*args, **kwargs):
            settle_on_connection(*args, **kwargs)
            raise RuntimeError("forced settlement rollback")

        monkeypatch.setattr(session_service_module, "settle_provider_attempt_on_connection", rollback_settlement)
        with pytest.raises(RuntimeError, match="forced settlement rollback"):
            await run_sync_in_worker(
                ExecutionServiceImpl._settle_run_llm_call,
                bridge,
                run.id,
                execute_lease,
                attempt_id,
                call_id,
                landscape_db=landscape,
                landscape_run_id=landscape_run_id,
            )
        with engine.connect() as connection:
            rolled_back_attempt = connection.execute(select(quota_provider_attempts_table)).one()
            assert connection.execute(select(token_usage_ledger_table)).all() == []
        assert rolled_back_attempt.settled_at is None
        monkeypatch.setattr(session_service_module, "settle_provider_attempt_on_connection", settle_on_connection)
        original = service._session_process_locked_begin

        @contextmanager
        def lost_receipt(session_id):
            with original(session_id) as connection:
                yield connection
            raise OperationalError("COMMIT", None, RuntimeError("injected lost settlement acknowledgement"))

        monkeypatch.setattr(service, "_session_process_locked_begin", lost_receipt)
        with pytest.raises(AuditIntegrityError, match=r"run provider settlement.*uncertain") as caught:
            await run_sync_in_worker(
                ExecutionServiceImpl._settle_run_llm_call,
                bridge,
                run.id,
                execute_lease,
                attempt_id,
                call_id,
                landscape_db=landscape,
                landscape_run_id=landscape_run_id,
            )
        assert isinstance(caught.value.__cause__, OperationalError)
        with engine.connect() as connection:
            after_commit = connection.execute(select(token_usage_ledger_table)).one()
        assert after_commit.model == "provider/real-model"
        assert (after_commit.prompt_tokens, after_commit.completion_tokens) == (7, 4)
        assert (after_commit.source, after_commit.session_id, after_commit.run_id) == ("run", str(session.id), str(run.id))
        assert after_commit.recorded_at.replace(tzinfo=UTC) == recorded_at
        monkeypatch.setattr(service, "_session_process_locked_begin", original)
        await run_sync_in_worker(
            ExecutionServiceImpl._settle_run_llm_call,
            bridge,
            run.id,
            execute_lease,
            attempt_id,
            call_id,
            landscape_db=landscape,
            landscape_run_id=landscape_run_id,
        )
        with engine.connect() as connection:
            attempts = connection.execute(select(quota_provider_attempts_table)).all()
            ledger = connection.execute(select(token_usage_ledger_table)).all()
        assert len(attempts) == len(ledger) == 1
        assert attempts[0].ledger_entry_id == ledger[0].entry_id
        with pytest.raises(AuditIntegrityError, match="exactly one durable LLM call"):
            await run_sync_in_worker(
                ExecutionServiceImpl._settle_run_llm_call,
                bridge,
                run.id,
                execute_lease,
                attempt_id,
                "different-call",
                landscape_db=landscape,
                landscape_run_id=landscape_run_id,
            )
        with landscape.write_connection() as connection:
            connection.execute(calls_table.update().where(calls_table.c.call_id == call_id).values(prompt_tokens=8))
        with pytest.raises(AuditIntegrityError, match="contradicts"):
            await run_sync_in_worker(
                ExecutionServiceImpl._settle_run_llm_call,
                bridge,
                run.id,
                execute_lease,
                attempt_id,
                call_id,
                landscape_db=landscape,
                landscape_run_id=landscape_run_id,
            )
        assert bridge_call.calls == 0
    engine.dispose()
