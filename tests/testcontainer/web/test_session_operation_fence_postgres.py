"""Real-PostgreSQL proofs for persistent session operation authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, MetaData, Table, column, delete, insert, literal, select, table, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.repository import PostgresSessionOperationRepository, SessionOperationConflictError
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    chat_messages_table,
    session_operation_fences_table,
    sessions_table,
    web_instances_table,
)
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
    cleanup_b = str(uuid4())
    with postgres_engine.begin() as conn:
        conn.execute(
            insert(blob_deletion_cleanups_table).values(**_postgres_cleanup_values(cleanup_id=cleanup_b, session_id=str(session_b.id)))
        )
    fence = repository.acquire(
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
        cleanup_ids=(cleanup_b,),
    )

    def attempt_cross_session_access(transaction):
        changed = transaction.execute(
            update(sessions_table).where(sessions_table.c.id == str(session_b.id)).values(title="cross-session update")
        )
        removed = transaction.execute(delete(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == cleanup_b))
        visible = transaction.execute(select(sessions_table.c.id).order_by(sessions_table.c.id))
        return changed, removed, visible

    changed, removed, visible = repository.mutate(fence, attempt_cross_session_access)

    assert changed.rowcount == 0
    assert removed.rowcount == 0
    assert [row["id"] for row in visible.rows] == [str(session_a.id)]
    assert (
        _postgres_snapshot_mutation_scope(
            postgres_engine,
            session_ids=session_ids,
            message_ids=(),
            cleanup_ids=(cleanup_b,),
        )
        == before
    )


def test_postgres_fenced_mutation_supports_same_session_crud(postgres_engine: Engine) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    created = _create(repository, owner=f"creator-{uuid4()}")
    fence = repository.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"composer-{uuid4()}",
        lease_seconds=30,
    )
    cleanup_id = str(uuid4())

    def same_session_crud(transaction):
        changed = transaction.execute(update(sessions_table).values(title="same-session update"))
        inserted = transaction.execute(insert(blob_deletion_cleanups_table).values(**_postgres_cleanup_values(cleanup_id=cleanup_id)))
        visible = transaction.execute(select(blob_deletion_cleanups_table.c.blob_id, blob_deletion_cleanups_table.c.session_id))
        removed = transaction.execute(delete(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == cleanup_id))
        return changed, inserted, visible, removed

    changed, inserted, visible, removed = repository.mutate(fence, same_session_crud)

    assert changed.rowcount == 1
    assert inserted.rowcount in {-1, 1}
    assert visible.rows == ({"blob_id": cleanup_id, "session_id": str(created.id)},)
    assert removed.rowcount == 1
    with postgres_engine.connect() as conn:
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
def test_postgres_fenced_mutation_refuses_unsafe_statement_and_rolls_back(
    postgres_engine: Engine,
    attack_kind: str,
) -> None:
    repository = PostgresSessionOperationRepository(postgres_engine)
    session_a = _create(repository, owner=f"creator-{uuid4()}")
    session_b = _create(repository, owner=f"creator-{uuid4()}")
    selected_parent_id = str(uuid4())
    selected_message_id = str(uuid4())
    second_selected_message_id = str(uuid4())
    fence = repository.acquire(
        session_id=session_a.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=f"composer-{uuid4()}",
        lease_seconds=30,
    )
    session_ids = (str(session_a.id), str(session_b.id), selected_parent_id)
    selected_message_ids = (selected_message_id, second_selected_message_id)
    before = _postgres_snapshot_mutation_scope(
        postgres_engine,
        session_ids=session_ids,
        message_ids=selected_message_ids,
    )

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
        reflected_fences = Table("session_operation_fences", MetaData(), autoload_with=postgres_engine)
        statement = (
            update(reflected_fences)
            .where(reflected_fences.c.session_id == str(session_a.id))
            .values(operation_epoch=fence.operation_epoch + 100)
        )
    elif attack_kind == "reflected_sessions_update":
        reflected_sessions = Table("sessions", MetaData(), autoload_with=postgres_engine)
        statement = (
            update(reflected_sessions).where(reflected_sessions.c.id == str(session_b.id)).values(title="reflected cross-session update")
        )
    elif attack_kind == "mismatched_child_insert":
        statement = insert(chat_messages_table).values(
            **_postgres_message_values(message_id=selected_message_id, session_id=str(session_b.id))
        )
    elif attack_kind == "multirow_child_insert":
        statement = insert(chat_messages_table).values(
            [
                _postgres_message_values(message_id=selected_message_id, session_id=str(session_b.id)),
                _postgres_message_values(message_id=second_selected_message_id, session_id=str(session_b.id)),
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
        reflected_cleanup = Table("sessions_cleanup_claims", MetaData(), autoload_with=postgres_engine)
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
        repository.mutate(fence, mutate_then_attack)
    except ValueError:
        refused = True

    assert (
        _postgres_snapshot_mutation_scope(
            postgres_engine,
            session_ids=session_ids,
            message_ids=selected_message_ids,
        )
        == before
    )
    assert refused is True


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
    cleanup_id = str(uuid4())
    with postgres_engine.begin() as conn:
        conn.execute(
            insert(blob_deletion_cleanups_table).values(**_postgres_cleanup_values(cleanup_id=cleanup_id, session_id=str(session_a.id)))
        )
    fence = repository.acquire(
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
        cleanup_ids=(cleanup_id,),
    )

    if assignment_shape == "child_values_string":
        statement = update(blob_deletion_cleanups_table).values(session_id=str(session_b.id))
    elif assignment_shape == "child_values_column_unchanged":
        statement = update(blob_deletion_cleanups_table).values({blob_deletion_cleanups_table.c.session_id: str(session_a.id)})
    elif assignment_shape == "child_ordered_string":
        statement = update(blob_deletion_cleanups_table).ordered_values(("session_id", str(session_b.id)))
    elif assignment_shape == "child_ordered_column":
        statement = update(blob_deletion_cleanups_table).ordered_values((blob_deletion_cleanups_table.c.session_id, str(session_b.id)))
    elif assignment_shape == "parent_values_id_unchanged":
        statement = update(sessions_table).values(id=str(session_a.id))
    elif assignment_shape == "parent_ordered_id_unchanged":
        statement = update(sessions_table).ordered_values(("id", str(session_a.id)))
    else:
        statement = (
            postgresql_insert(blob_deletion_cleanups_table)
            .values(**_postgres_cleanup_values(cleanup_id=cleanup_id, session_id=str(session_a.id)))
            .on_conflict_do_update(
                index_elements=(blob_deletion_cleanups_table.c.blob_id,),
                set_={"session_id": str(session_b.id)},
            )
        )

    refused = False

    def mutate_then_reassign(transaction) -> None:
        transaction.execute(update(sessions_table).values(title="must roll back ownership reassignment"))
        transaction.execute(statement)

    try:
        repository.mutate(fence, mutate_then_reassign)
    except ValueError:
        refused = True

    assert (
        _postgres_snapshot_mutation_scope(
            postgres_engine,
            session_ids=session_ids,
            message_ids=(),
            cleanup_ids=(cleanup_id,),
        )
        == before
    )
    assert refused is True
