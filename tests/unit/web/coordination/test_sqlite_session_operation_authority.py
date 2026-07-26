"""SQLite local-authority parity and locking proofs."""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, update

from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import PostgresSessionOperationRepository, SessionOperationConflictError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import session_operation_fences_table, sessions_table


def _created(authority: SQLiteLocalSessionOperationAuthority):
    return authority.create_session_with_initial_fence(
        user_id="alice",
        title="SQLite local authority",
        auth_provider_type="local",
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )


def test_sqlite_adapter_matches_postgres_authority_signatures() -> None:
    public_methods = (
        "create_session_with_initial_fence",
        "acquire",
        "renew",
        "compare_and_swap",
        "mutate",
        "release",
        "archive_delete",
    )
    for method_name in public_methods:
        sqlite_signature = inspect.signature(getattr(SQLiteLocalSessionOperationAuthority, method_name))
        postgres_signature = inspect.signature(getattr(PostgresSessionOperationRepository, method_name))
        assert sqlite_signature == postgres_signature
        assert "connection" not in sqlite_signature.parameters
        assert "engine" not in sqlite_signature.parameters


def test_sqlite_uses_table_backed_random_epoch_cas(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    first = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    authority.compare_and_swap(first)
    authority.release(first)
    second = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )

    assert first.operation_epoch == 2
    assert second.operation_epoch == 3
    assert first.lease_token != second.lease_token


def test_sqlite_process_and_file_lock_allow_exactly_one_claimant(tmp_path) -> None:
    from elspeth.web.sessions.engine import create_session_engine
    from elspeth.web.sessions.schema import initialize_session_schema

    database_url = f"sqlite:///{tmp_path / 'authority.db'}"
    first_engine = create_session_engine(database_url)
    initialize_session_schema(first_engine)
    second_engine = create_session_engine(database_url)
    first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
    second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
    created = _created(first_authority)

    def claim(authority: SQLiteLocalSessionOperationAuthority, owner: str):
        try:
            return authority.acquire(
                session_id=created.id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=owner,
                lease_seconds=30,
            )
        except SessionOperationConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda item: claim(*item), ((first_authority, "owner-a"), (second_authority, "owner-b"))))

    assert sum(not isinstance(outcome, SessionOperationConflictError) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, SessionOperationConflictError) for outcome in outcomes) == 1
    first_engine.dispose()
    second_engine.dispose()


def _expire_fence(engine, *, session_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == session_id)
            .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )


def test_sqlite_live_active_fence_conflicts_for_different_local_owner(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )

    with pytest.raises(SessionOperationConflictError):
        authority.acquire(
            session_id=created.id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="different-local-owner",
            lease_seconds=30,
        )


def test_sqlite_expired_fence_recovers_locally_without_membership_lookup(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    expired = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="sqlite-owner-before-restart",
        lease_seconds=30,
    )
    _expire_fence(engine, session_id=str(created.id))
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        recovered = authority.acquire(
            session_id=created.id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="sqlite-owner-after-restart",
            lease_seconds=30,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert recovered.operation_epoch == expired.operation_epoch + 1
    assert recovered.operation_id != expired.operation_id
    assert recovered.lease_token != expired.lease_token
    assert all("web_instances" not in statement for statement in statements)
    with engine.connect() as conn:
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()
    assert row.owner_instance_id == "sqlite-owner-after-restart"
    assert row.released_at is None


def test_sqlite_stale_fence_cannot_mutate_or_release_after_expiry_recovery(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    stale = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="sqlite-owner-before-restart",
        lease_seconds=30,
    )
    _expire_fence(engine, session_id=str(created.id))
    recovered = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="sqlite-owner-after-restart",
        lease_seconds=30,
    )
    callback_called = False

    def stale_mutation(transaction) -> None:
        nonlocal callback_called
        callback_called = True
        transaction.execute(update(sessions_table).where(sessions_table.c.id == str(created.id)).values(title="Forbidden stale write"))

    with pytest.raises(SessionOperationFenceLost) as mutate_error:
        authority.mutate(stale, stale_mutation)
    with pytest.raises(SessionOperationFenceLost) as release_error:
        authority.release(stale)

    assert mutate_error.value.reason is FenceLossReason.STALE_EPOCH
    assert release_error.value.reason is FenceLossReason.STALE_EPOCH
    assert callback_called is False
    authority.compare_and_swap(recovered)
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one() == (
            "SQLite local authority"
        )


def test_sqlite_stale_archive_cannot_recreate_deleted_state(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    fence = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    authority.archive_delete(fence)

    with pytest.raises(SessionOperationFenceLost):
        authority.renew(fence, lease_seconds=30)
    with pytest.raises(SessionOperationFenceLost):
        authority.release(fence)
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(session_operation_fences_table.c.session_id).where(session_operation_fences_table.c.session_id == str(created.id))
            ).first()
            is None
        )


def test_sqlite_creation_does_not_offer_a_session_id_parameter(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parameters = inspect.signature(authority.create_session_with_initial_fence).parameters
    assert "session_id" not in parameters
    assert "operation_id" not in parameters
    assert "lease_token" not in parameters
