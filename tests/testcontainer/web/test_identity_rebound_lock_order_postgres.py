"""PostgreSQL proof that R3's rebound disable takes its locks in a safe order.

R3 disables an identity whose verified email no longer matches
``subject_email_at_first_seen``.  When that identity holds deployment admin
the disable can lower R5's count, and ``_ADMIN_HOLDER_ROWS_FOR_UPDATE``
requires that class of mutation to take the admin POPULATION before its own
target row -- two mutations that disagree about the order deadlock on each
other's target.  ``_ensure_identity_once`` already holds the target row by the
time it can discover the target is an admin, so attempt 1 raises
``_AdminLockRequired`` and ROLLS BACK rather than reaching for the population
out of order; attempt 2 re-runs with the population taken first.

**SQLite cannot test any of that, which is why this file exists.**
``create_session_engine`` rebinds ``engine.begin()`` to ``BEGIN IMMEDIATE``
and the SQLite dialect DROPS ``FOR UPDATE`` entirely -- the same two facts
``test_identity_last_admin_race_postgres`` documents.  So under the unit suite
every ``SELECT ... FOR UPDATE`` in this path is a plain SELECT, one writer
runs at a time, and a lock-ordering defect is not merely undetected but
unexpressible.  A green ``pytest tests/`` says nothing here.

What is deliberately NOT tested: a forced ABBA deadlock.  With the retry in
place attempt 1 never holds the target while reaching for the population, so
the interleaving that deadlocks is unreachable by construction -- a test that
tried to force it would be asserting on timing it cannot control, and would
report flake rather than fact.  The properties below are the ones that are
both decidable and load-bearing: the retry's second attempt really does
execute its population lock against a server that honours it, a rebound login
and a concurrent admin disable both commit without either deadlocking, and
R5's carve-out survives two replicas racing the same last admin.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.identity_authority import (
    IdentityAdminActor,
    RepositoryIdentityAuthority,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


def _noop(*_args: Any) -> None:
    return None


def _claims(subject: str, *, email: str, provider: Any = "vanguard") -> IdentityClaims:
    """An IdP login.  R3 excludes ``local``, whose subject IS the username."""
    return IdentityClaims(
        provider=provider,
        subject=subject,
        username=subject,
        display_name=subject,
        email=email,
        organisation_id=None,
    )


def _actor(identity_id: str) -> IdentityAdminActor:
    return IdentityAdminActor(identity_id=identity_id, on_behalf_of=None, console_request_id=None)


def _login(authority: RepositoryIdentityAuthority, claims: IdentityClaims, *, record_rebound: Any = _noop) -> Any:
    # ``identity_dormancy_days`` is passed wide enough that R9 cannot fire on
    # these fixtures: this file pins R3's lock order, and a dormancy re-pend
    # sharing the same retry protocol would change which refusal a red here
    # is reporting.
    return authority.ensure_identity(
        claims=claims,
        activate=False,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        identity_dormancy_days=36_500,
        record_admission=_noop,
        record_rebound=record_rebound,
        record_dormant=_noop,
    )


def _row(engine: Engine, identity_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(identities_table).where(identities_table.c.identity_id == identity_id)).one()


def _seed_service_admin(engine: Engine, granted_by: str, authority: RepositoryIdentityAuthority) -> str:
    """The actor R5 does not count, so only R5 itself can refuse a disable."""
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


def _identity_id(authority: RepositoryIdentityAuthority, subject: str) -> str:
    record = authority.read_identity_by_natural_key(provider="vanguard", subject=subject)
    assert record is not None, f"no identity for subject {subject!r}"
    return record.identity_id


def _one_concurrent_round(
    *,
    setup_engine: Engine,
    setup: RepositoryIdentityAuthority,
    rebounding: RepositoryIdentityAuthority,
    disabling: RepositoryIdentityAuthority,
    service: str,
    rebounder: str,
    target_id: str,
) -> None:
    """One rebound login against one admin disable, started together.

    Taken as a function rather than a loop body so each thread closes over
    THIS round's values: a closure over the loop variable would let a slow
    thread act on the next round's identity and turn a lock-order proof into
    a test of its own scheduling.
    """
    barrier = Barrier(2)

    def rebound_login() -> None:
        barrier.wait(timeout=30)
        _login(rebounding, _claims(rebounder, email=f"{rebounder}@new.example"))

    def admin_disable() -> None:
        barrier.wait(timeout=30)
        disabling.disable_identity(actor=_actor(service), identity_id=target_id, reason="concurrent", record=_noop)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(rebound_login), pool.submit(admin_disable))
        for future in futures:
            try:
                future.result(timeout=60)
            except DBAPIError as exc:  # pragma: no cover - this IS the defect's signature
                pytest.fail(f"lock-order defect on round {rebounder}: {type(exc).__name__}: {exc}")

    assert _row(setup_engine, _identity_id(setup, rebounder)).disable_reason == "rebound"
    assert _row(setup_engine, target_id).disable_reason == "concurrent"


def _fresh_database(url: str, prefix: str) -> str:
    """R5's count is deployment-wide and the container is shared across files."""
    admin_url = make_url(url)
    database = f"{prefix}_{uuid.uuid4().hex}"
    control = create_session_engine(url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    return admin_url.set(database=database).render_as_string(hide_password=False)


def _admin(authority: RepositoryIdentityAuthority, actor_id: str, subject: str, email: str) -> str:
    """An IdP identity holding deployment admin.  ``admin`` is not an activation role (D14)."""
    record = _login(authority, _claims(subject, email=email)).record
    authority.activate_identity(
        actor=_actor(actor_id),
        identity_id=record.identity_id,
        role="none",
        note="admitted",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=_noop,
    )
    authority.grant_role(
        actor=_actor(actor_id), identity_id=record.identity_id, role="admin", scope=None, expires_at=None, note=None, record=_noop
    )
    return record.identity_id


def test_an_admin_rebound_commits_through_the_retry_against_a_server_that_honours_for_update(
    external_deployment_postgres_url: str,
) -> None:
    """Attempt 2's population lock executes for the first time here.

    Under SQLite the same code path runs with ``FOR UPDATE`` stripped, so this
    is the first execution in which the statement means anything.  A rebound
    on a non-last admin must disable it and leave R5's population intact.
    """
    url = _fresh_database(external_deployment_postgres_url, "rebound_retry")
    engine = create_session_engine(url)
    initialize_session_schema(engine)
    authority = RepositoryIdentityAuthority(engine)

    root = authority.bootstrap_admin(
        claims=_claims("root", email="root@example.com"),
        note="first admin",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=_noop,
    )
    root_id = root.record.identity_id
    bob_id = _admin(authority, root_id, "bob", "bob@old.example")
    assert authority.count_active_human_admins() == 2

    outcome = _login(authority, _claims("bob", email="bob@new.example"))

    assert outcome.rebound_refused is True
    row = _row(engine, bob_id)
    assert row.access_state == "disabled"
    assert row.disable_reason == "rebound"
    assert row.rebound_at is not None
    assert row.disabled_by_identity_id is None
    # R5 is unharmed and the container still has an administrator.
    assert authority.count_active_human_admins() == 1
    assert _row(engine, root_id).access_state == "active"


def test_a_rebound_login_and_a_concurrent_admin_disable_both_commit_without_deadlocking(
    external_deployment_postgres_url: str,
) -> None:
    """Both mutations can lower R5's count, so both must take the population first.

    ``disable_identity`` takes it before its target by construction.  The
    rebound path cannot -- it holds the target before it can know the target
    is an admin -- so it rolls back and retries in the right order instead.
    If it reached for the population while holding its target, these two would
    hold each other's rows and PostgreSQL would raise a deadlock on one of
    them.  Repeated, because a lock-order defect is a race and one clean pass
    would not distinguish it from luck.
    """
    url = _fresh_database(external_deployment_postgres_url, "rebound_deadlock")
    setup_engine = create_session_engine(url)
    initialize_session_schema(setup_engine)
    setup = RepositoryIdentityAuthority(setup_engine)

    root = setup.bootstrap_admin(
        claims=_claims("root", email="root@example.com"),
        note="first admin",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=_noop,
    )
    root_id = root.record.identity_id
    service = _seed_service_admin(setup_engine, root_id, setup)

    rounds = 8
    subjects = [(f"rebounder{n}", f"target{n}") for n in range(rounds)]
    for rebounder, target in subjects:
        _admin(setup, root_id, rebounder, f"{rebounder}@old.example")
        _admin(setup, root_id, target, f"{target}@old.example")

    first_engine = create_session_engine(url)
    second_engine = create_session_engine(url)
    first = RepositoryIdentityAuthority(first_engine)
    second = RepositoryIdentityAuthority(second_engine)

    for rebounder, target in subjects:
        _one_concurrent_round(
            setup_engine=setup_engine,
            setup=setup,
            rebounding=first,
            disabling=second,
            service=service,
            rebounder=rebounder,
            target_id=_identity_id(setup, target),
        )


def test_two_replicas_rebounding_the_last_admin_leave_it_active(external_deployment_postgres_url: str) -> None:
    """R5's carve-out under a real population lock, not SQLite's single writer.

    Both replicas see the same rebound on the LAST active human admin at the
    same moment.  Each takes the population lock on its retry, so each reads
    the count as the other committed it; neither may disable, because
    disabling would brick the container into C2's lockout.  Both refuse the
    login -- that part is not a race -- and the identity stays ``active``.
    """
    url = _fresh_database(external_deployment_postgres_url, "rebound_last_admin")
    setup_engine = create_session_engine(url)
    initialize_session_schema(setup_engine)
    setup = RepositoryIdentityAuthority(setup_engine)

    root = setup.bootstrap_admin(
        claims=_claims("root", email="root@old.example"),
        note="only admin",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=_noop,
    )
    root_id = root.record.identity_id
    assert setup.count_active_human_admins() == 1

    first = RepositoryIdentityAuthority(create_session_engine(url))
    second = RepositoryIdentityAuthority(create_session_engine(url))
    barrier = Barrier(2)

    def attempt(authority: RepositoryIdentityAuthority) -> bool:
        barrier.wait(timeout=30)
        return bool(_login(authority, _claims("root", email="root@new.example")).rebound_refused)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(attempt, first), pool.submit(attempt, second))
        refusals = [future.result(timeout=60) for future in futures]

    # Both refuse, whichever order they land in: the carve-out does NOT
    # rebase the baseline, so the row the second replica reads still says
    # root@old.example and is still a rebound.
    assert refusals == [True, True]
    row = _row(setup_engine, root_id)
    assert row.access_state == "active"
    assert row.disable_reason is None
    assert row.rebound_at is not None
    assert setup.count_active_human_admins() == 1
