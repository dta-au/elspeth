"""PostgreSQL proofs for request-scoped inspection and its audit row."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.coordination.identity_authority import AdminAuthorityRequired, RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    approvals_table,
    audit_access_log_table,
    composition_states_table,
    identities_table,
    identity_relationships_table,
    identity_roles_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import WorkflowInspectDenied
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def workflow_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"workflow_inspect_{uuid4().hex}"
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
                    granted_at=now,
                    granted_by_identity_id="alice",
                )
            )
        conn.execute(
            insert(sessions_table).values(
                id="session-1",
                user_id="alice",
                auth_provider_type="local",
                title="inspection",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id="state-1",
                session_id="session-1",
                version=1,
                provenance="session_seed",
                created_at=now,
            )
        )
        conn.execute(
            insert(approvals_table).values(
                approval_id="approval-1",
                session_id="session-1",
                state_id="state-1",
                binding_json={},
                requested_by_identity_id="alice",
                approver_identity_id="bob",
                requested_at=now,
            )
        )


def _inspect(engine: Engine) -> str:
    try:
        RepositoryAuditAccessLogAuthority(engine).record_workflow_inspect(
            session_id="session-1",
            state_id="state-1",
            requesting_principal="carol",
            ip_address=None,
        )
    except WorkflowInspectDenied:
        return "denied"
    return "logged"


def test_role_eligible_non_addressee_commits_a_workflow_audit_row(workflow_engine: Engine) -> None:
    _seed(workflow_engine)

    assert _inspect(workflow_engine) == "logged"

    with workflow_engine.connect() as conn:
        rows = conn.execute(select(audit_access_log_table)).all()
    assert len(rows) == 1
    assert rows[0].writer_principal == "workflow_inspect"
    assert rows[0].requesting_principal == "carol"
    assert rows[0].query_args == {}


def test_role_expiring_while_inspection_waits_on_identity_lock_is_denied(workflow_engine: Engine) -> None:
    _seed(workflow_engine)
    probe = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
    )
    with ThreadPoolExecutor(max_workers=1) as pool, workflow_engine.connect() as holder:
        with holder.begin():
            holder.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == "carol").with_for_update()).one()
            blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            pending = pool.submit(_inspect, workflow_engine)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert not pending.done(), "inspection completed before acquiring the caller lock"
                with workflow_engine.connect() as observer:
                    if observer.execute(probe, {"blocker": blocker}).scalar_one() >= 1:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("inspection never waited on the caller identity lock")
            holder.execute(
                update(identity_roles_table).where(identity_roles_table.c.role_id == "role-carol").values(expires_at=func.clock_timestamp())
            )
        assert pending.result(timeout=30) == "denied"
    with workflow_engine.connect() as conn:
        assert conn.execute(select(audit_access_log_table.c.id)).all() == []


def _appoint_curator(engine: Engine) -> str:
    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    try:
        authority.grant_curator_as_approver(
            actor_identity_id="bob", identity_id="carol", expires_at=None, note=None, record=lambda _event: None
        )
    except AdminAuthorityRequired:
        return "denied"
    return "granted"


@pytest.mark.parametrize("change", ["expire_role", "revoke_edge"])
def test_delegated_curator_rechecks_role_and_edge_after_identity_lock_wait(workflow_engine: Engine, change: str) -> None:
    now = datetime.now(UTC)
    with workflow_engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="role-bob", identity_id="bob", role="approver", granted_at=now, granted_by_identity_id="alice"
            )
        )
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id="edge-bob-carol",
                from_identity_id="bob",
                to_identity_id="carol",
                relationship_type="approver",
                asserted_by_identity_id="alice",
                asserted_at=now,
            )
        )

    probe = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
    )
    with ThreadPoolExecutor(max_workers=1) as pool, workflow_engine.connect() as holder:
        with holder.begin():
            holder.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == "carol").with_for_update()).one()
            blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            pending = pool.submit(_appoint_curator, workflow_engine)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert not pending.done(), "appointment completed before acquiring the target lock"
                with workflow_engine.connect() as observer:
                    if observer.execute(probe, {"blocker": blocker}).scalar_one() >= 1:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("appointment never waited on the target identity lock")
            if change == "expire_role":
                holder.execute(
                    update(identity_roles_table)
                    .where(identity_roles_table.c.role_id == "role-bob")
                    .values(expires_at=func.clock_timestamp())
                )
            else:
                holder.execute(
                    update(identity_relationships_table)
                    .where(identity_relationships_table.c.relationship_id == "edge-bob-carol")
                    .values(revoked_at=func.clock_timestamp(), revoked_by_identity_id="alice")
                )
        assert pending.result(timeout=30) == "denied"
    with workflow_engine.connect() as conn:
        assert conn.execute(select(identity_roles_table.c.role_id).where(identity_roles_table.c.role == "curator")).all() == []
