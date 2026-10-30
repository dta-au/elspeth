"""Recover a committed provider outcome using only the two durable stores."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
import structlog
from sqlalchemy import select

from elspeth.contracts import NodeType
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.chargeable_admission import ChargeableAdmissionPolicy, ChargeableAdmissionRefused
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.execution import calls as landscape_calls
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.provider_clock import RunProviderAttemptClock
from elspeth.web.execution.recovery import RunRecoveryCoordinator, observe_run
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.fixtures.landscape import leader_coordination_token
from tests.helpers.composition_state import composition_state_data
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.plugins.clients.test_audited_llm_client import FakeOpenAIClient, provider_response


@pytest.mark.asyncio
@pytest.mark.parametrize("known_usage", [True, False])
@pytest.mark.parametrize("clock_year", [2000, 2099])
async def test_restart_settles_orphan_from_explicit_call_identity(
    tmp_path, monkeypatch: pytest.MonkeyPatch, known_usage: bool, clock_year: int
) -> None:
    sessions_url = f"sqlite:///{tmp_path / 'sessions.db'}"
    landscape_url = f"sqlite:///{tmp_path / 'landscape.db'}"
    engine = create_session_engine(sessions_url)
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
        grant_test_pipeline_user(connection, identity_id="alice")
        seed_token_policies(connection, identity_id="alice")

    def sessions_service(engine):
        return SessionServiceImpl(
            engine,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.run_crash_recovery"),
            session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
            chargeable_admission_policy=ChargeableAdmissionPolicy(
                identity_token_quota_configured=True,
                secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
            ),
        )

    sessions = sessions_service(engine)
    # A fast or slow Web/Landscape host clock must not move the spend into a
    # different UTC day from the authority that admitted it.
    monkeypatch.setattr(landscape_calls, "now", lambda: datetime(clock_year, 1, 1, tzinfo=UTC))
    session = await sessions.create_session(user_id="alice", title="crashed worker", auth_provider_type="local")
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

        def lose_worker(_attempt: str, _call: str) -> None:
            raise RuntimeError("worker lost before Sessions settlement")

        with LandscapeDB.from_url(landscape_url) as db:
            # No payload store: model attribution must survive independently
            # of the optional response archive.
            factory = RecorderFactory(db)
            factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=str(run.id))
            leader = leader_coordination_token(factory, str(run.id))
            node = factory.data_flow.register_node(
                coordination_token=leader,
                plugin_name="llm",
                node_type=NodeType.SOURCE,
                plugin_version="1.0",
                config={"model": "requested/deployment"},
                schema_config=SchemaConfig.from_dict({"mode": "observed"}),
            )
            operation = factory.execution.begin_operation(coordination_token=leader, node_id=node.node_id, operation_type="source_load")
            provider_clock = RunProviderAttemptClock()

            def admit() -> str:
                attempt = sessions.begin_run_provider_attempt_sync(session_operation_context=execute.context, run_id=run.id)
                provider_clock.bind(attempt)
                return attempt.attempt_id

            client = AuditedLLMClient(
                execution=factory.execution,
                state_id=None,
                operation_id=operation.operation_id,
                coordination_token=leader,
                run_id=str(run.id),
                telemetry_emit=lambda event: None,
                underlying_client=FakeOpenAIClient(
                    response=provider_response(model="provider/actual", usage=None)
                    if not known_usage
                    else provider_response(model="provider/actual")
                ),
                llm_call_governance=LLMCallGovernance(
                    before_call=admit,
                    after_call=lose_worker,
                    outcome_timestamp=provider_clock.timestamp_for_outcome,
                ),
            )
            with pytest.raises(RuntimeError, match="worker lost"):
                await run_sync_in_worker(
                    client.chat_completion, model="requested/deployment", messages=[ChatMessage(role="user", content="hello")]
                )
            calls = factory.execution.get_operation_calls(operation.operation_id)
            assert len(calls) == 1
            assert calls[0].returned_model == "provider/actual"
            assert calls[0].response_ref is None
            assert calls[0].created_at.year != clock_year
        with engine.connect() as connection:
            pending = connection.execute(select(quota_provider_attempts_table)).one()
            assert pending.settled_at is None
            assert pending.attempt_id == calls[0].call_id
            assert connection.execute(select(token_usage_ledger_table)).all() == []
        with pytest.raises(AuditIntegrityError, match="no admitted clock binding"):
            await run_sync_in_worker(provider_clock.timestamp_for_outcome, "different-attempt")
    # Discard both the old service and the old database connections. No
    # attempt/call tuple is delivered to the replacement recovery worker.
    engine.dispose()
    engine = create_session_engine(sessions_url)
    sessions = sessions_service(engine)
    coordinator = RunRecoveryCoordinator(
        sessions,
        Mock(spec=ExecutionServiceImpl),
        BlobServiceImpl(engine, tmp_path / "blobs"),
        landscape_url=landscape_url,
        create_tables=False,
    )
    observation = await run_sync_in_worker(observe_run, landscape_url, str(run.id), create_tables=False)
    async with await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=30,
    ) as recovered:
        # A replacement worker must replay the durable timestamp; it has no
        # admission receipt with which to stamp this old physical outcome.
        with pytest.raises(AuditIntegrityError, match="no admitted clock binding"):
            await run_sync_in_worker(RunProviderAttemptClock().timestamp_for_outcome, observation.token_usage_entries[0].call_id)
        for _ in range(2):
            await coordinator._reconcile_token_usage(await sessions.get_run(run.id), observation, recovered)
        with engine.connect() as connection:
            settled = connection.execute(select(quota_provider_attempts_table)).one()
            ledger = connection.execute(select(token_usage_ledger_table)).one()
        assert settled.settled_at is not None
        assert settled.ledger_entry_id == ledger.entry_id
        assert ledger.model == "provider/actual"
        assert ledger.recorded_at == observation.token_usage_entries[0].recorded_at.replace(tzinfo=None)
        assert (ledger.prompt_tokens, ledger.completion_tokens) == ((10, 5) if known_usage else (None, None))
        if known_usage:
            await run_sync_in_worker(sessions.begin_run_provider_attempt_sync, session_operation_context=recovered.context, run_id=run.id)
        else:
            with pytest.raises(ChargeableAdmissionRefused):
                await run_sync_in_worker(
                    sessions.begin_run_provider_attempt_sync, session_operation_context=recovered.context, run_id=run.id
                )
    engine.dispose()
