"""Real PostgreSQL identity locks serialize chargeable permits and disable writes."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic, sleep
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, event, insert, select, update
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationKind, StartPermitState
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.coordination.run_cancellation_authority import RepositoryRunCancellationAuthority
from elspeth.web.execution.envelope import RunExecutionInput
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composition_states_table, identities_table, run_start_permits_table, runs_table
from elspeth.web.sessions.protocol import RunRecord, RunStartPermitRecord, SessionOperationMutationTransaction
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer

_POLICY = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)


@pytest.fixture
def admission_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"chargeable_admission_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        yield engine
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _admit(engine: Engine) -> tuple[PostgresSessionOperationRepository, SessionOperationContext, RunRecord]:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    authority = PostgresSessionOperationRepository(engine)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="chargeable admission", auth_provider_type="local", owner_instance_id="owner", lease_seconds=120
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id="owner", lease_seconds=120
    )
    state_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id),
                session_id=str(session.id),
                version=1,
                provenance="session_seed",
                created_at=datetime.now(UTC),
            )
        )
    envelope = RunExecutionInput(
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
    run = authority.mutate(
        context,
        lambda tx: tx.runs.create_pending_run(
            run_id=uuid4(),
            state_id=state_id,
            pipeline_yaml=None,
            started_at=datetime.now(UTC),
            execution_input=envelope,
        ),
    )
    return authority, context, run


def _issue(authority: PostgresSessionOperationRepository, context: SessionOperationContext, run_id: UUID) -> RunStartPermitRecord:
    return authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run_id, policy=_POLICY))


def _assert_lock_wait(engine: Engine, waiter: int, blocker: int) -> None:
    """Observe the exact blocker; time only bounds the probe, never decides success."""
    deadline = monotonic() + 10
    last = None
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while monotonic() < deadline:
            last = observer.exec_driver_sql(
                "SELECT wait_event_type, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity WHERE pid = %s",
                (waiter,),
            ).one()
            if last.wait_event_type == "Lock" and blocker in last.blockers:
                return
            sleep(0.01)
    pytest.fail(f"Backend {waiter} never waited for blocker {blocker}: {last!r}")


def test_disable_first_forces_wait_then_committed_refusal(admission_engine: Engine) -> None:
    authority, context, run = _admit(admission_engine)
    attempted = Event()
    backend: list[int] = []

    def probe(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _execution_context: Any, _many: bool) -> None:
        if statement.startswith("SELECT") and "FROM identities" in statement and "FOR UPDATE" in statement:
            backend.append(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            attempted.set()

    event.listen(admission_engine, "before_cursor_execute", probe)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            with admission_engine.begin() as disabling:
                blocker = disabling.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                disabling.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
                future = workers.submit(_issue, authority, context, run.id)
                assert attempted.wait(10), "permit never attempted its identity row lock"
                _assert_lock_wait(admission_engine, backend[0], blocker)
                assert not future.done()
            refused = future.result(timeout=10)
    finally:
        event.remove(admission_engine, "before_cursor_execute", probe)
    assert refused.state is StartPermitState.REFUSED
    assert refused.admission_decision is not None
    assert refused.admission_decision.refusal_reason is AdmissionRefusalReason.IDENTITY_DISABLED
    # A fresh transaction sees a durable refusal and pending cleanup, not a rollback.
    with admission_engine.connect() as conn:
        stored = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == str(run.id))).one()
        execution = conn.execute(select(runs_table).where(runs_table.c.id == str(run.id))).one()
    assert stored.start_state == "refused"
    assert stored.admission_decision_hash == refused.admission_decision.canonical_hash
    assert execution.status == "failed"
    assert execution.saga_state == "admission_refusal_pending"


def test_permit_first_excludes_disable_then_recovery_preserves_allowance(admission_engine: Engine) -> None:
    authority, context, run = _admit(admission_engine)
    decided = Event()
    release = Event()
    disable_attempted = Event()
    holder_pid: list[int] = []
    writer_pid: list[int] = []

    def capture_holder(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _execution_context: Any, _many: bool) -> None:
        if statement.startswith("SELECT") and "FROM identities" in statement and "FOR UPDATE" in statement:
            holder_pid.append(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())

    def park_after_decision(tx: SessionOperationMutationTransaction) -> RunStartPermitRecord:
        permit = tx.runs.issue_start_permit(run_id=run.id, policy=_POLICY)
        assert permit.state is StartPermitState.START_PERMITTED
        decided.set()
        assert release.wait(20), "permit transaction was never released"
        return permit

    def disable() -> None:
        with admission_engine.begin() as conn:
            writer_pid.append(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            disable_attempted.set()
            conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))

    event.listen(admission_engine, "before_cursor_execute", capture_holder)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(authority.mutate, context, park_after_decision)
            try:
                assert decided.wait(10), "permit never reached the transaction barrier"
                second = workers.submit(disable)
                assert disable_attempted.wait(10)
                _assert_lock_wait(admission_engine, writer_pid[0], holder_pid[0])
                assert not second.done()
            finally:
                release.set()
            permitted = first.result(timeout=10)
            second.result(timeout=10)
    finally:
        event.remove(admission_engine, "before_cursor_execute", capture_holder)
    refused = _issue(PostgresSessionOperationRepository(admission_engine), context, run.id)
    assert refused.state is StartPermitState.START_PERMITTED
    assert refused.permit_id == permitted.permit_id
    assert refused.permit_epoch == permitted.permit_epoch
    assert refused.subject_hash == permitted.subject_hash
    assert refused.admission_decision == permitted.admission_decision
    assert refused.execution_refusal is not None
    assert refused.execution_refusal.refusal_reason is AdmissionRefusalReason.IDENTITY_DISABLED
    with admission_engine.connect() as conn:
        execution = conn.execute(select(runs_table).where(runs_table.c.id == str(run.id))).one()
    assert execution.status == "failed"
    assert execution.saga_state == "admission_refusal_pending"


def test_disabled_identity_retains_existing_permit_for_cancellation_cleanup(admission_engine: Engine) -> None:
    authority, context, run = _admit(admission_engine)
    permitted = _issue(authority, context, run.id)
    with admission_engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    cancelled = RepositoryRunCancellationAuthority(admission_engine).request(
        run.id,
        session_id=run.session_id,
        user_id="alice",
        auth_provider_type="local",
    )
    assert cancelled.cancel_requested_at is not None
    cleanup = authority.mutate(context, lambda tx: tx.runs.observe_start_permit_for_cleanup(run_id=run.id))
    assert cleanup == permitted
    assert cleanup.execution_refusal is None
