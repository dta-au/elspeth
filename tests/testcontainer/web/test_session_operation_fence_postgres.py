"""Real-PostgreSQL proofs for persistent session operation authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, event, insert, select, update

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
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    chat_messages_table,
    guided_operations_table,
    session_operation_fences_table,
    sessions_table,
    web_instances_table,
)
from elspeth.web.sessions.protocol import SessionArchiveDisposition, SessionGuidedOperationInProgressError
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


def _seed_completed_fork_result(
    engine: Engine,
    repository: PostgresSessionOperationRepository,
    *,
    result_session_id: UUID,
) -> None:
    parent = _create(repository, owner=f"creator-{uuid4()}")
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


def _seed_in_progress_fork(
    engine: Engine,
    *,
    parent_session_id: UUID,
    result_session_id: UUID | None = None,
) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(guided_operations_table).values(
                session_id=str(parent_session_id),
                operation_id=str(uuid4()),
                kind="session_fork",
                status="in_progress",
                request_hash="a" * 64,
                lease_token="guided-lease",
                lease_expires_at=now + timedelta(minutes=5),
                attempt=1,
                result_session_id=str(result_session_id) if result_session_id is not None else None,
                created_at=now,
                updated_at=now,
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


def test_postgres_authority_acquires_shared_sessions_advisory_lock_before_fence_row_lock(
    postgres_engine: Engine,
) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    context = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"composer-{uuid4()}",
        lease_seconds=30,
    )
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(postgres_engine, "before_cursor_execute", capture_statement)
    try:
        repository.compare_and_swap(context)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture_statement)

    advisory_index = next(index for index, statement in enumerate(statements) if "pg_catalog.pg_advisory_xact_lock" in statement)
    fence_row_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("select ") and "from session_operation_fences" in statement and "for update" in statement
    )
    assert advisory_index < fence_row_index


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

    assert taken_over.fence.operation_epoch == first.fence.operation_epoch + 1
    assert taken_over.fence.operation_id != first.fence.operation_id
    assert taken_over.fence.lease_token != first.fence.lease_token


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

    assert sum(isinstance(outcome, SessionOperationContext) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, SessionOperationConflictError) for outcome in outcomes) == 1


def test_postgres_renew_cas_and_release_are_exact(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    _seed_completed_fork_result(postgres_engine, repository, result_session_id=created.id)
    current = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id=f"progress-{uuid4()}",
        lease_seconds=30,
    )
    stale = SessionOperationFence(
        session_id=current.fence.session_id,
        operation_id=current.fence.operation_id,
        lease_token=f"stale-{uuid4()}",
        operation_epoch=current.fence.operation_epoch,
    )
    stale_context = SessionOperationContext(
        fence=stale,
        operation_kind=current.operation_kind,
    )
    with postgres_engine.connect() as conn:
        before = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        before = dict(before)

    for stale_mutation in (
        lambda: repository.compare_and_swap(stale_context),
        lambda: repository.mutate(
            stale_context,
            lambda transaction: transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC)),
        ),
        lambda: repository.renew(stale_context, lease_seconds=60),
        lambda: repository.release(stale_context),
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
    committed_at = datetime.now(UTC)

    def rollback_mutation(transaction) -> None:
        transaction.session.decide_and_soft_archive(archived_at=committed_at + timedelta(hours=1))
        raise RuntimeError("abort PostgreSQL mutation")

    with pytest.raises(RuntimeError, match="abort PostgreSQL mutation"):
        repository.mutate(current, rollback_mutation)
    with postgres_engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.archived_at).where(sessions_table.c.id == str(created.id))).scalar_one() is None

    committed = repository.mutate(
        current,
        lambda transaction: transaction.session.decide_and_soft_archive(archived_at=committed_at),
    )
    assert committed is SessionArchiveDisposition.SOFT_ARCHIVED
    with postgres_engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.archived_at).where(sessions_table.c.id == str(created.id))).scalar_one() == committed_at
    repository.release(current)


def test_postgres_archive_delete_is_atomic_update_only_no_registry(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    context = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"archive-{uuid4()}",
        lease_seconds=30,
    )
    repository.archive_delete(context)

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
        repository.archive_delete(context)
    assert exc_info.value.reason is FenceLossReason.MISSING


def test_postgres_archive_reconciliation_is_process_independent_and_stale_retry_safe(
    postgres_engine: Engine,
) -> None:
    archive_process = PostgresSessionOperationRepository(postgres_engine)
    retry_process = PostgresSessionOperationRepository(postgres_engine)
    created = _create(archive_process, owner=f"creator-{uuid4()}")
    context = archive_process.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"archive-{uuid4()}",
        lease_seconds=30,
    )

    assert retry_process.reconcile_archive_delete(context) is ArchiveDeleteReconciliation.CURRENT
    archive_process.archive_delete(context)
    assert retry_process.reconcile_archive_delete(context) is ArchiveDeleteReconciliation.CONSUMED
    assert retry_process.reconcile_archive_delete(context) is ArchiveDeleteReconciliation.CONSUMED


def test_postgres_archive_manifest_classifier_locks_before_reads_and_performs_no_dml(
    postgres_engine: Engine,
) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    with postgres_engine.connect() as conn:
        predecessor = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()
    context = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"archive-{uuid4()}",
        lease_seconds=30,
    )
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(postgres_engine, "before_cursor_execute", capture_statement)
    try:
        relation = repository.classify_archive_manifest(
            context,
            manifest_operation_id=predecessor.operation_id,
            manifest_operation_epoch=predecessor.operation_epoch,
        )
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture_statement)

    advisory_index = next(index for index, statement in enumerate(statements) if "pg_catalog.pg_advisory_xact_lock" in statement)
    fence_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("select ") and "from session_operation_fences" in statement and "for update" in statement
    )
    session_index = next(
        index for index, statement in enumerate(statements) if statement.startswith("select ") and "from sessions" in statement
    )
    database_time_index = next(index for index, statement in enumerate(statements) if "select clock_timestamp()" in statement)

    assert relation is ArchiveManifestRelation.STALE_OPERATION
    assert advisory_index < fence_index < session_index < database_time_index
    assert all(not statement.startswith(("insert ", "update ", "delete ")) for statement in statements)


def _postgres_snapshot_mutation_scope(
    engine: Engine,
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


def _postgres_message_values(*, message_id: str, session_id: str | None = None) -> dict[str, object]:
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


def _postgres_cleanup_values(*, cleanup_id: str, session_id: str | None = None) -> dict[str, object]:
    values: dict[str, object] = {
        "blob_id": cleanup_id,
        "storage_path": f"{cleanup_id}.blob",
        "created_at": datetime.now(UTC),
    }
    if session_id is not None:
        values["session_id"] = session_id
    return values


def test_postgres_fenced_mutation_auto_scopes_update_delete_and_select(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session_a = _create(repository, owner=f"creator-{uuid4()}")
    session_b = _create(repository, owner=f"creator-{uuid4()}")
    _seed_completed_fork_result(postgres_engine, repository, result_session_id=session_b.id)
    context = repository.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"composer-{uuid4()}",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id))
    before = _postgres_snapshot_mutation_scope(
        postgres_engine,
        session_ids=session_ids,
        message_ids=(),
    )

    disposition = repository.mutate(
        context,
        lambda transaction: transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC)),
    )

    assert disposition is SessionArchiveDisposition.PHYSICAL_DELETE
    assert (
        _postgres_snapshot_mutation_scope(
            postgres_engine,
            session_ids=session_ids,
            message_ids=(),
        )
        == before
    )


def test_postgres_fenced_mutation_supports_same_session_crud(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    _seed_completed_fork_result(postgres_engine, repository, result_session_id=created.id)
    context = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"composer-{uuid4()}",
        lease_seconds=30,
    )
    captured: list[object] = []
    archived_at = datetime.now(UTC)

    def soft_archive_and_capture(transaction):
        captured.extend((transaction, transaction.session, transaction.runs, transaction.blobs))
        return transaction.session.decide_and_soft_archive(archived_at=archived_at)

    disposition = repository.mutate(context, soft_archive_and_capture)

    assert disposition is SessionArchiveDisposition.SOFT_ARCHIVED
    with postgres_engine.connect() as conn:
        row = conn.execute(select(sessions_table).where(sessions_table.c.id == str(created.id))).one()
    assert row.archived_at is not None
    assert row.title == "PostgreSQL fence"
    transaction, session, runs, blobs = captured
    with pytest.raises(RuntimeError, match="closed"):
        _ = transaction.database_now  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        session.decide_and_soft_archive(archived_at=datetime.now(UTC))  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        runs.list_run_events_after(run_id=uuid4(), after_sequence=0)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        blobs.list_blob_run_links(blob_id=uuid4())  # type: ignore[attr-defined]
    private_states = []
    for capability in captured:
        state_names = [name for name in dir(capability) if name.endswith("__state")]
        assert state_names
        private_states.append(getattr(capability, state_names[0]))
    assert len({id(state) for state in private_states}) == 1
    state = private_states[0]
    assert not hasattr(state, "_connection")
    assert state._connection_token not in coordination_repository._MUTATION_CONNECTION_REGISTRY


@pytest.mark.parametrize("blocker_relation", ("own", "incoming"))
def test_postgres_archive_capability_rejects_active_guided_blockers(
    postgres_engine: Engine,
    blocker_relation: str,
) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    target = _create(repository, owner=f"creator-{uuid4()}")
    if blocker_relation == "own":
        _seed_in_progress_fork(postgres_engine, parent_session_id=target.id)
    else:
        parent = _create(repository, owner=f"creator-{uuid4()}")
        _seed_in_progress_fork(postgres_engine, parent_session_id=parent.id, result_session_id=target.id)
    context = repository.acquire(
        session_id=target.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=f"archive-{uuid4()}",
        lease_seconds=30,
    )

    with pytest.raises(SessionGuidedOperationInProgressError):
        repository.mutate(
            context,
            lambda transaction: transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC)),
        )

    repository.compare_and_swap(context)
    with postgres_engine.connect() as conn:
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
def test_postgres_fenced_mutation_refuses_unsafe_statement_and_rolls_back(
    postgres_engine: Engine,
    attack_kind: str,
) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session_a = _create(repository, owner=f"creator-{uuid4()}")
    session_b = _create(repository, owner=f"creator-{uuid4()}")
    context = repository.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"composer-{uuid4()}",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id))
    before = _postgres_snapshot_mutation_scope(
        postgres_engine,
        session_ids=session_ids,
        message_ids=(),
    )

    def prove_statement_surface_absent(transaction) -> None:
        assert attack_kind
        assert not hasattr(transaction, "execute")
        assert not hasattr(transaction.session, "execute")
        assert not hasattr(transaction.runs, "execute")
        assert not hasattr(transaction.blobs, "execute")

    repository.mutate(context, prove_statement_surface_absent)

    assert (
        _postgres_snapshot_mutation_scope(
            postgres_engine,
            session_ids=session_ids,
            message_ids=(),
        )
        == before
    )


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
def test_postgres_fenced_update_rejects_every_ownership_assignment(
    postgres_engine: Engine,
    assignment_shape: str,
) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session_a = _create(repository, owner=f"creator-{uuid4()}")
    session_b = _create(repository, owner=f"creator-{uuid4()}")
    context = repository.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"composer-{uuid4()}",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id))
    before = _postgres_snapshot_mutation_scope(
        postgres_engine,
        session_ids=session_ids,
        message_ids=(),
    )

    def prove_ownership_assignment_surface_absent(transaction) -> None:
        assert assignment_shape
        for capability in (transaction, transaction.session, transaction.runs, transaction.blobs):
            assert not hasattr(capability, "execute")
            assert not hasattr(capability, "session_id")
            assert not hasattr(capability, "update")

    repository.mutate(context, prove_ownership_assignment_surface_absent)

    assert (
        _postgres_snapshot_mutation_scope(
            postgres_engine,
            session_ids=session_ids,
            message_ids=(),
        )
        == before
    )


class _ObservedDatabaseClockRepository(PostgresSessionOperationRepository):
    """Expose when a deciding operation samples PostgreSQL time."""

    def __init__(self, engine: Engine, *, clock_sampled: Event) -> None:
        super().__init__(engine)
        self._clock_sampled = clock_sampled

    def _database_now(self, conn: Connection) -> datetime:
        value = super()._database_now(conn)
        self._clock_sampled.set()
        return value


@pytest.mark.parametrize("operation", ("mutate", "renew", "release", "archive_delete"))
def test_postgres_waiter_cannot_act_after_expiry(
    postgres_engine: Engine,
    operation: str,
) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    kind = SessionOperationKind.ARCHIVE if operation == "archive_delete" else SessionOperationKind.COMPOSE
    context = repository.acquire(
        session_id=created.id,
        operation_kind=kind,
        owner_instance_id=f"waiter-{uuid4()}",
        lease_seconds=2,
    )
    with postgres_engine.connect() as conn:
        before_fence = dict(
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        before_title = conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one()

    clock_sampled = Event()
    callback_called = Event()
    contender = _ObservedDatabaseClockRepository(postgres_engine, clock_sampled=clock_sampled)

    def act() -> None:
        if operation == "mutate":

            def mutation(transaction) -> None:
                callback_called.set()
                _ = transaction.database_now

            contender.mutate(context, mutation)
        elif operation == "renew":
            contender.renew(context, lease_seconds=30)
        elif operation == "release":
            contender.release(context)
        else:
            contender.archive_delete(context)

    blocker = postgres_engine.connect()
    blocker_transaction = blocker.begin()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        blocker.execute(
            select(session_operation_fences_table.c.session_id)
            .where(session_operation_fences_table.c.session_id == str(created.id))
            .with_for_update()
        ).one()
        outcome = pool.submit(act)
        # On the broken ordering the time read happens before the waiter
        # blocks on its UPDATE.  On the fixed ordering it remains behind
        # this row lock and samples time only after the lock is released.
        clock_sampled.wait(timeout=0.5)
        blocker.exec_driver_sql(
            "SELECT pg_sleep(GREATEST(EXTRACT(EPOCH FROM "
            "(SELECT lease_expires_at FROM session_operation_fences WHERE session_id = %s) "
            "- clock_timestamp()), 0) + 0.1)",
            (str(created.id),),
        )
        expired_before_unblock = blocker.exec_driver_sql(
            "SELECT clock_timestamp() >= lease_expires_at FROM session_operation_fences WHERE session_id = %s",
            (str(created.id),),
        ).scalar_one()
        blocker_transaction.commit()

        assert expired_before_unblock
        with pytest.raises(SessionOperationFenceLost) as exc_info:
            outcome.result(timeout=5)
        assert exc_info.value.reason is FenceLossReason.LEASE_EXPIRED
    finally:
        if blocker_transaction.is_active:
            blocker_transaction.rollback()
        blocker.close()
        pool.shutdown(wait=True)

    assert callback_called.is_set() is False
    with postgres_engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one() == before_title
        assert (
            dict(
                conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
                .one()
                ._mapping
            )
            == before_fence
        )
