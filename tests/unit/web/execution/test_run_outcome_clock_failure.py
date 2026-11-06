"""A post-spend Sessions outage must not erase a known Landscape outcome."""

from __future__ import annotations

import asyncio
from functools import partial

import pytest
import structlog
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts import CallStatus
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.provider_clock import RunProviderAttemptClock
from elspeth.web.execution.service import _run_token_usage_entries
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.fixtures.landscape import leader_coordination_token
from tests.helpers import execution_custody
from tests.helpers.composition_state import composition_state_data
from tests.unit.core.landscape.test_call_recording import _setup_with_operation
from tests.unit.plugins.clients.test_audited_llm_client import FakeOpenAIClient, empty_choices_response, provider_response
from tests.unit.web.execution.test_session_operation_lease import _execution_service

execution_fixture = execution_custody.execution_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp_outcome", [False, True])
@pytest.mark.parametrize("outcome", ["known_success", "unknown_success", "known_error", "unknown_error"])
async def test_sessions_outage_after_spend_keeps_exact_known_landscape_outcome(
    tmp_path, monkeypatch, execution_fixture, stamp_outcome: bool, outcome: str
) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
        grant_test_pipeline_user(connection, identity_id="alice")
    sessions = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run_outcome_clock_failure"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
    )
    session = await sessions.create_session(user_id="alice", title="post-spend outage", auth_provider_type="local")
    async with await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=30,
    ) as compose:
        state = await sessions.save_composition_state(
            session.id, composition_state_data(is_valid=True), provenance="session_seed", session_operation_context=compose.context
        )
    async with await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=30,
    ) as execute:
        run = await sessions.create_run(session.id, state.id, session_operation_context=execute.context)
        db, factory, _state_id, operation_id = _setup_with_operation(run_id=str(run.id))
        execution, _, _ = _execution_service(asyncio.get_running_loop(), execution_fixture)
        execution._session_service = sessions
        clock = RunProviderAttemptClock()
        if outcome == "known_success":
            provider = FakeOpenAIClient(response=provider_response(model="provider/actual"))
        elif outcome == "unknown_success":
            provider = FakeOpenAIClient(response=provider_response(model="provider/actual", usage=None))
        elif outcome == "known_error":
            provider = FakeOpenAIClient(
                response=empty_choices_response(model="provider/actual", usage={"prompt_tokens": 10, "completion_tokens": 5})
            )
        else:
            provider = FakeOpenAIClient(exception=RuntimeError("provider timeout after dispatch"))
        sdk_create = provider.chat.completions.create
        sessions_unavailable = False

        def spend_then_outage(*args, **kwargs):
            nonlocal sessions_unavailable
            try:
                return sdk_create(*args, **kwargs)
            finally:
                sessions_unavailable = True

        def fail_sessions_sql(*args):
            if sessions_unavailable:
                raise OperationalError("Sessions unavailable after provider spend", {}, RuntimeError("lost database"))

        monkeypatch.setattr(provider.chat.completions, "create", spend_then_outage)
        event.listen(engine, "before_cursor_execute", fail_sessions_sql)
        governance = LLMCallGovernance(
            before_call=partial(execution._admit_run_llm_call, run.id, execute, clock=clock),
            after_call=partial(
                execution._settle_run_llm_call,
                run.id,
                execute,
                landscape_db=db,
                landscape_run_id=str(run.id),
            ),
            outcome_timestamp=clock.timestamp_for_outcome if stamp_outcome else None,
        )
        client = AuditedLLMClient(
            execution=factory.execution,
            state_id=None,
            operation_id=operation_id,
            coordination_token=leader_coordination_token(factory, str(run.id)),
            run_id=str(run.id),
            telemetry_emit=lambda event: None,
            underlying_client=provider,
            llm_call_governance=governance,
        )
        try:
            with pytest.raises(OperationalError, match="Sessions unavailable"):
                await run_sync_in_worker(client.chat_completion, model="requested/model", messages=[ChatMessage(role="user", content="hi")])
        finally:
            sessions_unavailable = False
            event.remove(engine, "before_cursor_execute", fail_sessions_sql)
        calls = factory.execution.get_operation_calls(operation_id)
        with engine.connect() as connection:
            attempt = connection.execute(select(quota_provider_attempts_table)).one()
            ledger = connection.execute(select(token_usage_ledger_table)).all()
        assert len(provider.create_calls) == 1
        assert ledger == [] and attempt.settled_at is None
        assert len(calls) == 1, f"stamp_outcome={stamp_outcome}: expected durable known outcome after physical spend, got {len(calls)}"
        assert calls[0].call_id == attempt.attempt_id
        assert (calls[0].prompt_tokens, calls[0].completion_tokens) == ((10, 5) if outcome.startswith("known") else (None, None))
        assert calls[0].status is (CallStatus.ERROR if outcome.endswith("error") else CallStatus.SUCCESS)
        retained_entries = _run_token_usage_entries(db, landscape_run_id=str(run.id))
    engine.dispose()
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    replacement = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run_outcome_clock_replay"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
    )
    async with await SessionOperationLease.acquire(
        replacement.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=replacement.session_operation_owner_instance_id,
        lease_seconds=30,
    ) as recovered:
        for _ in range(2):
            await replacement.record_token_usage(
                session_operation_context=recovered.context, source="run", run_id=run.id, entries=retained_entries
            )
        with engine.connect() as connection:
            settled = connection.execute(select(quota_provider_attempts_table)).one()
            usage = connection.execute(select(token_usage_ledger_table)).one()
        assert settled.ledger_entry_id == usage.entry_id and settled.settled_at is not None
        assert (usage.prompt_tokens, usage.completion_tokens) == ((10, 5) if outcome.startswith("known") else (None, None))
        assert usage.model == retained_entries[0].model
        assert usage.recorded_at == retained_entries[0].recorded_at.replace(tzinfo=None)
    db.close()
    engine.dispose()
