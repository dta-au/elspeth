"""PostgreSQL contention proofs for pending-identity purge linearization."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, text, update
from sqlalchemy.engine import make_url

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import (
    AdminAuthorityRequired,
    IdentityAdminActor,
    IdentityNotFound,
    PendingIdentitiesPurged,
    RepositoryIdentityAuthority,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, identity_roles_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def purge_postgres(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"identity_purge_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with control.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
        engine = create_session_engine(
            make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False)
        )
        try:
            initialize_session_schema(engine)
            yield engine
        finally:
            engine.dispose()
            with control.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    finally:
        control.dispose()


def _authority(engine: Engine) -> RepositoryIdentityAuthority:
    return RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)


def _named_engine(engine: Engine, name: str) -> Engine:
    url = make_url(engine.url).update_query_dict({"application_name": name}).render_as_string(hide_password=False)
    return create_session_engine(url)


def _wait_for_lock(engine: Engine, application_name: str) -> None:
    deadline = time.monotonic() + 10
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :name AND wait_event_type = 'Lock'"),
                {"name": application_name},
            ).scalar_one()
            if waiting:
                return
            time.sleep(0.01)
    pytest.fail(f"{application_name} did not block on the identity row")


def _pending(authority: RepositoryIdentityAuthority, subject: str) -> str:
    outcome = authority.ensure_identity(
        claims=IdentityClaims(provider="local", subject=subject, username=subject),
        activate=False,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        identity_dormancy_days=90,
        record_admission=lambda *_args: None,
        record_rebound=lambda *_args: None,
        record_dormant=lambda *_args: None,
    )
    return outcome.record.identity_id


def _age(engine: Engine, identity_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == identity_id)
            .values(first_seen_at=datetime.now(UTC) - timedelta(days=120))
        )


def test_admin_expiring_while_purge_waits_is_refused_without_delete_or_audit(purge_postgres: Engine) -> None:
    setup = _authority(purge_postgres)
    admin = setup.bootstrap_admin(
        claims=IdentityClaims(provider="local", subject="root", username="root"),
        note="bootstrap",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    actor = IdentityAdminActor(identity_id=admin.record.identity_id, on_behalf_of=None, console_request_id=None)
    pending = _pending(setup, "expired-while-waiting")
    _age(purge_postgres, pending)
    with purge_postgres.begin() as conn:
        expires_at = database_now(conn) + timedelta(seconds=3)
        conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.identity_id == actor.identity_id, identity_roles_table.c.role == "admin")
            .values(expires_at=expires_at)
        )

    waiting_engine = _named_engine(purge_postgres, "expiring-purger")
    purger = _authority(waiting_engine)
    events: list[PendingIdentitiesPurged] = []
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with purge_postgres.begin() as holder:
                holder.execute(select(identities_table).where(identities_table.c.identity_id == actor.identity_id).with_for_update()).one()
                future = pool.submit(
                    purger.purge_stale_pending_identities,
                    actor=actor,
                    retention_days=90,
                    record=events.append,
                )
                _wait_for_lock(purge_postgres, "expiring-purger")
                assert database_now(holder) < expires_at
                holder.exec_driver_sql("SELECT pg_sleep(4)")
                assert database_now(holder) > expires_at
            with pytest.raises(AdminAuthorityRequired):
                future.result(timeout=10)
    finally:
        waiting_engine.dispose()

    assert events == []
    assert setup.read_identity(identity_id=pending) is not None


def test_unexpired_admin_waiting_on_the_same_lock_still_purges_and_audits(purge_postgres: Engine) -> None:
    setup = _authority(purge_postgres)
    admin = setup.bootstrap_admin(
        claims=IdentityClaims(provider="local", subject="root", username="root"),
        note="bootstrap",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    actor = IdentityAdminActor(identity_id=admin.record.identity_id, on_behalf_of=None, console_request_id=None)
    pending = _pending(setup, "unexpired-after-wait")
    _age(purge_postgres, pending)
    with purge_postgres.begin() as conn:
        expires_at = database_now(conn) + timedelta(seconds=30)
        conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.identity_id == actor.identity_id, identity_roles_table.c.role == "admin")
            .values(expires_at=expires_at)
        )

    waiting_engine = _named_engine(purge_postgres, "unexpired-purger")
    purger = _authority(waiting_engine)
    events: list[PendingIdentitiesPurged] = []
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with purge_postgres.begin() as holder:
                holder.execute(select(identities_table).where(identities_table.c.identity_id == actor.identity_id).with_for_update()).one()
                future = pool.submit(
                    purger.purge_stale_pending_identities,
                    actor=actor,
                    retention_days=90,
                    record=events.append,
                )
                _wait_for_lock(purge_postgres, "unexpired-purger")
                assert not future.done()
            outcome = future.result(timeout=10)
    finally:
        waiting_engine.dispose()

    assert outcome.at < expires_at
    assert outcome.identity_ids == (pending,)
    assert events == [outcome]
    assert setup.read_identity(identity_id=pending) is None


def test_activation_and_parallel_purges_linearize_on_actual_deleted_rows(purge_postgres: Engine) -> None:
    setup = _authority(purge_postgres)
    admin = setup.bootstrap_admin(
        claims=IdentityClaims(provider="local", subject="root", username="root"),
        note="bootstrap",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    actor = IdentityAdminActor(identity_id=admin.record.identity_id, on_behalf_of=None, console_request_id=None)

    purge_engine = _named_engine(purge_postgres, "pending-purger")
    activation_engine = _named_engine(purge_postgres, "pending-activator")
    purger = _authority(purge_engine)
    activator = _authority(activation_engine)
    try:
        # Activation wins: purge waits on the pending row, then its predicate
        # re-evaluates after activation commits and reports no deletion.
        activation_wins = _pending(setup, "activation-wins")
        _age(purge_postgres, activation_wins)
        activation_recording = Event()
        release_activation = Event()

        def hold_activation(_event: object) -> None:
            activation_recording.set()
            assert release_activation.wait(10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            activation_future = pool.submit(
                activator.activate_identity,
                actor=actor,
                identity_id=activation_wins,
                role="user",
                note="admit",
                quota_tokens_per_day=None,
                quota_storage_bytes=None,
                record=hold_activation,
            )
            assert activation_recording.wait(10)
            purge_future = pool.submit(
                purger.purge_stale_pending_identities,
                actor=actor,
                retention_days=90,
                record=lambda _event: None,
            )
            _wait_for_lock(purge_postgres, "pending-purger")
            release_activation.set()
            activation_future.result(timeout=10)
            assert purge_future.result(timeout=10).identity_ids == ()
        assert setup.read_identity(identity_id=activation_wins).access_state == "active"

        # Purge wins: activation waits on the candidate, then sees that the
        # row no longer exists. The purge reports exactly the committed row.
        purge_wins = _pending(setup, "purge-wins")
        _age(purge_postgres, purge_wins)
        purge_recording = Event()
        release_purge = Event()

        def hold_purge(_event: PendingIdentitiesPurged) -> None:
            purge_recording.set()
            assert release_purge.wait(10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            purge_future = pool.submit(
                purger.purge_stale_pending_identities,
                actor=actor,
                retention_days=90,
                record=hold_purge,
            )
            assert purge_recording.wait(10)
            activation_future = pool.submit(
                activator.activate_identity,
                actor=actor,
                identity_id=purge_wins,
                role="user",
                note="late admit",
                quota_tokens_per_day=None,
                quota_storage_bytes=None,
                record=lambda _event: None,
            )
            _wait_for_lock(purge_postgres, "pending-activator")
            release_purge.set()
            assert purge_future.result(timeout=10).identity_ids == (purge_wins,)
            with pytest.raises(IdentityNotFound):
                activation_future.result(timeout=10)

        # Parallel purges cannot both claim the same delete.
        one_delete = _pending(setup, "one-delete")
        _age(purge_postgres, one_delete)
        first_recording = Event()
        release_first = Event()

        def hold_first(_event: PendingIdentitiesPurged) -> None:
            first_recording.set()
            assert release_first.wait(10)

        second_engine = _named_engine(purge_postgres, "second-purger")
        try:
            second = _authority(second_engine)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(
                    purger.purge_stale_pending_identities,
                    actor=actor,
                    retention_days=90,
                    record=hold_first,
                )
                assert first_recording.wait(10)
                second_future = pool.submit(
                    second.purge_stale_pending_identities,
                    actor=actor,
                    retention_days=90,
                    record=lambda _event: None,
                )
                _wait_for_lock(purge_postgres, "second-purger")
                release_first.set()
                outcomes = (first_future.result(timeout=10), second_future.result(timeout=10))
            assert sorted(len(outcome.identity_ids) for outcome in outcomes) == [0, 1]
            assert {identity_id for outcome in outcomes for identity_id in outcome.identity_ids} == {one_delete}
        finally:
            second_engine.dispose()
    finally:
        purge_engine.dispose()
        activation_engine.dispose()


def test_admin_role_revocation_and_purge_share_one_linearization(purge_postgres: Engine) -> None:
    setup = _authority(purge_postgres)
    root = setup.bootstrap_admin(
        claims=IdentityClaims(provider="local", subject="root", username="root"),
        note="bootstrap",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    root_actor = IdentityAdminActor(identity_id=root.record.identity_id, on_behalf_of=None, console_request_id=None)
    target = setup.pre_provision_identity(
        actor=root_actor,
        provider="local",
        subject="purge-admin",
        username=None,
        organisation_id=None,
        role="none",
        note="purge operator",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    target_actor = IdentityAdminActor(identity_id=target.record.identity_id, on_behalf_of=None, console_request_id=None)
    target_grant = setup.grant_role(
        actor=root_actor,
        identity_id=target.record.identity_id,
        role="admin",
        scope=None,
        expires_at=None,
        note="purge authority",
        record=lambda _event: None,
    )

    purge_engine = _named_engine(purge_postgres, "role-race-purger")
    revoke_engine = _named_engine(purge_postgres, "role-race-revoker")
    purger = _authority(purge_engine)
    revoker = _authority(revoke_engine)
    try:
        purge_wins = _pending(setup, "purge-authorized-first")
        _age(purge_postgres, purge_wins)
        purge_recording = Event()
        release_purge = Event()

        def hold_purge(_event: PendingIdentitiesPurged) -> None:
            purge_recording.set()
            assert release_purge.wait(10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            purge_future = pool.submit(
                purger.purge_stale_pending_identities,
                actor=target_actor,
                retention_days=90,
                record=hold_purge,
            )
            assert purge_recording.wait(10)
            revoke_future = pool.submit(
                revoker.revoke_role,
                actor=root_actor,
                role_id=target_grant.role_id,
                note="retire purge operator",
                record=lambda _event: None,
            )
            _wait_for_lock(purge_postgres, "role-race-revoker")
            release_purge.set()
            assert purge_future.result(timeout=10).identity_ids == (purge_wins,)
            assert revoke_future.result(timeout=10).revoked_at is not None

        replacement_grant = setup.grant_role(
            actor=root_actor,
            identity_id=target.record.identity_id,
            role="admin",
            scope=None,
            expires_at=None,
            note="second purge authority",
            record=lambda _event: None,
        )
        revoke_wins = _pending(setup, "purge-revoked-first")
        _age(purge_postgres, revoke_wins)
        revoke_recording = Event()
        release_revoke = Event()

        def hold_revoke(_event: object) -> None:
            revoke_recording.set()
            assert release_revoke.wait(10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            revoke_future = pool.submit(
                revoker.revoke_role,
                actor=root_actor,
                role_id=replacement_grant.role_id,
                note="revoke before purge",
                record=hold_revoke,
            )
            assert revoke_recording.wait(10)
            purge_future = pool.submit(
                purger.purge_stale_pending_identities,
                actor=target_actor,
                retention_days=90,
                record=lambda _event: None,
            )
            _wait_for_lock(purge_postgres, "role-race-purger")
            release_revoke.set()
            assert revoke_future.result(timeout=10).revoked_at is not None
            with pytest.raises(AdminAuthorityRequired):
                purge_future.result(timeout=10)
        assert setup.read_identity(identity_id=revoke_wins) is not None
    finally:
        purge_engine.dispose()
        revoke_engine.dispose()
