"""Real-PostgreSQL proofs for persistent session operation authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select, update

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.repository import PostgresSessionOperationRepository, SessionOperationConflictError
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, sessions_table, web_instances_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_engine(external_deployment_postgres_url: str) -> Engine:
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _create(repository: PostgresSessionOperationRepository, *, owner: str):
    return repository.create_session_with_initial_fence(
        user_id="alice",
        title="PostgreSQL fence",
        auth_provider_type="local",
        owner_instance_id=owner,
        lease_seconds=30,
    )


def _register_instance(engine: Engine, *, instance_id: str, lease_delta: timedelta) -> None:
    with engine.begin() as conn:
        database_now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            insert(web_instances_table).values(
                instance_id=instance_id,
                deployment_target="testcontainer",
                deployment_generation="generation-1",
                session_epoch=37,
                landscape_epoch=29,
                coordination_protocol=1,
                image_digest="sha256:test",
                revision_label="test-revision",
                state="active",
                started_at=database_now,
                last_heartbeat_at=database_now,
                lease_expires_at=database_now + lease_delta,
            )
        )


def test_postgres_creation_releases_epoch_one_at_database_time(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    with postgres_engine.connect() as conn:
        before = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    created = _create(repository, owner=f"creator-{uuid4()}")
    with postgres_engine.connect() as conn:
        after = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()

    assert row.operation_epoch == 1
    assert row.operation_kind == SessionOperationKind.CREATE.value
    assert row.released_at == row.lease_expires_at
    assert before <= row.released_at <= after
    assert row.operation_id and row.lease_token and row.owner_instance_id


def test_postgres_expiry_takeover_requires_operation_and_owner_instance_expiry(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    owner = f"owner-{uuid4()}"
    successor = f"successor-{uuid4()}"
    _register_instance(postgres_engine, instance_id=owner, lease_delta=timedelta(minutes=5))
    created = _create(repository, owner=owner)
    first = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=owner,
        lease_seconds=30,
    )
    with postgres_engine.begin() as conn:
        database_now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(created.id))
            .values(lease_expires_at=database_now - timedelta(seconds=1))
        )

    with pytest.raises(SessionOperationConflictError):
        repository.acquire(
            session_id=created.id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=successor,
            lease_seconds=30,
        )

    with postgres_engine.begin() as conn:
        database_now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == owner)
            .values(lease_expires_at=database_now - timedelta(seconds=1))
        )
    taken_over = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=successor,
        lease_seconds=30,
    )

    assert taken_over.operation_epoch == first.operation_epoch + 1
    assert taken_over.operation_id != first.operation_id
    assert taken_over.lease_token != first.lease_token


def test_postgres_missing_owner_membership_fails_closed_on_expired_operation(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    owner = f"unregistered-owner-{uuid4()}"
    created = _create(repository, owner=owner)
    repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=owner,
        lease_seconds=30,
    )
    with postgres_engine.begin() as conn:
        database_now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(created.id))
            .values(lease_expires_at=database_now - timedelta(seconds=1))
        )

    with pytest.raises(SessionOperationConflictError):
        repository.acquire(
            session_id=created.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=f"successor-{uuid4()}",
            lease_seconds=30,
        )


def test_postgres_two_claimants_have_exactly_one_winner(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")

    def claim(owner: str):
        contender = PostgresSessionOperationRepository(postgres_engine)
        try:
            return contender.acquire(
                session_id=created.id,
                operation_kind=SessionOperationKind.PROPOSAL,
                owner_instance_id=owner,
                lease_seconds=30,
            )
        except SessionOperationConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (f"claimant-{uuid4()}", f"claimant-{uuid4()}")))

    assert sum(isinstance(outcome, SessionOperationFence) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, SessionOperationConflictError) for outcome in outcomes) == 1


def test_postgres_renew_cas_and_release_are_exact(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    current = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id=f"progress-{uuid4()}",
        lease_seconds=30,
    )
    stale = SessionOperationFence(
        session_id=current.session_id,
        operation_id=current.operation_id,
        lease_token=f"stale-{uuid4()}",
        operation_epoch=current.operation_epoch,
    )
    with postgres_engine.connect() as conn:
        before = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        before = dict(before)

    for stale_mutation in (
        lambda: repository.compare_and_swap(stale),
        lambda: repository.mutate(
            stale,
            lambda transaction: transaction.execute(
                update(sessions_table).where(sessions_table.c.id == str(created.id)).values(title="Forbidden")
            ),
        ),
        lambda: repository.renew(stale, lease_seconds=60),
        lambda: repository.release(stale),
    ):
        with pytest.raises(SessionOperationFenceLost) as exc_info:
            stale_mutation()
        assert exc_info.value.reason is FenceLossReason.TOKEN_MISMATCH

    with postgres_engine.connect() as conn:
        after = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        after = dict(after)
    assert after == before

    assert repository.renew(current, lease_seconds=60) == current
    committed = repository.mutate(
        current,
        lambda transaction: transaction.execute(
            update(sessions_table).where(sessions_table.c.id == str(created.id)).values(title="Committed")
        ),
    )
    assert committed.rowcount == 1

    def rollback_mutation(transaction) -> None:
        transaction.execute(update(sessions_table).where(sessions_table.c.id == str(created.id)).values(title="Must roll back"))
        raise RuntimeError("abort PostgreSQL mutation")

    with pytest.raises(RuntimeError, match="abort PostgreSQL mutation"):
        repository.mutate(current, rollback_mutation)
    with postgres_engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one() == "Committed"
    repository.release(current)


def test_postgres_archive_delete_is_atomic_update_only_no_registry(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    fence = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"archive-{uuid4()}",
        lease_seconds=30,
    )
    repository.archive_delete(fence)

    with postgres_engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == str(created.id))).first() is None
        assert (
            conn.execute(
                select(session_operation_fences_table.c.session_id).where(session_operation_fences_table.c.session_id == str(created.id))
            ).first()
            is None
        )
        table_names = set(conn.dialect.get_table_names(conn))
        assert not {name for name in table_names if "session" in name and "deleted" in name}

    with pytest.raises(SessionOperationFenceLost) as exc_info:
        repository.archive_delete(fence)
    assert exc_info.value.reason is FenceLossReason.MISSING
