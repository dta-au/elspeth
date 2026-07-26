"""SQLite local-authority parity and locking proofs."""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, column, delete, event, insert, literal, select, table, update

from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import PostgresSessionOperationRepository, SessionOperationConflictError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    chat_messages_table,
    session_operation_fences_table,
    sessions_table,
)


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


def _snapshot_mutation_scope(
    engine,
    *,
    session_ids: tuple[str, ...],
    message_ids: tuple[str, ...],
    cleanup_ids: tuple[str, ...] = (),
) -> dict[str, tuple[dict, ...]]:
    with engine.connect() as conn:
        sessions = tuple(
            dict(row._mapping)
            for row in conn.execute(select(sessions_table).where(sessions_table.c.id.in_(session_ids)).order_by(sessions_table.c.id))
        )
        fences = tuple(
            dict(row._mapping)
            for row in conn.execute(
                select(session_operation_fences_table)
                .where(session_operation_fences_table.c.session_id.in_(session_ids))
                .order_by(session_operation_fences_table.c.session_id)
            )
        )
        messages = tuple(
            dict(row._mapping)
            for row in conn.execute(
                select(chat_messages_table).where(chat_messages_table.c.id.in_(message_ids)).order_by(chat_messages_table.c.id)
            )
        )
        cleanups = tuple(
            dict(row._mapping)
            for row in conn.execute(
                select(blob_deletion_cleanups_table)
                .where(blob_deletion_cleanups_table.c.blob_id.in_(cleanup_ids))
                .order_by(blob_deletion_cleanups_table.c.blob_id)
            )
        )
    return {"sessions": sessions, "fences": fences, "messages": messages, "cleanups": cleanups}


def _message_values(*, message_id: str, session_id: str | None = None) -> dict[str, object]:
    values: dict[str, object] = {
        "id": message_id,
        "role": "user",
        "content": "scoped message",
        "sequence_no": 1,
        "writer_principal": "route_user_message",
        "created_at": datetime.now(UTC),
    }
    if session_id is not None:
        values["session_id"] = session_id
    return values


def _cleanup_values(*, cleanup_id: str, session_id: str | None = None) -> dict[str, object]:
    values: dict[str, object] = {
        "blob_id": cleanup_id,
        "storage_path": f"{cleanup_id}.blob",
        "created_at": datetime.now(UTC),
    }
    if session_id is not None:
        values["session_id"] = session_id
    return values


def test_sqlite_fenced_mutation_auto_scopes_update_delete_and_select(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_a = _created(authority)
    session_b = _created(authority)
    cleanup_b = str(uuid4())
    with engine.begin() as conn:
        conn.execute(insert(blob_deletion_cleanups_table).values(**_cleanup_values(cleanup_id=cleanup_b, session_id=str(session_b.id))))
    fence = authority.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id))
    before = _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=(), cleanup_ids=(cleanup_b,))

    def attempt_cross_session_access(transaction):
        changed = transaction.execute(
            update(sessions_table).where(sessions_table.c.id == str(session_b.id)).values(title="cross-session update")
        )
        removed = transaction.execute(delete(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == cleanup_b))
        visible = transaction.execute(select(sessions_table.c.id).order_by(sessions_table.c.id))
        return changed, removed, visible

    changed, removed, visible = authority.mutate(fence, attempt_cross_session_access)

    assert changed.rowcount == 0
    assert removed.rowcount == 0
    assert [row["id"] for row in visible.rows] == [str(session_a.id)]
    assert _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=(), cleanup_ids=(cleanup_b,)) == before


def test_sqlite_fenced_mutation_supports_same_session_crud(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    fence = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    cleanup_id = str(uuid4())

    def same_session_crud(transaction):
        changed = transaction.execute(update(sessions_table).values(title="same-session update"))
        inserted = transaction.execute(insert(blob_deletion_cleanups_table).values(**_cleanup_values(cleanup_id=cleanup_id)))
        visible = transaction.execute(select(blob_deletion_cleanups_table.c.blob_id, blob_deletion_cleanups_table.c.session_id))
        removed = transaction.execute(delete(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == cleanup_id))
        return changed, inserted, visible, removed

    changed, inserted, visible, removed = authority.mutate(fence, same_session_crud)

    assert changed.rowcount == 1
    assert inserted.rowcount == 1
    assert visible.rows == ({"blob_id": cleanup_id, "session_id": str(created.id)},)
    assert removed.rowcount == 1
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one() == (
            "same-session update"
        )
        assert (
            conn.execute(select(blob_deletion_cleanups_table.c.blob_id).where(blob_deletion_cleanups_table.c.blob_id == cleanup_id)).first()
            is None
        )


@pytest.mark.parametrize(
    "attack_kind",
    (
        "caller_selected_parent_insert",
        "lightweight_parent_delete",
        "lightweight_fence_update",
        "reflected_fence_update",
        "reflected_sessions_update",
        "mismatched_child_insert",
        "multirow_child_insert",
        "from_select_child_insert",
        "lightweight_rate_update",
        "reflected_cleanup_delete",
        "protected_unscoped_select",
        "nested_protected_select",
        "nested_same_table_select",
        "nested_same_table_dml",
        "same_table_exists_dml",
        "raw_select_prefix",
    ),
)
def test_sqlite_fenced_mutation_refuses_unsafe_statement_and_rolls_back(engine, attack_kind: str) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_a = _created(authority)
    session_b = _created(authority)
    selected_parent_id = str(uuid4())
    selected_message_id = str(uuid4())
    second_selected_message_id = str(uuid4())
    fence = authority.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id), selected_parent_id)
    selected_message_ids = (selected_message_id, second_selected_message_id)
    before = _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=selected_message_ids)

    if attack_kind == "caller_selected_parent_insert":
        statement = insert(sessions_table).values(
            id=selected_parent_id,
            user_id="attacker",
            auth_provider_type="local",
            title="caller selected",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    elif attack_kind == "lightweight_parent_delete":
        lightweight_sessions = table("sessions", column("id"))
        statement = delete(lightweight_sessions).where(lightweight_sessions.c.id == str(session_a.id))
    elif attack_kind == "lightweight_fence_update":
        lightweight_fences = table("session_operation_fences", column("session_id"), column("lease_token"))
        statement = (
            update(lightweight_fences)
            .where(lightweight_fences.c.session_id == str(session_a.id))
            .values(lease_token="forged-lightweight-token")
        )
    elif attack_kind == "reflected_fence_update":
        reflected_fences = Table("session_operation_fences", MetaData(), autoload_with=engine)
        statement = (
            update(reflected_fences)
            .where(reflected_fences.c.session_id == str(session_a.id))
            .values(operation_epoch=fence.operation_epoch + 100)
        )
    elif attack_kind == "reflected_sessions_update":
        reflected_sessions = Table("sessions", MetaData(), autoload_with=engine)
        statement = (
            update(reflected_sessions).where(reflected_sessions.c.id == str(session_b.id)).values(title="reflected cross-session update")
        )
    elif attack_kind == "mismatched_child_insert":
        statement = insert(chat_messages_table).values(**_message_values(message_id=selected_message_id, session_id=str(session_b.id)))
    elif attack_kind == "multirow_child_insert":
        statement = insert(chat_messages_table).values(
            [
                _message_values(message_id=selected_message_id, session_id=str(session_b.id)),
                _message_values(message_id=second_selected_message_id, session_id=str(session_b.id)),
            ]
        )
    elif attack_kind == "from_select_child_insert":
        statement = insert(chat_messages_table).from_select(
            ("id", "session_id", "role", "content", "sequence_no", "writer_principal", "created_at"),
            select(
                literal(selected_message_id),
                literal(str(session_b.id)),
                literal("user"),
                literal("from-select"),
                literal(1),
                literal("route_user_message"),
                literal(datetime.now(UTC)),
            ).where(literal(False)),
        )
    elif attack_kind == "lightweight_rate_update":
        lightweight_rate = table("rate_limit_buckets", column("subject_digest"))
        statement = update(lightweight_rate).where(lightweight_rate.c.subject_digest == "missing").values(subject_digest="forged-rate-key")
    elif attack_kind == "reflected_cleanup_delete":
        reflected_cleanup = Table("sessions_cleanup_claims", MetaData(), autoload_with=engine)
        statement = delete(reflected_cleanup)
    elif attack_kind == "protected_unscoped_select":
        statement = select(session_operation_fences_table)
    elif attack_kind == "nested_protected_select":
        statement = select(
            sessions_table.c.id,
            select(session_operation_fences_table.c.operation_id).limit(1).scalar_subquery(),
        )
    elif attack_kind == "nested_same_table_select":
        statement = select(
            sessions_table.c.id,
            select(sessions_table.c.title).order_by(sessions_table.c.id).limit(1).scalar_subquery(),
        )
    elif attack_kind == "nested_same_table_dml":
        statement = update(sessions_table).values(
            title=select(sessions_table.c.title).where(sessions_table.c.id == str(session_b.id)).limit(1).scalar_subquery()
        )
    elif attack_kind == "same_table_exists_dml":
        statement = (
            update(sessions_table)
            .where(select(sessions_table.c.id).where(sessions_table.c.id == str(session_b.id)).exists())
            .values(title="cross-session exists")
        )
    else:
        statement = select(sessions_table.c.id).prefix_with("ALL")

    refused = False

    def mutate_then_attack(transaction) -> None:
        transaction.execute(update(sessions_table).values(title="must roll back"))
        transaction.execute(statement)

    try:
        authority.mutate(fence, mutate_then_attack)
    except ValueError:
        refused = True

    assert _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=selected_message_ids) == before
    assert refused is True


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
