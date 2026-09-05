"""PostgreSQL proof that R5's last-admin guard holds across replicas.

Two replicas, acting as one service administrator (which R5 does not count),
each disable one of the last two active human administrators at the same
moment.  Under READ COMMITTED with only the TARGET row locked, both
read an admin count of 2 and both commit: zero administrators, the lockout R5
exists to prevent (pre-review SOFT 2 on elspeth-e483fe7f85).  The authority
now locks the admin row set (``SELECT ... FOR UPDATE`` over R5's population)
before anything else in the transaction, so the second replica waits on the
first and re-reads the count as committed.

SQLite cannot show the race in either direction: ``create_session_engine``
rebinds ``engine.begin()`` to ``BEGIN IMMEDIATE``, so the whole
read-count-then-write of one replica runs under the single writer lock and
the other cannot begin reading until it commits.  Only PostgreSQL can be
wrong here, which is why this proof is a testcontainer test.

Determinism: the ``record`` callback runs inside each replica's transaction
after its UPDATE and before its COMMIT, so it is the rendezvous.  The first
replica to reach it does not return until EITHER the other replica has also
reached its callback (only possible when the count was read without the
lock: the defect, and both then commit to zero) OR ``pg_stat_activity``
shows the other replica blocked on a lock (the fix: it is waiting on the
admin row set).  No sleeps decide the outcome; the poll interval only paces
the observation.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.identity_authority import (
    AdminAlreadyBootstrapped,
    IdentityActivated,
    IdentityAdminActor,
    IdentityDisabled,
    LastActiveAdminProtected,
    RepositoryIdentityAuthority,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer

_RENDEZVOUS_DEADLINE_SECONDS = 30.0
_POLL_SECONDS = 0.01


def _noop(*_args: Any) -> None:
    return None


def _claims(subject: str) -> IdentityClaims:
    return IdentityClaims(
        provider="local",
        subject=subject,
        username=subject,
        display_name=subject,
        email=f"{subject}@example.com",
        organisation_id=None,
    )


def _actor(identity_id: str) -> IdentityAdminActor:
    return IdentityAdminActor(identity_id=identity_id, on_behalf_of=None, console_request_id=None)


def _seed_service_admin(engine: Engine, granted_by: str, authority: RepositoryIdentityAuthority) -> str:
    """The actor R5 does not count: a service identity holding deployment admin.

    A service identity has no minting path in this delivery, so the row is
    seeded directly (as the unit suite does); its admin grant goes through the
    authority.  Both replicas act AS this identity, so neither disable can be
    refused by the actor fence -- only R5 can refuse the loser.
    """
    identity_id = "console-service"
    with engine.begin() as conn:
        conn.execute(
            identities_table.insert().values(
                identity_id=identity_id,
                provider="service",
                kind="service",
                subject=identity_id,
                username=identity_id,
                first_seen_at=datetime.now(UTC),
                access_state="active",
                activated_at=datetime.now(UTC),
            )
        )
    authority.grant_role(
        actor=_actor(granted_by), identity_id=identity_id, role="admin", scope=None, expires_at=None, note=None, record=_noop
    )
    return identity_id


def _lock_waiters_on_the_admin_population(observer: Engine) -> int:
    """Sessions blocked on a lock while executing the admin-population read."""
    with observer.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND query ILIKE :pattern AND pid <> pg_backend_pid()"
                ),
                {"pattern": "%identity_roles%"},
            ).scalar_one()
        )


def _rendezvous(observer: Engine, arrived: tuple[Event, Event], index: int) -> Callable[[IdentityDisabled], None]:
    mine, other = arrived[index], arrived[1 - index]

    def record(_outcome: IdentityDisabled) -> None:
        mine.set()
        deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
        while not other.is_set():
            if _lock_waiters_on_the_admin_population(observer) >= 1:
                return
            if time.monotonic() > deadline:
                raise AssertionError("the other replica neither reached its audit callback nor blocked on a lock")
            time.sleep(_POLL_SECONDS)

    return record


def test_two_replicas_disabling_the_last_two_admins_leave_exactly_one(external_deployment_postgres_url: str) -> None:
    # A database of this test's own: the container is shared across the
    # acceptance files, and R5's count is deployment-wide.
    admin_url = make_url(external_deployment_postgres_url)
    database = f"last_admin_race_{uuid.uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    race_url = admin_url.set(database=database).render_as_string(hide_password=False)

    first_engine = create_session_engine(race_url)
    second_engine = create_session_engine(race_url)
    observer = create_session_engine(race_url)
    try:
        initialize_session_schema(first_engine)
        first = RepositoryIdentityAuthority(first_engine)
        second = RepositoryIdentityAuthority(second_engine)

        root = first.bootstrap_admin(
            claims=_claims("root"),
            note="first admin",
            quota_tokens_per_day=None,
            quota_storage_bytes=None,
            record=_noop,
        )
        root_id = root.record.identity_id
        other = first.pre_provision_identity(
            actor=_actor(root_id),
            provider="local",
            subject="second",
            username=None,
            organisation_id=None,
            role="none",
            note="second admin",
            quota_tokens_per_day=None,
            quota_storage_bytes=None,
            record=_noop,
        )
        other_id = other.record.identity_id
        first.grant_role(actor=_actor(root_id), identity_id=other_id, role="admin", scope=None, expires_at=None, note=None, record=_noop)
        service = _seed_service_admin(first_engine, root_id, first)
        assert first.count_active_human_admins() == 2

        arrived = (Event(), Event())
        barrier = Barrier(2)

        def disable(authority: RepositoryIdentityAuthority, actor_id: str, target_id: str, index: int) -> str:
            barrier.wait(timeout=10)
            try:
                authority.disable_identity(
                    actor=_actor(actor_id),
                    identity_id=target_id,
                    reason="race",
                    record=_rendezvous(observer, arrived, index),
                )
            except LastActiveAdminProtected:
                return "refused"
            return "disabled"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(disable, first, service, other_id, 0),
                pool.submit(disable, second, service, root_id, 1),
            )
            outcomes = sorted(future.result(timeout=60) for future in futures)

        assert outcomes == ["disabled", "refused"]
        assert first.count_active_human_admins() == 1
        survivors = {
            identity_id
            for identity_id in (root_id, other_id)
            if (record := first.read_identity(identity_id=identity_id)) is not None and record.is_active
        }
        assert len(survivors) == 1
    finally:
        first_engine.dispose()
        second_engine.dispose()
        observer.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _bootstrap_rendezvous(observer: Engine, arrived: tuple[Event, Event], index: int) -> Callable[[IdentityActivated], None]:
    mine, other = arrived[index], arrived[1 - index]

    def record(_outcome: IdentityActivated) -> None:
        mine.set()
        deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
        while not other.is_set():
            if _lock_waiters_on_the_admin_population(observer) >= 1:
                return
            if time.monotonic() > deadline:
                raise AssertionError("the other replica neither reached its audit callback nor blocked on a lock")
            time.sleep(_POLL_SECONDS)

    return record


def test_two_replicas_bootstrapping_at_once_mint_exactly_one_admin(external_deployment_postgres_url: str) -> None:
    """D20 across replicas: the population the inert check counts is EMPTY, so no row lock can serialise it.

    Two replicas bootstrap different subjects at the same moment.  With only
    the natural-key row locked, both count zero active human admins and both
    self-grant.  The authority now takes a PostgreSQL table lock on the admin
    population before the count; the loser waits, counts the winner and is
    refused.  Rendezvous as in the disable race: ``record`` runs inside the
    transaction after the writes and before the commit, and the first arrival
    returns only when the other has also arrived (the defect) or is blocked on
    a lock (the fix).
    """
    admin_url = make_url(external_deployment_postgres_url)
    database = f"bootstrap_race_{uuid.uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    race_url = admin_url.set(database=database).render_as_string(hide_password=False)

    first_engine = create_session_engine(race_url)
    second_engine = create_session_engine(race_url)
    observer = create_session_engine(race_url)
    try:
        initialize_session_schema(first_engine)
        first = RepositoryIdentityAuthority(first_engine)
        second = RepositoryIdentityAuthority(second_engine)
        assert first.count_active_human_admins() == 0

        arrived = (Event(), Event())
        barrier = Barrier(2)

        def bootstrap(authority: RepositoryIdentityAuthority, subject: str, index: int) -> str:
            barrier.wait(timeout=10)
            try:
                authority.bootstrap_admin(
                    claims=_claims(subject),
                    note="first admin",
                    quota_tokens_per_day=None,
                    quota_storage_bytes=None,
                    record=_bootstrap_rendezvous(observer, arrived, index),
                )
            except AdminAlreadyBootstrapped:
                return "refused"
            return "bootstrapped"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(bootstrap, first, "root-one", 0),
                pool.submit(bootstrap, second, "root-two", 1),
            )
            outcomes = sorted(future.result(timeout=60) for future in futures)

        assert outcomes == ["bootstrapped", "refused"]
        assert first.count_active_human_admins() == 1
        # The loser wrote nothing: its subject never became an identity.
        minted = [
            subject
            for subject in ("root-one", "root-two")
            if first.read_identity_by_natural_key(provider="local", subject=subject) is not None
        ]
        assert len(minted) == 1
    finally:
        first_engine.dispose()
        second_engine.dispose()
        observer.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()
