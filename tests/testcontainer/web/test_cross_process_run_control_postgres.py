"""Independent PostgreSQL processes contend through the production run authorities."""

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select
from tests.fixtures.identities import ensure_test_identity
from tests.testcontainer.web.test_global_run_recovery_postgres import _register_live_instance

from elspeth.contracts.chargeable_admission import ChargeableAdmissionPolicy
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.coordination.run_cancellation_authority import RepositoryRunCancellationAuthority
from elspeth.web.execution.envelope import RunExecutionInput
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    composition_states_table,
    run_events_table,
    run_execution_inputs_table,
    run_start_permits_table,
    runs_table,
)
from elspeth.web.sessions.protocol import RunAlreadyActiveError
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer
_NO_QUOTA_POLICY = ChargeableAdmissionPolicy(
    identity_token_quota_configured=False,
    container_token_quota_configured=False,
    secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
)


def _envelope():
    return RunExecutionInput(
        schema_version=1,
        envelope_json="{}",
        canonical_input_digest="a" * 64,
        topology_digest="b" * 64,
        source_manifest_digest="c" * 64,
        application_fingerprint="d" * 64,
        plugin_registry_fingerprint="e" * 64,
        configuration_fingerprint="f" * 64,
        graph_fingerprint="a" * 64,
        runtime_fingerprint="b" * 64,
        implementation_fingerprint="c" * 64,
        deployment_generation="test",
        session_epoch=1,
        landscape_epoch=1,
        coordination_protocol=1,
        automatic_recovery_eligible=True,
    )


def _contend(url, context, state_id, run_id, action, barrier, results):
    engine = create_session_engine(url)
    try:
        authority = PostgresSessionOperationRepository(engine)
        barrier.wait(timeout=60)
        if action == "admit":
            try:
                authority.mutate(
                    context,
                    lambda tx: tx.runs.create_pending_run(
                        run_id=run_id,
                        state_id=state_id,
                        pipeline_yaml=None,
                        started_at=datetime.now(UTC),
                        execution_input=_envelope(),
                    ),
                )
                results.put(("admitted", str(run_id)))
            except RunAlreadyActiveError:
                results.put(("lost", str(run_id)))
        elif action == "permit":
            permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run_id, policy=_NO_QUOTA_POLICY))
            results.put(("permit", permit.state.value))
        elif action == "cancel":
            run = RepositoryRunCancellationAuthority(engine).request(
                run_id,
                session_id=UUID(context.fence.session_id),
                user_id="alice",
                auth_provider_type="local",
            )
            results.put(("cancel", run.status))
        else:
            raise AssertionError(action)
    finally:
        engine.dispose()


def _race(url, context, state_id, first_run_id, second_run_id, actions):
    spawn = multiprocessing.get_context("spawn")
    barrier = spawn.Barrier(2)
    results = spawn.Queue()
    processes = [
        spawn.Process(target=_contend, args=(url, context, state_id, run_id, action, barrier, results))
        for run_id, action in zip((first_run_id, second_run_id), actions, strict=True)
    ]
    try:
        for process in processes:
            process.start()
        messages = [results.get(timeout=90) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        return messages
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()


def _prepare(url):
    engine = create_session_engine(url)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    authority = PostgresSessionOperationRepository(engine)
    owner = f"process-admission-{uuid4()}"
    _register_live_instance(engine, owner)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="process race", auth_provider_type="local", owner_instance_id=owner, lease_seconds=300
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id=owner, lease_seconds=300
    )
    state_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id), session_id=str(session.id), version=1, provenance="session_seed", created_at=datetime.now(UTC)
            )
        )
    return engine, authority, context, state_id


def test_independent_process_admission_has_one_winner_and_no_loser_rows(external_deployment_postgres_url):
    url = external_deployment_postgres_url
    engine, _, context, state_id = _prepare(url)
    try:
        first, second = uuid4(), uuid4()
        results = _race(url, context, state_id, first, second, ("admit", "admit"))
        assert sorted(result[0] for result in results) == ["admitted", "lost"]
        winner = next(run_id for outcome, run_id in results if outcome == "admitted")
        loser = next(run_id for outcome, run_id in results if outcome == "lost")
        with engine.connect() as conn:
            assert conn.execute(select(runs_table.c.id).where(runs_table.c.session_id == context.fence.session_id)).scalars().all() == [
                winner
            ]
            for table in (run_execution_inputs_table, run_start_permits_table):
                assert conn.execute(select(func.count()).select_from(table).where(table.c.run_id == winner)).scalar_one() == 1
                assert conn.execute(select(func.count()).select_from(table).where(table.c.run_id == loser)).scalar_one() == 0
    finally:
        engine.dispose()


def test_independent_process_permit_cancel_race_has_one_linearization(external_deployment_postgres_url):
    url = external_deployment_postgres_url
    engine, authority, context, state_id = _prepare(url)
    try:
        run_id = uuid4()
        authority.mutate(
            context,
            lambda tx: tx.runs.create_pending_run(
                run_id=run_id, state_id=state_id, pipeline_yaml=None, started_at=datetime.now(UTC), execution_input=_envelope()
            ),
        )
        results = _race(url, context, state_id, run_id, run_id, ("permit", "cancel"))
        permit_outcome = next(state for action, state in results if action == "permit")
        with engine.connect() as conn:
            permit = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == str(run_id))).one()
            run = conn.execute(select(runs_table).where(runs_table.c.id == str(run_id))).one()
            assert run.cancel_requested_at is not None
            assert permit.start_state == permit_outcome
            events = conn.execute(select(run_events_table).where(run_events_table.c.run_id == str(run_id))).all()
            if permit.start_state == "cancelled_before_permit":
                assert run.status == "cancelled"
                assert permit.permit_id is None
                assert len(events) == 1
                assert events[0].event_type == "cancelled"
                assert events[0].data["source_rows_processed"] == 0
            else:
                assert permit.start_state == "start_permitted"
                assert events == []
                assert run.status == "pending"
                assert permit.permit_id is not None
    finally:
        engine.dispose()
