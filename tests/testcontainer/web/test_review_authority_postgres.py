"""PostgreSQL contention proof for the review authority's lock-then-clock order."""

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

from elspeth.web.coordination.review_authority import (
    OpenReviewRequestExists,
    RepositoryReviewAuthority,
    ReviewerRoleRequired,
    StateNotInSession,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def review_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"review_authority_{uuid4().hex}"
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
                    role="reviewer",
                    granted_at=now,
                    granted_by_identity_id="alice",
                )
            )
        conn.execute(
            insert(sessions_table).values(
                id="session-1", user_id="alice", auth_provider_type="local", title="review", created_at=now, updated_at=now
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id="state-1", session_id="session-1", version=1, provenance="session_seed", created_at=now
            )
        )


def _noop(_value: object) -> None:
    pass


def _request(authority: RepositoryReviewAuthority, reviewer: str) -> str:
    try:
        authority.request(session_id="session-1", state_id="state-1", requested_by="alice", reviewer=reviewer, note=None, record=_noop)
    except ReviewerRoleRequired:
        return "refused"
    return "requested"


def test_concurrent_requests_for_one_state_leave_one_open_row(review_engine: Engine) -> None:
    _seed(review_engine)
    authority = RepositoryReviewAuthority(review_engine)

    def attempt(reviewer: str) -> str:
        try:
            authority.request(session_id="session-1", state_id="state-1", requested_by="alice", reviewer=reviewer, note=None, record=_noop)
        except OpenReviewRequestExists:
            return "already_open"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, ("bob", "carol")))
    assert outcomes == ["already_open", "created"]
    with review_engine.connect() as conn:
        assert len(conn.execute(select(review_requests_table)).all()) == 1


def test_reviewer_grant_expiring_during_identity_lock_wait_is_refused(review_engine: Engine) -> None:
    _seed(review_engine)
    authority = RepositoryReviewAuthority(review_engine)
    probe = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
    )
    with ThreadPoolExecutor(max_workers=1) as pool, review_engine.connect() as holder:
        with holder.begin():
            holder.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == "bob").with_for_update()).one()
            blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            attempt = pool.submit(_request, authority, "bob")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert not attempt.done(), "the request finished before it acquired the identity lock"
                with review_engine.connect() as observer:
                    if observer.execute(probe, {"blocker": blocker}).scalar_one() >= 1:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("request never waited on the reviewer identity row")
            holder.execute(
                update(identity_roles_table).where(identity_roles_table.c.role_id == "role-bob").values(expires_at=func.clock_timestamp())
            )
        assert attempt.result(timeout=30) == "refused"
    with review_engine.connect() as conn:
        assert conn.execute(select(review_requests_table)).all() == []


def test_reviewer_grant_expiring_during_attestation_wait_is_refused(review_engine: Engine) -> None:
    _seed(review_engine)
    authority = RepositoryReviewAuthority(review_engine)
    authority.request(session_id="session-1", state_id="state-1", requested_by="alice", reviewer="bob", note=None, record=_noop)

    def attempt_attest() -> str:
        try:
            authority.attest(
                session_id="session-1",
                state_id="state-1",
                payload_digest="sha256:" + "ab" * 32,
                reviewer="bob",
                verdict="signed_off",
                note=None,
                record=_noop,
            )
        except ReviewerRoleRequired:
            return "refused"
        return "attested"

    probe = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
    )
    with ThreadPoolExecutor(max_workers=1) as pool, review_engine.connect() as holder:
        with holder.begin():
            holder.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == "bob").with_for_update()).one()
            blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            attempt = pool.submit(attempt_attest)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert not attempt.done(), "attestation completed while the reviewer identity was locked"
                with review_engine.connect() as observer:
                    if observer.execute(probe, {"blocker": blocker}).scalar_one() >= 1:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("attestation never waited on the reviewer identity row")
            holder.execute(
                update(identity_roles_table).where(identity_roles_table.c.role_id == "role-bob").values(expires_at=func.clock_timestamp())
            )
        assert attempt.result(timeout=30) == "refused"
    with review_engine.connect() as conn:
        assert conn.execute(select(review_attestations_table)).all() == []


def test_session_archive_during_attestation_lock_wait_refuses_the_attestation(review_engine: Engine) -> None:
    _seed(review_engine)
    authority = RepositoryReviewAuthority(review_engine)
    authority.request(session_id="session-1", state_id="state-1", requested_by="alice", reviewer="bob", note=None, record=_noop)

    def attempt_attest() -> str:
        try:
            authority.attest(
                session_id="session-1",
                state_id="state-1",
                payload_digest="sha256:" + "ab" * 32,
                reviewer="bob",
                verdict="signed_off",
                note=None,
                record=_noop,
            )
        except StateNotInSession:
            return "not_reviewable"
        return "attested"

    probe = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))"
    )
    with ThreadPoolExecutor(max_workers=1) as pool, review_engine.connect() as holder:
        with holder.begin():
            holder.execute(select(sessions_table.c.id).where(sessions_table.c.id == "session-1").with_for_update()).one()
            blocker = int(holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            attempt = pool.submit(attempt_attest)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert not attempt.done(), "attestation completed while the session row was locked"
                with review_engine.connect() as observer:
                    if observer.execute(probe, {"blocker": blocker}).scalar_one() >= 1:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("attestation never waited on the session row")
            holder.execute(update(sessions_table).where(sessions_table.c.id == "session-1").values(archived_at=func.clock_timestamp()))
        assert attempt.result(timeout=30) == "not_reviewable"
    with review_engine.connect() as conn:
        assert conn.execute(select(review_attestations_table)).all() == []
