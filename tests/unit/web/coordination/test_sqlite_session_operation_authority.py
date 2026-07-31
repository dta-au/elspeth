"""SQLite local-authority parity and locking proofs."""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, insert, select, text, update
from sqlalchemy.engine import Connection, Engine, Transaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination import repository as coordination_repository
from elspeth.web.coordination.contracts import (
    ArchiveDeleteReconciliation,
    ArchiveManifestRelation,
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.repository import PostgresSessionOperationRepository, SessionOperationConflictError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    chat_messages_table,
    composition_states_table,
    guided_operations_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    GuidedOperationFence,
    SessionArchiveDisposition,
    SessionForkAuthority,
    SessionForkChildCreation,
    SessionForkChildStateCreation,
    SessionForkParentAuthority,
    SessionGuidedOperationInProgressError,
)


def _created(authority: SQLiteLocalSessionOperationAuthority):
    return authority.create_session_with_initial_fence(
        user_id="alice",
        title="SQLite local authority",
        auth_provider_type="local",
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )


def _callback_slot_graph(root: object) -> tuple[object, ...]:
    seen: set[int] = set()
    pending = [root]
    values: list[object] = []
    while pending:
        value = pending.pop()
        if id(value) in seen or isinstance(value, (str, bytes, int, float, bool, type(None), datetime, UUID, type)):
            continue
        seen.add(id(value))
        values.append(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(value)
            continue
        for owner in type(value).__mro__:
            slots = owner.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                attribute = f"_{owner.__name__.lstrip('_')}__{slot[2:]}" if slot.startswith("__") and not slot.endswith("__") else slot
                try:
                    pending.append(object.__getattribute__(value, attribute))
                except AttributeError:
                    continue
    return tuple(values)


def _active_locked_fork_pair_count() -> int | None:
    count = getattr(
        coordination_repository._SessionOperationAuthorityRepository,
        "_SessionOperationAuthorityRepository__active_locked_fork_pair_count",
        None,
    )
    return None if count is None else count()


def _fork_mutation_pair_count() -> int | None:
    count = getattr(coordination_repository, "_fork_mutation_pair_count", None)
    return None if count is None else count()


def test_sqlite_adapter_matches_postgres_authority_signatures() -> None:
    public_methods = (
        "create_session_with_initial_fence",
        "acquire",
        "renew",
        "compare_and_swap",
        "mutate",
        "mutate_fork_creation",
        "release",
        "archive_delete",
        "reconcile_archive_delete",
        "classify_archive_manifest",
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

    assert first.fence.operation_epoch == 2
    assert second.fence.operation_epoch == 3
    assert first.fence.lease_token != second.fence.lease_token


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

    assert recovered.fence.operation_epoch == expired.fence.operation_epoch + 1
    assert recovered.fence.operation_id != expired.fence.operation_id
    assert recovered.fence.lease_token != expired.fence.lease_token
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
        transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC))

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


@pytest.mark.parametrize(
    "action",
    ("renew", "compare_and_swap", "mutate", "release", "archive_delete", "reconcile_archive_delete"),
)
def test_forged_operation_kind_refuses_every_authority_action_before_mutation(engine, action: str) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    acquired = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    forged = SessionOperationContext(
        fence=acquired.fence,
        operation_kind=SessionOperationKind.ARCHIVE,
    )
    before = _snapshot_mutation_scope(
        engine,
        session_ids=(str(created.id),),
        message_ids=(),
    )
    callback_called = False

    def forbidden_mutation(_transaction) -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(SessionOperationFenceLost) as exc_info:
        if action == "renew":
            authority.renew(forged, lease_seconds=60)
        elif action == "compare_and_swap":
            authority.compare_and_swap(forged)
        elif action == "mutate":
            authority.mutate(forged, forbidden_mutation)
        elif action == "release":
            authority.release(forged)
        elif action == "archive_delete":
            authority.archive_delete(forged)
        else:
            authority.reconcile_archive_delete(forged)

    assert exc_info.value.reason is FenceLossReason.TOKEN_MISMATCH
    assert callback_called is False
    assert (
        _snapshot_mutation_scope(
            engine,
            session_ids=(str(created.id),),
            message_ids=(),
        )
        == before
    )


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


def _seed_parent_messages(engine, *, session_id: str, messages: tuple[tuple[UUID, int], ...]) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(chat_messages_table).values(
                [
                    {
                        **_message_values(message_id=str(message_id), session_id=session_id),
                        "sequence_no": sequence_no,
                    }
                    for message_id, sequence_no in messages
                ]
            )
        )


def _seed_fork_operation(engine, *, session_id: str, operation_id: str, result_session_id: str | None = None) -> None:
    # SQLite's repository clock is second-precision. Keep fixture creation
    # strictly before that DB-owned mutation time so the real ordering CHECK
    # remains active when a test exercises the guided binding UPDATE.
    now = datetime.now(UTC) - timedelta(seconds=1)
    with engine.begin() as conn:
        conn.execute(
            insert(guided_operations_table).values(
                session_id=session_id,
                operation_id=operation_id,
                kind="session_fork",
                status="in_progress",
                request_hash="a" * 64,
                lease_token="guided-lease",
                lease_expires_at=now.replace(year=now.year + 1),
                attempt=1,
                result_session_id=result_session_id,
                created_at=now,
                updated_at=now,
            )
        )


def _seed_completed_fork_result(engine, authority, *, result_session_id: UUID) -> None:
    parent = _created(authority)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(guided_operations_table).values(
                session_id=str(parent.id),
                operation_id=str(uuid4()),
                kind="session_fork",
                status="completed",
                request_hash="a" * 64,
                lease_token=None,
                lease_expires_at=None,
                attempt=1,
                result_kind="session",
                result_session_id=str(result_session_id),
                response_hash="b" * 64,
                created_at=now,
                updated_at=now,
                settled_at=now,
            )
        )


def _mutate_fork(
    authority: SQLiteLocalSessionOperationAuthority,
    engine,
    *,
    parent,
    operation_id: str,
    mutation,
    fork_message_id=None,
    pass_authority: bool = False,
):
    if fork_message_id is None:
        fork_message_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(chat_messages_table).values(
                **_message_values(
                    message_id=str(fork_message_id),
                    session_id=str(parent.id),
                )
            )
        )
    parent_context = authority.acquire(
        session_id=parent.id,
        operation_kind=SessionOperationKind.SESSION_FORK,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    return authority.mutate_fork_creation(
        SessionForkParentAuthority(
            parent_context=parent_context,
            guided_fence=GuidedOperationFence(
                session_id=parent.id,
                operation_id=operation_id,
                lease_token="guided-lease",
                attempt=1,
            ),
        ),
        SessionForkChildCreation(
            user_id="alice",
            auth_provider_type="local",
            title="Hidden child",
            created_at=datetime.now(UTC),
            archived_at=datetime.now(UTC),
            forked_from_message_id=fork_message_id,
        ),
        lambda transaction, fork_authority: mutation(transaction, fork_authority) if pass_authority else mutation(transaction),
    )


def test_sqlite_fenced_mutation_auto_scopes_update_delete_and_select(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_a = _created(authority)
    session_b = _created(authority)
    _seed_completed_fork_result(engine, authority, result_session_id=session_b.id)
    context = authority.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id))
    before = _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=())

    disposition = authority.mutate(
        context,
        lambda transaction: transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC)),
    )

    assert disposition is SessionArchiveDisposition.PHYSICAL_DELETE
    assert _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=()) == before


def test_sqlite_fenced_mutation_supports_same_session_crud(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    _seed_completed_fork_result(engine, authority, result_session_id=created.id)
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    archived_at = datetime.now(UTC)

    disposition = authority.mutate(
        context,
        lambda transaction: transaction.session.decide_and_soft_archive(archived_at=archived_at),
    )

    assert disposition is SessionArchiveDisposition.SOFT_ARCHIVED
    with engine.connect() as conn:
        row = conn.execute(select(sessions_table).where(sessions_table.c.id == str(created.id))).one()
    assert row.archived_at is not None
    assert row.title == "SQLite local authority"


@pytest.mark.parametrize("blocker_relation", ("own", "incoming"))
def test_sqlite_archive_capability_rejects_active_guided_blockers(engine, blocker_relation: str) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    target = _created(authority)
    if blocker_relation == "own":
        _seed_fork_operation(engine, session_id=str(target.id), operation_id=str(uuid4()))
    else:
        parent = _created(authority)
        _seed_fork_operation(
            engine,
            session_id=str(parent.id),
            operation_id=str(uuid4()),
            result_session_id=str(target.id),
        )
    context = authority.acquire(
        session_id=target.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )

    with pytest.raises(SessionGuidedOperationInProgressError):
        authority.mutate(
            context,
            lambda transaction: transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC)),
        )

    authority.compare_and_swap(context)
    with engine.connect() as conn:
        archived_at = conn.execute(select(sessions_table.c.archived_at).where(sessions_table.c.id == str(target.id))).scalar_one()
    assert archived_at is None


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
    context = authority.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id))
    before = _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=())

    def prove_statement_surface_absent(transaction) -> None:
        assert attack_kind
        assert not hasattr(transaction, "execute")
        assert not hasattr(transaction.session, "execute")
        assert not hasattr(transaction.runs, "execute")
        assert not hasattr(transaction.blobs, "execute")

    authority.mutate(context, prove_statement_surface_absent)

    assert _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=()) == before


@pytest.mark.parametrize(
    "assignment_shape",
    (
        "child_values_string",
        "child_values_column_unchanged",
        "child_ordered_string",
        "child_ordered_column",
        "parent_values_id_unchanged",
        "parent_ordered_id_unchanged",
        "dialect_upsert",
    ),
)
def test_sqlite_fenced_update_rejects_every_ownership_assignment(engine, assignment_shape: str) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_a = _created(authority)
    session_b = _created(authority)
    context = authority.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id))
    before = _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=())

    def prove_ownership_assignment_surface_absent(transaction) -> None:
        assert assignment_shape
        for capability in (transaction, transaction.session, transaction.runs, transaction.blobs):
            assert not hasattr(capability, "execute")
            assert not hasattr(capability, "session_id")
            assert not hasattr(capability, "update")

    authority.mutate(context, prove_ownership_assignment_surface_absent)

    assert _snapshot_mutation_scope(engine, session_ids=session_ids, message_ids=()) == before


def test_sqlite_stale_archive_cannot_recreate_deleted_state(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    authority.archive_delete(context)

    with pytest.raises(SessionOperationFenceLost):
        authority.renew(context, lease_seconds=30)
    with pytest.raises(SessionOperationFenceLost):
        authority.release(context)
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(session_operation_fences_table.c.session_id).where(session_operation_fences_table.c.session_id == str(created.id))
            ).first()
            is None
        )


def test_sqlite_archive_reconciliation_is_current_before_and_consumed_after_cascade(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )

    assert authority.reconcile_archive_delete(context) is ArchiveDeleteReconciliation.CURRENT
    authority.archive_delete(context)
    assert authority.reconcile_archive_delete(context) is ArchiveDeleteReconciliation.CONSUMED


def test_sqlite_archive_reconciliation_current_probe_performs_no_dml(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        assert authority.reconcile_archive_delete(context) is ArchiveDeleteReconciliation.CURRENT
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert statements
    assert all(not statement.startswith(("insert ", "update ", "delete ")) for statement in statements)


def test_sqlite_archive_manifest_classifier_returns_current_and_stale_without_dml(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    with engine.connect() as conn:
        predecessor = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        stale = authority.classify_archive_manifest(
            context,
            manifest_operation_id=UUID(predecessor.operation_id),
            manifest_operation_epoch=predecessor.operation_epoch,
        )
        current = authority.classify_archive_manifest(
            context,
            manifest_operation_id=context.fence.operation_id,
            manifest_operation_epoch=context.fence.operation_epoch,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert stale is ArchiveManifestRelation.STALE_OPERATION
    assert current is ArchiveManifestRelation.CURRENT_OPERATION
    assert statements
    assert all(not statement.startswith(("insert ", "update ", "delete ")) for statement in statements)


def test_sqlite_archive_manifest_classifier_rejects_taken_over_context(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    stale = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner-a",
        lease_seconds=30,
    )
    _expire_fence(engine, session_id=str(created.id))
    current = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner-b",
        lease_seconds=30,
    )

    with pytest.raises(SessionOperationFenceLost) as raised:
        authority.classify_archive_manifest(
            stale,
            manifest_operation_id=stale.fence.operation_id,
            manifest_operation_epoch=stale.fence.operation_epoch,
        )

    assert raised.value.reason is FenceLossReason.STALE_EPOCH
    assert (
        authority.classify_archive_manifest(
            current,
            manifest_operation_id=stale.fence.operation_id,
            manifest_operation_epoch=stale.fence.operation_epoch,
        )
        is ArchiveManifestRelation.STALE_OPERATION
    )


def test_sqlite_archive_manifest_classifier_rejects_future_and_same_epoch_conflict(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )

    with pytest.raises(AuditIntegrityError, match="future operation epoch"):
        authority.classify_archive_manifest(
            context,
            manifest_operation_id=str(uuid4()),
            manifest_operation_epoch=context.fence.operation_epoch + 1,
        )
    with pytest.raises(AuditIntegrityError, match="same operation epoch"):
        authority.classify_archive_manifest(
            context,
            manifest_operation_id=str(uuid4()),
            manifest_operation_epoch=context.fence.operation_epoch,
        )
    for invalid_epoch in (True, 0, -1):
        with pytest.raises(ValueError, match="manifest_operation_epoch"):
            authority.classify_archive_manifest(
                context,
                manifest_operation_id=context.fence.operation_id,
                manifest_operation_epoch=invalid_epoch,  # type: ignore[arg-type]
            )
    for invalid_operation_id in ("not-a-uuid", context.fence.operation_id.upper(), 7):
        with pytest.raises((TypeError, ValueError), match="manifest_operation_id"):
            authority.classify_archive_manifest(
                context,
                manifest_operation_id=invalid_operation_id,  # type: ignore[arg-type]
                manifest_operation_epoch=context.fence.operation_epoch,
            )


def test_sqlite_archive_manifest_classifier_rejects_released_expired_and_wrong_kind_contexts(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)

    released_session = _created(authority)
    released = authority.acquire(
        session_id=released_session.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    authority.release(released)
    with pytest.raises(SessionOperationFenceLost) as released_error:
        authority.classify_archive_manifest(
            released,
            manifest_operation_id=released.fence.operation_id,
            manifest_operation_epoch=released.fence.operation_epoch,
        )
    assert released_error.value.reason is FenceLossReason.RELEASED

    expired_session = _created(authority)
    expired = authority.acquire(
        session_id=expired_session.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    _expire_fence(engine, session_id=str(expired_session.id))
    with pytest.raises(SessionOperationFenceLost) as expired_error:
        authority.classify_archive_manifest(
            expired,
            manifest_operation_id=expired.fence.operation_id,
            manifest_operation_epoch=expired.fence.operation_epoch,
        )
    assert expired_error.value.reason is FenceLossReason.LEASE_EXPIRED

    wrong_kind_session = _created(authority)
    wrong_kind = authority.acquire(
        session_id=wrong_kind_session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    with pytest.raises(SessionOperationFenceLost) as wrong_kind_error:
        authority.classify_archive_manifest(
            wrong_kind,
            manifest_operation_id=wrong_kind.fence.operation_id,
            manifest_operation_epoch=wrong_kind.fence.operation_epoch,
        )
    assert wrong_kind_error.value.reason is FenceLossReason.TOKEN_MISMATCH


@pytest.mark.parametrize("remaining_row", ("session", "fence"))
def test_sqlite_archive_manifest_classifier_rejects_half_present_session_fence_pair(
    engine,
    remaining_row: str,
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    with engine.begin() as conn:
        if remaining_row == "session":
            conn.execute(delete(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
    if remaining_row == "fence":
        raw_connection = engine.raw_connection()
        cursor = raw_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = OFF")
            cursor.execute("DELETE FROM sessions WHERE id = ?", (str(created.id),))
            raw_connection.commit()
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()
            raw_connection.close()

    with pytest.raises(AuditIntegrityError, match="archive manifest classification"):
        authority.classify_archive_manifest(
            context,
            manifest_operation_id=context.fence.operation_id,
            manifest_operation_epoch=context.fence.operation_epoch,
        )


def test_sqlite_archive_reconciliation_rejects_stale_takeover_context_while_rows_exist(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    stale = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner-a",
        lease_seconds=30,
    )
    _expire_fence(engine, session_id=str(created.id))
    current = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner-b",
        lease_seconds=30,
    )

    with pytest.raises(SessionOperationFenceLost) as raised:
        authority.reconcile_archive_delete(stale)
    assert raised.value.reason is FenceLossReason.STALE_EPOCH
    assert authority.reconcile_archive_delete(current) is ArchiveDeleteReconciliation.CURRENT


@pytest.mark.parametrize("remaining_row", ("session", "fence"))
def test_sqlite_archive_reconciliation_rejects_one_row_inconsistency(engine, remaining_row: str) -> None:
    from elspeth.contracts.errors import AuditIntegrityError

    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = _created(authority)
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    with engine.begin() as conn:
        if remaining_row == "session":
            conn.execute(delete(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
    if remaining_row == "fence":
        raw_connection = engine.raw_connection()
        cursor = raw_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = OFF")
            cursor.execute("DELETE FROM sessions WHERE id = ?", (str(created.id),))
            raw_connection.commit()
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()
            raw_connection.close()

    with pytest.raises(AuditIntegrityError, match="session operation archive reconciliation"):
        authority.reconcile_archive_delete(context)


def test_fork_creation_transaction_refuses_protected_table_dml(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    def forbidden(transaction) -> None:
        transaction.execute(update(sessions_table).where(sessions_table.c.id == str(parent.id)).values(title="forbidden"))

    with pytest.raises(AttributeError, match="execute"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=forbidden,
        )

    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(parent.id))).scalar_one() == (
            "SQLite local authority"
        )
        assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.forked_from_session_id == str(parent.id))).first() is None


def test_fork_creation_callback_graph_has_no_database_handle_or_third_session_escape(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    third = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    captured_token: list[str] = []

    class ProbeComplete(Exception):
        pass

    def inspect_then_abort(transaction) -> None:
        child_mutations = transaction.child_mutations
        parent_guided_mutations = transaction.parent_guided_mutations
        reachable = _callback_slot_graph((transaction, child_mutations, parent_guided_mutations))
        assert not any(isinstance(value, (Connection, Engine, Transaction)) for value in reachable)
        for capability in (transaction, child_mutations, parent_guided_mutations):
            assert not hasattr(capability, "_active_connection")
            assert not hasattr(capability, "connection")
            assert not hasattr(capability, "engine")
            assert not hasattr(capability, "execute")
        assert not hasattr(transaction, "insert_child_state")
        assert not hasattr(transaction, "append_child_messages")
        assert not hasattr(transaction, "bind_guided_fork")
        assert not hasattr(child_mutations, "bind_guided_fork")
        assert not hasattr(parent_guided_mutations, "insert_child_state")
        assert not hasattr(parent_guided_mutations, "append_child_messages")
        captured_token.append(object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token"))
        with pytest.raises(AttributeError):
            leaked = object.__getattribute__(transaction, "_ForkCreationTransaction__connection")
            leaked.execute(update(sessions_table).where(sessions_table.c.id == str(third.id)).values(title="Escaped"))
        raise ProbeComplete

    with pytest.raises(ProbeComplete):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=inspect_then_abort,
        )

    assert captured_token[0] not in coordination_repository._MUTATION_CONNECTION_REGISTRY
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(third.id))).scalar_one() == (
            "SQLite local authority"
        )


def test_fork_creation_transaction_constructor_derives_pair_from_exact_authority() -> None:
    parameters = inspect.signature(coordination_repository._ForkCreationTransaction).parameters
    assert tuple(parameters) == (
        "connection",
        "fork_authority",
        "guided_operation",
        "database_now",
        "child_created",
    )
    assert "parent_session_id" not in parameters
    assert "child_session_id" not in parameters


def test_fork_construction_authority_has_no_module_settable_mint_surface() -> None:
    assert not hasattr(coordination_repository, "_FORK_TRANSACTION_CONSTRUCTION_AUTHORIZATION")
    assert not hasattr(
        coordination_repository._SessionOperationAuthorityRepository,
        "_SessionOperationAuthorityRepository__active_locked_fork_pairs",
    )
    assert not hasattr(
        coordination_repository._SessionOperationAuthorityRepository,
        "_SessionOperationAuthorityRepository__active_locked_fork_pairs_snapshot",
    )


def test_fork_token_pair_authority_has_no_module_or_class_mutable_registry_surface() -> None:
    for owner in (
        coordination_repository,
        coordination_repository._SessionOperationAuthorityRepository,
    ):
        mutable_surfaces = {
            name
            for name in vars(owner)
            if "fork_mutation_pair_registry" in name.lower() or "fork_mutation_pair_registry_lock" in name.lower()
        }
        assert mutable_surfaces == set()


def test_captured_fork_mutation_facets_fail_closed_before_sql_after_callback(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    fork_message_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    captured: dict[str, object] = {}

    def capture_and_bind(transaction, _fork_authority) -> None:
        captured["child"] = transaction.child_mutations
        captured["guided"] = transaction.parent_guided_mutations
        captured["token"] = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=fork_message_id)
        transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=fork_message_id)

    _mutate_fork(
        authority,
        engine,
        parent=parent,
        operation_id=operation_id,
        fork_message_id=fork_message_id,
        mutation=capture_and_bind,
        pass_authority=True,
    )

    assert captured["token"] not in coordination_repository._MUTATION_CONNECTION_REGISTRY
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    child = captured["child"]
    guided = captured["guided"]
    state = SessionForkChildStateCreation(
        id=uuid4(),
        data=CompositionStateData(),
        created_at=datetime.now(UTC),
    )
    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(RuntimeError, match="closed"):
            child.insert_child_state(state)
        with pytest.raises(RuntimeError, match="closed"):
            child.append_child_messages(())
        with pytest.raises(RuntimeError, match="closed"):
            guided.bind_guided_fork(originating_message_id=fork_message_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)
    assert statements == []


def test_fork_child_facet_rejects_foreign_context_before_allocation_or_insert(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    statements: list[str] = []

    class ProbeComplete(Exception):
        pass

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    def reject_foreign_context(transaction, fork_authority) -> None:
        _row, database_now = transaction.require_parent_guided_operation(fork_authority.parent.guided_fence)
        exact = fork_authority.child_context.fence
        foreign_context = SessionOperationContext(
            fence=SessionOperationFence(
                session_id=exact.session_id,
                operation_id=exact.operation_id,
                lease_token="foreign-child-lease",
                operation_epoch=exact.operation_epoch,
            ),
            operation_kind=SessionOperationKind.SESSION_FORK,
        )
        token = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        foreign_facet = coordination_repository._ForkChildSessionMutations(
            token,
            parent_session_id=fork_authority.parent.parent_context.fence.session_id,
            child_context=foreign_context,
            database_now=database_now,
        )
        statements.clear()
        with pytest.raises((AuditIntegrityError, SessionOperationFenceLost)):
            foreign_facet.insert_child_state(
                SessionForkChildStateCreation(
                    id=uuid4(),
                    data=CompositionStateData(),
                    created_at=datetime.now(UTC),
                )
            )
        normalized = [statement.lower() for statement in statements]
        assert not any("max(" in statement for statement in normalized)
        assert not any(statement.lstrip().startswith("insert") for statement in normalized)
        raise ProbeComplete

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(ProbeComplete):
            _mutate_fork(
                authority,
                engine,
                parent=parent,
                operation_id=operation_id,
                mutation=reject_foreign_context,
                pass_authority=True,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)


def test_fork_pair_mint_rejects_valid_live_foreign_child_and_unbound_retokenization(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    third = _created(authority)
    third_context = authority.acquire(
        session_id=third.id,
        operation_kind=SessionOperationKind.SESSION_FORK,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    operation_id = str(uuid4())
    fork_message_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    statements: list[str] = []
    captured_token: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    def reject_foreign_pair(transaction, fork_authority) -> None:
        row, database_now = transaction.require_parent_guided_operation(fork_authority.parent.guided_fence)
        token = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        captured_token.append(token)
        connection = coordination_repository._resolve_mutation_connection(token)
        foreign_authority = SessionForkAuthority(parent=fork_authority.parent, child_context=third_context)
        transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=fork_message_id)

        statements.clear()
        foreign_transaction = None
        try:
            with pytest.raises(AuditIntegrityError, match="active locked fork pair"):
                foreign_transaction = coordination_repository._ForkCreationTransaction(
                    connection,
                    fork_authority=foreign_authority,
                    guided_operation=row,
                    database_now=database_now,
                    child_created=True,
                )
                foreign_transaction.child_mutations.insert_child_state(
                    SessionForkChildStateCreation(
                        id=uuid4(),
                        data=CompositionStateData(),
                        created_at=datetime.now(UTC),
                    )
                )
        finally:
            if foreign_transaction is not None:
                foreign_transaction._close()
        assert not any("max(" in statement.lower() for statement in statements)
        assert not any(statement.lstrip().lower().startswith("insert") for statement in statements)

        statements.clear()
        foreign_facet = coordination_repository._ForkChildSessionMutations(
            token,
            parent_session_id=fork_authority.parent.parent_context.fence.session_id,
            child_context=third_context,
            database_now=database_now,
        )
        with pytest.raises(AuditIntegrityError, match="exact fork pair"):
            foreign_facet.insert_child_state(
                SessionForkChildStateCreation(
                    id=uuid4(),
                    data=CompositionStateData(),
                    created_at=datetime.now(UTC),
                )
            )
        assert not any("max(" in statement.lower() for statement in statements)
        assert not any(statement.lstrip().lower().startswith("insert") for statement in statements)

        new_token = coordination_repository._register_mutation_connection(connection)
        try:
            statements.clear()
            unbound_facet = coordination_repository._ForkChildSessionMutations(
                new_token,
                parent_session_id=fork_authority.parent.parent_context.fence.session_id,
                child_context=third_context,
                database_now=database_now,
            )
            with pytest.raises(AuditIntegrityError, match="exact fork pair"):
                unbound_facet.insert_child_state(
                    SessionForkChildStateCreation(
                        id=uuid4(),
                        data=CompositionStateData(),
                        created_at=datetime.now(UTC),
                    )
                )
            assert not any("max(" in statement.lower() for statement in statements)
            assert not any(statement.lstrip().lower().startswith("insert") for statement in statements)
        finally:
            coordination_repository._unregister_mutation_connection(new_token)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            fork_message_id=fork_message_id,
            mutation=reject_foreign_pair,
            pass_authority=True,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert _fork_mutation_pair_count() == 0
    with engine.connect() as conn:
        assert (
            conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == str(third.id))).first()
            is None
        )


def test_fork_callback_cannot_forge_secondary_transaction_for_unlocked_live_pair(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    third = _created(authority)
    third_context = authority.acquire(
        session_id=third.id,
        operation_kind=SessionOperationKind.SESSION_FORK,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    statements: list[str] = []

    class ProbeComplete(Exception):
        pass

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    def reject_forged_mint(transaction, fork_authority) -> None:
        row, database_now = transaction.require_parent_guided_operation(fork_authority.parent.guided_fence)
        token = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        connection = coordination_repository._resolve_mutation_connection(token)
        foreign_authority = SessionForkAuthority(parent=fork_authority.parent, child_context=third_context)
        module_mint = getattr(coordination_repository, "_FORK_TRANSACTION_CONSTRUCTION_AUTHORIZATION", None)
        forged_token = (
            module_mint.set(
                (
                    connection,
                    foreign_authority,
                    fork_authority.parent.parent_context.fence.session_id,
                    third_context.fence.session_id,
                )
            )
            if module_mint is not None
            else None
        )
        foreign_transaction = None
        statements.clear()
        try:
            with pytest.raises(AuditIntegrityError, match="active locked fork pair"):
                foreign_transaction = coordination_repository._ForkCreationTransaction(
                    connection,
                    fork_authority=foreign_authority,
                    guided_operation=row,
                    database_now=database_now,
                    child_created=True,
                )
                foreign_transaction.child_mutations.insert_child_state(
                    SessionForkChildStateCreation(
                        id=uuid4(),
                        data=CompositionStateData(),
                        created_at=datetime.now(UTC),
                    )
                )
        finally:
            if foreign_transaction is not None:
                foreign_transaction._close()
            if forged_token is not None:
                module_mint.reset(forged_token)
        assert not any("max(" in statement.lower() for statement in statements)
        assert not any(statement.lstrip().lower().startswith("insert") for statement in statements)
        raise ProbeComplete

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(ProbeComplete):
            _mutate_fork(
                authority,
                engine,
                parent=parent,
                operation_id=operation_id,
                mutation=reject_forged_mint,
                pass_authority=True,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    with engine.connect() as conn:
        assert (
            conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == str(third.id))).first()
            is None
        )


def test_fork_secondary_transaction_cannot_reverse_parent_and_child_roles(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    fork_message_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    statements: list[str] = []
    observed: dict[str, object] = {}

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    def reject_role_reversal(transaction, fork_authority) -> None:
        row, database_now = transaction.require_parent_guided_operation(fork_authority.parent.guided_fence)
        canonical_token = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        connection = coordination_repository._resolve_mutation_connection(canonical_token)
        guided_fence = fork_authority.parent.guided_fence
        reversed_parent = SessionForkParentAuthority(
            parent_context=fork_authority.child_context,
            guided_fence=GuidedOperationFence(
                session_id=UUID(fork_authority.child_context.fence.session_id),
                operation_id=guided_fence.operation_id,
                lease_token=guided_fence.lease_token,
                attempt=guided_fence.attempt,
            ),
        )
        reversed_authority = SessionForkAuthority(
            parent=reversed_parent,
            child_context=fork_authority.parent.parent_context,
        )
        reversed_transaction = None
        statements.clear()
        try:
            reversed_transaction = coordination_repository._ForkCreationTransaction(
                connection,
                fork_authority=reversed_authority,
                guided_operation=row,
                database_now=database_now,
                child_created=True,
            )
            reversed_transaction.child_mutations.insert_child_state(
                SessionForkChildStateCreation(
                    id=uuid4(),
                    data=CompositionStateData(),
                    created_at=datetime.now(UTC),
                )
            )
        except AuditIntegrityError as error:
            observed["error"] = error
        finally:
            if reversed_transaction is not None:
                reversed_transaction._close()
        observed["target_statements"] = tuple(statements)
        transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=fork_message_id)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            fork_message_id=fork_message_id,
            mutation=reject_role_reversal,
            pass_authority=True,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    target_statements = tuple(str(statement).lower() for statement in observed["target_statements"])
    with engine.connect() as conn:
        parent_state_count = conn.execute(
            select(func.count(composition_states_table.c.id)).where(composition_states_table.c.session_id == str(parent.id))
        ).scalar_one()
    assert (
        parent_state_count,
        isinstance(observed.get("error"), AuditIntegrityError),
        any("max(" in statement or statement.lstrip().startswith("insert") for statement in target_statements),
    ) == (0, True, False)
    assert "active locked fork pair" in str(observed["error"])


def test_fork_callback_cannot_seed_fresh_token_pair_for_unlocked_live_child(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    third = _created(authority)
    third_context = authority.acquire(
        session_id=third.id,
        operation_kind=SessionOperationKind.SESSION_FORK,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    statements: list[str] = []

    class ProbeComplete(Exception):
        pass

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    def reject_seeded_pair(transaction, fork_authority) -> None:
        _row, database_now = transaction.require_parent_guided_operation(fork_authority.parent.guided_fence)
        canonical_token = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        connection = coordination_repository._resolve_mutation_connection(canonical_token)
        fresh_token = coordination_repository._register_mutation_connection(connection)
        module_pair_registry = getattr(coordination_repository, "_FORK_MUTATION_PAIR_REGISTRY", None)
        if module_pair_registry is not None:
            module_pair_registry[fresh_token] = (
                fork_authority.parent.parent_context.fence.session_id,
                third_context.fence.session_id,
            )
        statements.clear()
        try:
            foreign_facet = coordination_repository._ForkChildSessionMutations(
                fresh_token,
                parent_session_id=fork_authority.parent.parent_context.fence.session_id,
                child_context=third_context,
                database_now=database_now,
            )
            with pytest.raises(AuditIntegrityError, match="exact fork pair"):
                foreign_facet.insert_child_state(
                    SessionForkChildStateCreation(
                        id=uuid4(),
                        data=CompositionStateData(),
                        created_at=datetime.now(UTC),
                    )
                )
        finally:
            if module_pair_registry is not None:
                module_pair_registry.pop(fresh_token, None)
            coordination_repository._unregister_mutation_connection(fresh_token)
        assert not any("max(" in statement.lower() for statement in statements)
        assert not any(statement.lstrip().lower().startswith("insert") for statement in statements)
        raise ProbeComplete

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(ProbeComplete):
            _mutate_fork(
                authority,
                engine,
                parent=parent,
                operation_id=operation_id,
                mutation=reject_seeded_pair,
                pass_authority=True,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    with engine.connect() as conn:
        assert (
            conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == str(third.id))).first()
            is None
        )


@pytest.mark.parametrize("outcome", ("success", "callback_failure", "constructor_failure"))
def test_fork_active_lock_scope_registry_cleans_up(engine, monkeypatch, outcome: str) -> None:
    active_before = _active_locked_fork_pair_count()
    assert active_before is not None
    pair_before = _fork_mutation_pair_count()
    assert pair_before is not None
    connection_before = dict(coordination_repository._MUTATION_CONNECTION_REGISTRY)

    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    fork_message_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    class CallbackFailure(Exception):
        pass

    class ConstructorFailure(Exception):
        pass

    def bind_guided(transaction) -> None:
        transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=fork_message_id)

    def fail_callback(_transaction) -> None:
        raise CallbackFailure

    if outcome == "constructor_failure":

        def fail_constructor(*_args, **_kwargs):
            raise ConstructorFailure

        monkeypatch.setattr(coordination_repository, "_ForkChildSessionMutations", fail_constructor)
        with pytest.raises(ConstructorFailure):
            _mutate_fork(
                authority,
                engine,
                parent=parent,
                operation_id=operation_id,
                mutation=bind_guided,
                fork_message_id=fork_message_id,
            )
    elif outcome == "callback_failure":
        with pytest.raises(CallbackFailure):
            _mutate_fork(
                authority,
                engine,
                parent=parent,
                operation_id=operation_id,
                mutation=fail_callback,
                fork_message_id=fork_message_id,
            )
    else:
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=bind_guided,
            fork_message_id=fork_message_id,
        )

    assert _active_locked_fork_pair_count() == active_before
    assert _fork_mutation_pair_count() == pair_before
    assert connection_before == coordination_repository._MUTATION_CONNECTION_REGISTRY


def test_fork_parent_guided_facet_rejects_mismatched_authority_before_update(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    fork_message_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    statements: list[str] = []

    class ProbeComplete(Exception):
        pass

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    def reject_mismatched_guided(transaction, fork_authority) -> None:
        row, database_now = transaction.require_parent_guided_operation(fork_authority.parent.guided_fence)
        mismatched_parent = SessionForkParentAuthority(
            parent_context=fork_authority.parent.parent_context,
            guided_fence=replace(fork_authority.parent.guided_fence, operation_id=str(uuid4())),
        )
        mismatched_authority = SessionForkAuthority(
            parent=mismatched_parent,
            child_context=fork_authority.child_context,
        )
        token = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        mismatched_facet = coordination_repository._ForkParentGuidedMutations(
            token,
            fork_authority=mismatched_authority,
            guided_operation=row,
            database_now=database_now,
        )
        statements.clear()
        with pytest.raises(AuditIntegrityError, match="guided authority"):
            mismatched_facet.bind_guided_fork(originating_message_id=fork_message_id)
        assert not any("update guided_operations" in statement.lower() for statement in statements)
        raise ProbeComplete

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(ProbeComplete):
            _mutate_fork(
                authority,
                engine,
                parent=parent,
                operation_id=operation_id,
                fork_message_id=fork_message_id,
                mutation=reject_mismatched_guided,
                pass_authority=True,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)


def test_fork_parent_guided_facet_rejects_live_binding_drift_before_update(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    third = _created(authority)
    operation_id = str(uuid4())
    fork_message_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    statements: list[str] = []

    class ProbeComplete(Exception):
        pass

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    def reject_live_drift(transaction, fork_authority) -> None:
        token = object.__getattribute__(transaction, "_ForkCreationTransaction__connection_token")
        connection = coordination_repository._resolve_mutation_connection(token)
        connection.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
            .values(result_session_id=str(third.id))
        )
        statements.clear()
        with pytest.raises(AuditIntegrityError, match="live guided authority"):
            transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=fork_message_id)
        assert not any("update guided_operations" in statement.lower() for statement in statements)
        raise ProbeComplete

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(ProbeComplete):
            _mutate_fork(
                authority,
                engine,
                parent=parent,
                operation_id=operation_id,
                fork_message_id=fork_message_id,
                mutation=reject_live_drift,
                pass_authority=True,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(guided_operations_table.c.result_session_id).where(
                    guided_operations_table.c.session_id == str(parent.id),
                    guided_operations_table.c.operation_id == operation_id,
                )
            ).scalar_one()
            is None
        )


def test_fork_creation_callback_failure_rolls_back_child_and_closed_initial_fence(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    failure = RuntimeError("abort fork copy")

    def create_then_fail(_transaction) -> None:
        raise failure

    with pytest.raises(RuntimeError) as raised:
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=create_then_fail,
        )

    assert raised.value is failure
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.forked_from_session_id == str(parent.id))).first() is None


def test_fork_creation_transaction_refuses_raw_sql(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    with pytest.raises(AttributeError, match="execute"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=lambda transaction: transaction.execute(text("SELECT * FROM sessions")),
        )


@pytest.mark.parametrize("operation", ("insert", "update"))
def test_fork_creation_transaction_refuses_third_session_writes(engine, operation: str) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    third = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    def forbidden(transaction) -> None:
        if operation == "insert":
            transaction.execute(
                insert(chat_messages_table).values(
                    **_message_values(
                        message_id=str(uuid4()),
                        session_id=str(third.id),
                    )
                )
            )
        else:
            transaction.execute(
                update(guided_operations_table).where(guided_operations_table.c.session_id == str(third.id)).values(status="failed")
            )

    with pytest.raises(AttributeError, match="execute"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=forbidden,
        )


@pytest.mark.parametrize("column_name", ("session_id", "result_session_id"))
def test_fork_creation_transaction_refuses_third_session_update_values(engine, column_name: str) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    third = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    def forbidden(transaction) -> None:
        transaction.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
            .values(**{column_name: str(third.id)})
        )

    with pytest.raises(AttributeError, match="execute"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=forbidden,
        )


def test_fork_creation_transaction_refuses_sibling_guided_operation_update(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    sibling_operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=sibling_operation_id)

    with pytest.raises(AttributeError, match="execute"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=lambda transaction: transaction.execute(
                update(guided_operations_table)
                .where(
                    guided_operations_table.c.session_id == str(parent.id),
                    guided_operations_table.c.operation_id == sibling_operation_id,
                )
                .values(updated_at=datetime.now(UTC))
            ),
        )


def test_fork_creation_transaction_refuses_dynamic_update_values(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    with pytest.raises(AttributeError, match="execute"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=lambda transaction: transaction.execute(
                update(guided_operations_table)
                .where(
                    guided_operations_table.c.session_id == str(parent.id),
                    guided_operations_table.c.operation_id == operation_id,
                )
                .values(result_session_id=guided_operations_table.c.session_id)
            ),
        )


def test_fork_creation_requires_exact_guided_row_before_callback(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    callback_called = False

    def callback(_transaction) -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(AuditIntegrityError, match="guided operation"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=str(uuid4()),
            mutation=callback,
        )

    assert callback_called is False


def test_fork_creation_unbound_callback_omission_is_rejected(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    with pytest.raises(AuditIntegrityError, match="postcondition"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=lambda _transaction: None,
        )


def test_fork_creation_child_without_binding_rolls_back(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    def create_without_binding(_transaction) -> None:
        return None

    with pytest.raises(AuditIntegrityError, match="postcondition"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=create_without_binding,
        )

    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.forked_from_session_id == str(parent.id))).first() is None


def test_fork_creation_mismatched_message_binding_rolls_back_fresh_child(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    requested_message_id = uuid4()
    bound_message_id = uuid4()
    state_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    _seed_parent_messages(engine, session_id=str(parent.id), messages=((bound_message_id, 2),))

    def mutate_with_mismatched_message(transaction, fork_authority) -> None:
        transaction.child_mutations.insert_child_state(
            SessionForkChildStateCreation(
                id=state_id,
                data=CompositionStateData(),
                created_at=datetime.now(UTC),
            )
        )
        transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=bound_message_id)

    with pytest.raises(AuditIntegrityError, match="postcondition"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            fork_message_id=requested_message_id,
            mutation=mutate_with_mismatched_message,
            pass_authority=True,
        )

    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.forked_from_session_id == str(parent.id))).first() is None
        assert conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.id == str(state_id))).first() is None
        guided = conn.execute(
            select(
                guided_operations_table.c.originating_message_id,
                guided_operations_table.c.result_session_id,
            ).where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
        ).one()
    assert guided == (None, None)


def test_fork_creation_mismatched_message_binding_rolls_back_resumed_child(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    operation_id = str(uuid4())
    requested_message_id = uuid4()
    bound_message_id = uuid4()
    state_id = uuid4()
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)
    _seed_parent_messages(
        engine,
        session_id=str(parent.id),
        messages=((requested_message_id, 1), (bound_message_id, 2)),
    )
    parent_context = authority.acquire(
        session_id=parent.id,
        operation_kind=SessionOperationKind.SESSION_FORK,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    parent_authority = SessionForkParentAuthority(
        parent_context=parent_context,
        guided_fence=GuidedOperationFence(
            session_id=parent.id,
            operation_id=operation_id,
            lease_token="guided-lease",
            attempt=1,
        ),
    )
    child = SessionForkChildCreation(
        user_id="alice",
        auth_provider_type="local",
        title="Hidden child",
        created_at=datetime.now(UTC),
        archived_at=datetime.now(UTC),
        forked_from_message_id=requested_message_id,
    )
    child_session_id = authority.mutate_fork_creation(
        parent_authority,
        child,
        lambda transaction, fork_authority: (
            transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=requested_message_id),
            fork_authority.child_context.fence.session_id,
        )[1],
    )
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    with engine.begin() as conn:
        conn.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
            .values(originating_message_id=None)
        )
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == child_session_id)
            .values(lease_expires_at=expired_at, released_at=expired_at)
        )
    with engine.connect() as conn:
        fence_before = dict(
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == child_session_id))
            .mappings()
            .one()
        )

    def mutate_with_mismatched_message(transaction, _fork_authority) -> None:
        transaction.child_mutations.insert_child_state(
            SessionForkChildStateCreation(
                id=state_id,
                data=CompositionStateData(),
                created_at=datetime.now(UTC),
            )
        )
        transaction.parent_guided_mutations.bind_guided_fork(originating_message_id=bound_message_id)

    with pytest.raises(AuditIntegrityError, match="postcondition"):
        authority.mutate_fork_creation(
            parent_authority,
            child,
            mutate_with_mismatched_message,
        )

    with engine.connect() as conn:
        child_row = conn.execute(select(sessions_table).where(sessions_table.c.id == child_session_id)).mappings().one()
        guided = conn.execute(
            select(
                guided_operations_table.c.originating_message_id,
                guided_operations_table.c.result_session_id,
            ).where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
        ).one()
        fence_after = dict(
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == child_session_id))
            .mappings()
            .one()
        )
        state = conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.id == str(state_id))).first()
    assert child_row["forked_from_message_id"] == str(requested_message_id)
    assert guided == (None, child_session_id)
    assert fence_after == fence_before
    assert state is None


def test_fork_creation_refuses_binding_to_caller_selected_child(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parent = _created(authority)
    preexisting_candidate = _created(authority)
    operation_id = str(uuid4())
    _seed_fork_operation(engine, session_id=str(parent.id), operation_id=operation_id)

    def bind_without_hidden_child(transaction) -> None:
        transaction.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
            .values(result_session_id=str(preexisting_candidate.id))
        )

    with pytest.raises(AttributeError, match="execute"):
        _mutate_fork(
            authority,
            engine,
            parent=parent,
            operation_id=operation_id,
            mutation=bind_without_hidden_child,
        )

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(guided_operations_table.c.result_session_id).where(
                    guided_operations_table.c.session_id == str(parent.id),
                    guided_operations_table.c.operation_id == operation_id,
                )
            ).scalar_one()
            is None
        )


def test_sqlite_creation_does_not_offer_a_session_id_parameter(engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    parameters = inspect.signature(authority.create_session_with_initial_fence).parameters
    assert "session_id" not in parameters
    assert "operation_id" not in parameters
    assert "lease_token" not in parameters
