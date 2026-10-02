"""PostgreSQL identity-to-approval transaction ordering proofs.

The approval authoring/decision product is reserved. The competing writer here
is an explicit SQL fixture implementing its documented identity-first contract,
not a claim that the full approval workflow API has shipped.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select, text, update
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import IdentityAdminActor, RepositoryIdentityAuthority
from elspeth.web.coordination.identity_lifecycle import IdentityAuthorityRevoked
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import approvals_table, identities_table, identity_roles_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def engines(external_deployment_postgres_url: str) -> Iterator[tuple[Engine, Engine, Engine]]:
    database = f"approval_lifecycle_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False)
    disable_engine = create_session_engine(url, connect_args={"application_name": "approval_disable"})
    consumer_engine = create_session_engine(url, connect_args={"application_name": "approval_consumer"})
    observer = create_session_engine(url)
    try:
        initialize_session_schema(disable_engine)
        now = datetime.now(UTC)
        with disable_engine.begin() as conn:
            for identity_id in ("admin", "approver", "author"):
                ensure_test_identity(conn, identity_id=identity_id)
            conn.execute(
                insert(identity_roles_table).values(
                    role_id="admin-role",
                    identity_id="admin",
                    role="admin",
                    granted_at=now,
                    granted_by_identity_id="admin",
                )
            )
            conn.execute(
                insert(sessions_table).values(
                    id="session",
                    user_id="author",
                    title="approval",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(approvals_table).values(
                    approval_id="existing",
                    session_id="session",
                    state_id="existing",
                    binding_json={},
                    requested_by_identity_id="author",
                    approver_identity_id="approver",
                    requested_at=now,
                )
            )
        yield disable_engine, consumer_engine, observer
    finally:
        disable_engine.dispose()
        consumer_engine.dispose()
        observer.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _wait_for_identity_lock(observer: Engine, application: str) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        with observer.connect() as conn:
            blocked = conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = :application "
                    "AND wait_event_type = 'Lock' AND cardinality(pg_blocking_pids(pid)) > 0 "
                    "AND query ILIKE '%identities%')"
                ),
                {"application": application},
            ).scalar_one()
        if blocked:
            return
        sleep(0.01)
    raise AssertionError(f"{application} did not block on the identity lock")


def _ignore(_outcome: object) -> None:
    pass


@pytest.mark.parametrize("first", ["disable", "consumer"])
@pytest.mark.parametrize("operation", ["create", "decide"])
@pytest.mark.parametrize("identity_id", ["approver", "author"])
def test_approval_consumer_identity_lock_serializes_with_disable(
    engines: tuple[Engine, Engine, Engine],
    first: str,
    operation: str,
    identity_id: str,
) -> None:
    disable_engine, consumer_engine, observer = engines
    first_ready = Event()
    release_first = Event()

    def effect(token: str, event: IdentityAuthorityRevoked) -> None:
        RepositoryApprovalLifecycleAuthority().apply(token, event)
        if first == "disable":
            first_ready.set()
            if not release_first.wait(15):
                raise AssertionError("disable release timed out")

    authority = RepositoryIdentityAuthority(disable_engine, lifecycle_effect=effect)

    def disable() -> str:
        authority.disable_identity(
            actor=IdentityAdminActor(identity_id="admin", on_behalf_of=None, console_request_id=None),
            identity_id=identity_id,
            reason="withdrawn",
            record=_ignore,
        )
        return "disabled"

    def consumer() -> str:
        with consumer_engine.begin() as conn:
            states = (
                conn.execute(
                    select(identities_table.c.access_state)
                    .where(identities_table.c.identity_id.in_(("approver", "author")))
                    .order_by(identities_table.c.identity_id)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            assert len(states) == 2
            if any(state != "active" for state in states):
                return "refused"
            if operation == "create":
                conn.execute(
                    insert(approvals_table).values(
                        approval_id="new",
                        session_id="session",
                        state_id="new",
                        binding_json={},
                        requested_by_identity_id="author",
                        approver_identity_id="approver",
                        requested_at=datetime.now(UTC),
                    )
                )
            else:
                changed = conn.execute(
                    update(approvals_table)
                    .where(
                        approvals_table.c.approval_id == "existing",
                        approvals_table.c.decision.is_(None),
                    )
                    .values(decision="approved", decided_at=datetime.now(UTC))
                )
                assert changed.rowcount == 1
            if first == "consumer":
                first_ready.set()
                if not release_first.wait(15):
                    raise AssertionError("consumer release timed out")
        return "written"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(disable if first == "disable" else consumer)
        try:
            assert first_ready.wait(10)
            second_future = pool.submit(consumer if first == "disable" else disable)
            _wait_for_identity_lock(observer, "approval_consumer" if first == "disable" else "approval_disable")
        finally:
            release_first.set()
        assert first_future.result(timeout=15) == ("disabled" if first == "disable" else "written")
        assert second_future.result(timeout=15) == ("refused" if first == "disable" else "disabled")

    with observer.connect() as conn:
        rows = {row.approval_id: row for row in conn.execute(select(approvals_table))}
    if first == "consumer" and operation == "decide":
        assert rows["existing"].decision == "approved"
        assert rows["existing"].revocation_event_id is None
    else:
        assert rows["existing"].decision == "revoked"
        assert rows["existing"].revoked_by_identity_id == "admin"
    if first == "consumer" and operation == "create":
        assert rows["new"].decision == "revoked"
        assert rows["new"].revocation_event_id == rows["existing"].revocation_event_id
    else:
        assert "new" not in rows


@pytest.mark.parametrize("failure", ["audit", "effect"])
@pytest.mark.parametrize("identity_id", ["approver", "author"])
def test_failed_withdrawal_rolls_back_both_authorities_in_postgres(
    engines: tuple[Engine, Engine, Engine],
    failure: str,
    identity_id: str,
) -> None:
    disable_engine, _, observer = engines

    def effect(token: str, event: IdentityAuthorityRevoked) -> None:
        RepositoryApprovalLifecycleAuthority().apply(token, event)
        if failure == "effect":
            raise RuntimeError("effect unavailable")

    def record(_outcome: object) -> None:
        raise RuntimeError("audit unavailable")

    authority = RepositoryIdentityAuthority(disable_engine, lifecycle_effect=effect)
    with pytest.raises(RuntimeError, match="unavailable"):
        authority.disable_identity(
            actor=IdentityAdminActor(identity_id="admin", on_behalf_of=None, console_request_id=None),
            identity_id=identity_id,
            reason="withdrawn",
            record=record,
        )
    with observer.connect() as conn:
        assert (
            conn.execute(select(identities_table.c.access_state).where(identities_table.c.identity_id == identity_id)).scalar_one()
            == "active"
        )
        approval = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == "existing")).one()
        assert approval.decision is None
        assert approval.revocation_event_id is None
