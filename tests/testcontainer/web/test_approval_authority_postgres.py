"""PostgreSQL contention proofs for role-based approval decisions."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.approval_authority import (
    ApprovalAlreadyDecided,
    ApprovalBinding,
    ApprovalTransactionAuthority,
    ApproverRoleRequired,
    RepositoryApprovalAuthority,
)
from elspeth.web.coordination.quota_authority import TokenUsageEntry, record_token_usage_on_connection
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    approval_decisions_table,
    approvals_table,
    composition_states_table,
    identities_table,
    identity_roles_table,
    sessions_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer

BINDING = ApprovalBinding("config", "canonical", "manifest", "catalog", "generation", "policy")


@pytest.fixture
def approval_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"approval_{uuid4().hex}"
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


def _seed(engine: Engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id in ("bob", "carol"):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"role-{identity_id}",
                    identity_id=identity_id,
                    role="approver",
                    granted_by_identity_id="alice",
                    granted_at=now,
                )
            )
        conn.execute(
            insert(sessions_table).values(
                id="session-1",
                user_id="alice",
                title="approval",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id="state-1",
                session_id="session-1",
                version=1,
                is_valid=True,
                provenance="session_seed",
                created_at=now,
            )
        )


def _open_request(engine: Engine) -> str:
    record = ApprovalTransactionAuthority(engine).run(
        "session-1",
        lambda token: RepositoryApprovalAuthority.request(
            token,
            session_id="session-1",
            state_id="state-1",
            binding=BINDING,
            requested_by="alice",
            approver="bob",
            note=None,
            record=lambda _row: None,
        ),
    )
    return record.approval_id


def test_two_eligible_approvers_race_to_decide_one_open_request(approval_engine: Engine) -> None:
    _seed(approval_engine)
    approval_id = _open_request(approval_engine)
    gate = Barrier(2)

    def attempt(actor: str) -> str:
        gate.wait(timeout=30)
        try:
            ApprovalTransactionAuthority(approval_engine).run(
                "session-1",
                lambda token: RepositoryApprovalAuthority.decide(
                    token,
                    approval_id=approval_id,
                    decided_by=actor,
                    decision="approved",
                    note=None,
                    record=lambda _row: None,
                ),
            )
        except ApprovalAlreadyDecided:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, ("bob", "carol")))
    assert outcomes == ["lost", "won"]
    with approval_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(approval_decisions_table)).scalar_one() == 1
        assert (
            conn.execute(select(approvals_table.c.decision).where(approvals_table.c.approval_id == approval_id)).scalar_one() == "approved"
        )


def test_approval_request_waits_for_session_without_blocking_token_settlement(approval_engine: Engine) -> None:
    _seed(approval_engine)
    waiting_on_holder = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
    )
    now = datetime.now(UTC)
    entry = TokenUsageEntry(
        model="test-model",
        prompt_tokens=10,
        completion_tokens=5,
        cached_prompt_tokens=0,
        reasoning_tokens=0,
        recorded_at=now,
        call_id="approval-concurrency-call",
    )
    with ThreadPoolExecutor(max_workers=1) as pool, approval_engine.connect() as holder:
        with holder.begin():
            holder.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
            holder.execute(select(sessions_table.c.id).where(sessions_table.c.id == "session-1").with_for_update()).one()
            blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            pending = pool.submit(_open_request, approval_engine)
            # PostgreSQL's wait graph is the barrier: the request must have
            # reached its session lock before settlement asks for the owner FK.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert not pending.done(), "approval completed while its session was exclusively locked"
                with approval_engine.connect() as observer:
                    if observer.execute(waiting_on_holder, {"blocker": blocker}).scalar_one() >= 1:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("approval never waited on its session")

            recorded = record_token_usage_on_connection(
                holder,
                session_id="session-1",
                source="composer",
                run_id=None,
                entries=(entry,),
                recorded_at=now,
            )
            assert len(recorded) == 1
            assert not pending.done()
        approval_id = pending.result(timeout=30)

    with approval_engine.connect() as conn:
        approval = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == approval_id)).one()
        usage = conn.execute(select(token_usage_ledger_table)).one()
    assert approval.session_id == "session-1"
    assert approval.decision is None
    assert usage.identity_id == "alice"
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5


def test_role_expiring_during_identity_lock_wait_refuses_decision(approval_engine: Engine) -> None:
    _seed(approval_engine)
    approval_id = _open_request(approval_engine)

    def attempt() -> str:
        try:
            ApprovalTransactionAuthority(approval_engine).run(
                "session-1",
                lambda token: RepositoryApprovalAuthority.decide(
                    token,
                    approval_id=approval_id,
                    decided_by="carol",
                    decision="approved",
                    note=None,
                    record=lambda _row: None,
                ),
            )
        except ApproverRoleRequired:
            return "refused"
        return "approved"

    probe = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
    )
    with ThreadPoolExecutor(max_workers=1) as pool, approval_engine.connect() as holder:
        with holder.begin():
            holder.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == "carol").with_for_update()).one()
            blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            pending = pool.submit(attempt)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert not pending.done(), "decision finished before participant lock released"
                with approval_engine.connect() as observer:
                    if observer.execute(probe, {"blocker": blocker}).scalar_one() >= 1:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("decision never waited on participant identity")
            holder.execute(
                update(identity_roles_table).where(identity_roles_table.c.role_id == "role-carol").values(expires_at=func.clock_timestamp())
            )
        assert pending.result(timeout=30) == "refused"
    with approval_engine.connect() as conn:
        assert conn.execute(select(approval_decisions_table)).all() == []
        assert conn.execute(select(approvals_table.c.decision).where(approvals_table.c.approval_id == approval_id)).scalar_one() is None
