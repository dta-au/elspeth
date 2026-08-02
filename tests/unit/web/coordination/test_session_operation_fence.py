"""Lifecycle and exact-CAS proofs for persistent session-operation fences."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Thread
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import event, insert, select, update
from sqlalchemy.engine import Connection, Engine, Transaction
from tests.unit.web.conftest import _make_session

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination import repository as coordination_repository
from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.sessions.models import (
    blobs_table,
    composer_completion_events_table,
    composer_inflight_requests_table,
    composer_progress_snapshots_table,
    composition_states_table,
    guided_operations_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import SessionArchiveDisposition
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@pytest.fixture
def service(engine, tmp_path) -> SessionServiceImpl:
    return SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.session-operation-fence"),
        owner_instance_id="sqlite-test-instance",
    )


@pytest.mark.asyncio
async def test_service_creation_persists_closed_create_epoch_before_return(service, engine) -> None:
    session = await service.create_session("alice", "Fenced session", "local")

    with engine.connect() as conn:
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
        ).one()

    assert isinstance(session.id, UUID)
    assert row.operation_kind == SessionOperationKind.CREATE.value
    assert row.operation_epoch == 1
    assert row.operation_id
    assert row.lease_token
    assert row.owner_instance_id == "sqlite-test-instance"
    assert row.released_at is not None
    assert row.lease_expires_at == row.released_at


@pytest.mark.asyncio
async def test_creation_inserts_session_then_fence_then_release_before_commit(service, engine) -> None:
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith(("insert into sessions ", "insert into session_operation_fences ")) or normalized.startswith(
            "update session_operation_fences "
        ):
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        await service.create_session("alice", "Ordered initialization", "local")
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert len(statements) == 3
    assert statements[0].startswith("insert into sessions ")
    assert statements[1].startswith("insert into session_operation_fences ")
    assert statements[2].startswith("update session_operation_fences ")
    assert "released_at" in statements[2]
    assert "lease_expires_at" in statements[2]
    assert "operation_kind" in statements[2]


@pytest.mark.asyncio
async def test_creation_collision_retries_whole_transaction_with_fresh_server_id(
    service,
    engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    colliding_id = uuid4()
    fresh_id = uuid4()
    with engine.begin() as conn:
        _make_session(conn, session_id=str(colliding_id), title="Existing")

    generated = iter((colliding_id, fresh_id))
    monkeypatch.setattr("elspeth.web.coordination.repository._new_session_id", lambda: next(generated))

    created = await service.create_session("alice", "Fresh", "local")

    assert created.id == fresh_id
    with engine.connect() as conn:
        sessions = conn.execute(
            select(sessions_table.c.id, sessions_table.c.title).where(sessions_table.c.id.in_((str(colliding_id), str(fresh_id))))
        ).all()
        fences = (
            conn.execute(
                select(session_operation_fences_table.c.session_id).where(
                    session_operation_fences_table.c.session_id.in_((str(colliding_id), str(fresh_id)))
                )
            )
            .scalars()
            .all()
        )
    assert sorted(sessions) == sorted([(str(colliding_id), "Existing"), (str(fresh_id), "Fresh")])
    assert fences == [str(fresh_id)]


@pytest.mark.asyncio
async def test_first_later_operation_advances_epoch_and_release_retains_row(service, engine) -> None:
    created = await service.create_session("alice", "Later operation", "local")
    authority = service.session_operation_authority

    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    assert context.operation_kind is SessionOperationKind.COMPOSE
    assert context.fence.operation_epoch == 2
    with engine.connect() as conn:
        active = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()
    assert active.operation_id == context.fence.operation_id
    assert active.lease_token == context.fence.lease_token
    assert active.operation_kind == SessionOperationKind.COMPOSE.value
    assert active.released_at is None

    authority.release(context)
    with engine.connect() as conn:
        released = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()
    assert released.operation_id == context.fence.operation_id
    assert released.lease_token == context.fence.lease_token
    assert released.operation_epoch == 2
    assert released.released_at is not None
    assert released.lease_expires_at == released.released_at


@pytest.mark.asyncio
async def test_reacquisition_after_release_is_monotonic_with_new_authority(service) -> None:
    created = await service.create_session("alice", "Monotonic", "local")
    authority = service.session_operation_authority
    first = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    authority.release(first)
    second = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    assert second.fence.operation_epoch == first.fence.operation_epoch + 1
    assert second.fence.operation_id != first.fence.operation_id
    assert second.fence.lease_token != first.fence.lease_token


@pytest.mark.asyncio
async def test_unreleased_live_operation_conflicts(service) -> None:
    created = await service.create_session("alice", "Active", "local")
    authority = service.session_operation_authority
    authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    with pytest.raises(SessionOperationConflictError):
        authority.acquire(
            session_id=created.id,
            operation_kind=SessionOperationKind.PROGRESS,
            owner_instance_id="sqlite-test-instance",
            lease_seconds=30,
        )


@pytest.mark.asyncio
async def test_stale_exact_cas_changes_zero_rows_and_error_is_leak_safe(service, engine) -> None:
    created = await service.create_session("alice", "Stale CAS", "local")
    authority = service.session_operation_authority
    current = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    stale = SessionOperationFence(
        session_id=current.fence.session_id,
        operation_id=current.fence.operation_id,
        lease_token="stale-random-authority-token",
        operation_epoch=current.fence.operation_epoch,
    )
    stale_context = SessionOperationContext(
        fence=stale,
        operation_kind=current.operation_kind,
    )
    with engine.connect() as conn:
        before = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        before = dict(before)

    with pytest.raises(SessionOperationFenceLost) as exc_info:
        authority.compare_and_swap(stale_context)

    with engine.connect() as conn:
        after = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        after = dict(after)
    assert after == before
    assert exc_info.value.reason is FenceLossReason.TOKEN_MISMATCH
    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert current.fence.session_id not in rendered
    assert current.fence.operation_id not in rendered
    assert current.fence.lease_token not in rendered


@pytest.mark.asyncio
async def test_fenced_mutation_cas_and_state_write_share_commit_or_rollback(service, engine) -> None:
    created = await service.create_session("alice", "Before", "local")
    fork_parent = await service.create_session("alice", "Fork parent", "local")
    completed_at = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(guided_operations_table).values(
                session_id=str(fork_parent.id),
                operation_id=str(uuid4()),
                kind="session_fork",
                status="completed",
                request_hash="a" * 64,
                lease_token=None,
                lease_expires_at=None,
                attempt=1,
                result_kind="session",
                result_session_id=str(created.id),
                response_hash="b" * 64,
                created_at=completed_at,
                updated_at=completed_at,
                settled_at=completed_at,
            )
        )
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    with engine.connect() as conn:
        before_rollback_fence = dict(
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )

    def rollback_mutation(transaction) -> None:
        transaction.session.decide_and_soft_archive(archived_at=completed_at + timedelta(hours=1))
        raise RuntimeError("abort representative mutation")

    with pytest.raises(RuntimeError, match="abort representative mutation"):
        authority.mutate(context, rollback_mutation)

    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.archived_at).where(sessions_table.c.id == str(created.id))).scalar_one() is None
        after_rollback_fence = dict(
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
    assert after_rollback_fence == before_rollback_fence
    authority.compare_and_swap(context)

    observed: list[tuple[int, str]] = []

    def capture_statement(conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update session_operation_fences ") or normalized.startswith("update sessions "):
            observed.append((id(conn), normalized))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        result = authority.mutate(
            context,
            lambda transaction: transaction.session.decide_and_soft_archive(archived_at=completed_at),
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert result is SessionArchiveDisposition.SOFT_ARCHIVED
    assert [statement.split()[1] for _, statement in observed] == ["session_operation_fences", "sessions"]
    assert len({connection_id for connection_id, _ in observed}) == 1

    with engine.connect() as conn:
        committed_archived_at = conn.execute(
            select(sessions_table.c.archived_at).where(sessions_table.c.id == str(created.id))
        ).scalar_one()
    assert committed_archived_at.replace(tzinfo=UTC) == completed_at


@pytest.mark.asyncio
async def test_stale_fenced_mutation_never_invokes_callback(service, engine) -> None:
    created = await service.create_session("alice", "Unchanged", "local")
    authority = service.session_operation_authority
    current = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    stale = SessionOperationFence(
        session_id=current.fence.session_id,
        operation_id=current.fence.operation_id,
        lease_token="stale-mutation-token",
        operation_epoch=current.fence.operation_epoch,
    )
    stale_context = SessionOperationContext(
        fence=stale,
        operation_kind=current.operation_kind,
    )
    callback_called = False

    def mutation(transaction) -> None:
        nonlocal callback_called
        callback_called = True
        transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC))

    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(stale_context, mutation)

    assert callback_called is False
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one() == "Unchanged"


def _callback_instance_graph(root: object) -> tuple[object, ...]:
    """Traverse callback-owned instances without following types or module globals."""
    seen: set[int] = set()
    pending = [root]
    values: list[object] = []
    scalar_types = (str, bytes, int, float, bool, type(None), datetime, UUID, type)
    while pending:
        value = pending.pop()
        if id(value) in seen or isinstance(value, scalar_types):
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


@pytest.mark.asyncio
async def test_active_mutation_object_graph_has_no_database_handle_or_cross_session_escape(service, engine) -> None:
    owned = await service.create_session("alice", "Owned", "local")
    foreign = await service.create_session("alice", "Foreign", "local")
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=owned.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    captured_token: list[str] = []

    def inspect_active_graph(transaction) -> None:
        capabilities = (
            transaction,
            transaction.session,
            transaction.composition_states,
            transaction.runs,
            transaction.blobs,
            transaction.composer_progress,
            transaction.composer_completion,
        )
        reachable = tuple(value for capability in capabilities for value in _callback_instance_graph(capability))
        assert not any(isinstance(value, (Connection, Engine, Transaction)) for value in reachable)

        state = object.__getattribute__(transaction, "_RepositoryMutationTransaction__state")
        assert not hasattr(state, "_active_connection")
        assert not hasattr(transaction.composer_progress, "_connection")
        captured_token.append(object.__getattribute__(state, "_connection_token"))
        with pytest.raises(AttributeError):
            leaked = object.__getattribute__(state, "_connection")
            leaked.execute(update(sessions_table).where(sessions_table.c.id == str(foreign.id)).values(title="Escaped"))

    authority.mutate(context, inspect_active_graph)

    assert captured_token[0] not in coordination_repository._MUTATION_CONNECTION_REGISTRY
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(foreign.id))).scalar_one() == "Foreign"


@pytest.mark.asyncio
async def test_every_mutation_capability_is_confined_to_the_callback_thread(service) -> None:
    created = await service.create_session("alice", "Thread-confined UoW", "local")
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    def probe_every_capability(transaction) -> None:
        actions = {
            "outer": lambda: transaction.database_now,
            "session": lambda: transaction.session.decide_and_soft_archive(archived_at=datetime.now(UTC)),
            "composition_states": lambda: transaction.composition_states.append_state(object()),
            "runs": lambda: transaction.runs.list_run_events_after(run_id=uuid4(), after_sequence=0),
            "blobs": lambda: transaction.blobs.list_blob_run_links(blob_id=uuid4()),
            "composer_progress": transaction.composer_progress.retire_session_progress,
            "composer_completion": lambda: transaction.composer_completion.record_yaml_export(
                composition_state_id=uuid4(),
                actor="alice",
                created_at=datetime.now(UTC),
            ),
        }
        errors: dict[str, BaseException] = {}

        def invoke(label: str, action) -> None:
            try:
                action()
            except BaseException as error:
                errors[label] = error

        for label, action in actions.items():
            worker = Thread(target=invoke, args=(label, action))
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()

        assert set(errors) == set(actions)
        for error in errors.values():
            assert isinstance(error, RuntimeError)
            assert "owning callback thread" in str(error)

    authority.mutate(context, probe_every_capability)
    retained = await service.get_session(created.id)
    assert retained.archived_at is None


def test_mutation_transaction_constructor_failure_does_not_leak_registry_entry(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    before = set(coordination_repository._MUTATION_CONNECTION_REGISTRY)

    def fail_facet_initialization(_state) -> None:
        raise RuntimeError("facet initialization failed")

    monkeypatch.setattr(coordination_repository, "_RepositorySessionMutations", fail_facet_initialization)
    with engine.begin() as conn, pytest.raises(RuntimeError, match="facet initialization failed"):
        coordination_repository._RepositoryMutationTransaction(
            conn,
            session_id=str(uuid4()),
            database_now=datetime.now(UTC),
        )

    assert set(coordination_repository._MUTATION_CONNECTION_REGISTRY) == before


def test_blob_reservation_facets_are_kind_and_operation_scoped(service, engine, tmp_path) -> None:
    authority = service.session_operation_authority
    created = authority.create_session_with_initial_fence(
        user_id="alice",
        title="Blob facet authority",
        auth_provider_type="local",
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    blob_id = uuid4()
    pending = BlobRecord(
        id=blob_id,
        session_id=created.id,
        filename="authority.txt",
        mime_type="text/plain",
        size_bytes=4,
        content_hash="0" * 64,
        storage_path=str(tmp_path / "authority.txt"),
        created_at=datetime.now(UTC),
        created_by="user",
        source_description=None,
        status="pending",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )
    read_context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    with pytest.raises(AuditIntegrityError, match="operation kind"):
        authority.mutate(
            read_context,
            lambda transaction: transaction.blobs.reserve_blob(
                record=pending,
                max_storage_per_session=1024,
                idempotent=False,
                guided_operation_write_fence=None,
            ),
        )
    authority.release(read_context)
    with engine.connect() as conn:
        assert conn.execute(select(blobs_table.c.id).where(blobs_table.c.id == str(blob_id))).one_or_none() is None

    first = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    assert authority.mutate(
        first,
        lambda transaction: transaction.blobs.reserve_blob(
            record=pending,
            max_storage_per_session=1024,
            idempotent=False,
            guided_operation_write_fence=None,
        ),
    )
    authority.release(first)
    second = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    with pytest.raises(AuditIntegrityError, match="pending reservation"):
        authority.mutate(
            second,
            lambda transaction: transaction.blobs.mark_blob_ready(
                blob_id=blob_id,
                guided_operation_write_fence=None,
            ),
        )
    assert (
        authority.mutate(
            second,
            lambda transaction: transaction.blobs.discard_pending_blob(
                blob_id=blob_id,
                guided_operation_write_fence=None,
            ),
        )
        is False
    )
    obligations = authority.mutate(second, lambda transaction: transaction.blobs.list_abandoned_blob_reservations())
    assert len(obligations) == 1
    assert authority.mutate(
        second,
        lambda transaction: transaction.blobs.retire_abandoned_blob_reservation(obligation=obligations[0]),
    )
    authority.release(second)


def test_archived_session_refuses_acquire_and_existing_uow(service, engine) -> None:
    authority = service.session_operation_authority
    created = authority.create_session_with_initial_fence(
        user_id="alice",
        title="Archived authority",
        auth_provider_type="local",
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(created.id)).values(archived_at=datetime.now(UTC)))
    with pytest.raises(SessionOperationFenceLost) as existing:
        authority.mutate(context, lambda _transaction: None)
    assert existing.value.reason is FenceLossReason.OWNER_INACTIVE
    authority.release(context)
    with pytest.raises(SessionOperationFenceLost) as later:
        authority.acquire(
            session_id=created.id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id="later-owner",
            lease_seconds=30,
        )
    assert later.value.reason is FenceLossReason.OWNER_INACTIVE


def test_fork_transaction_constructor_failure_does_not_leak_registry_entry(engine) -> None:
    before = set(coordination_repository._MUTATION_CONNECTION_REGISTRY)
    with engine.begin() as conn, pytest.raises(TypeError):
        coordination_repository._ForkCreationTransaction(
            conn,
            parent_session_id=str(uuid4()),
            child_session_id=str(uuid4()),
            guided_operation=object(),  # type: ignore[arg-type]
            database_now=datetime.now(UTC),
            child_created=True,
        )

    assert set(coordination_repository._MUTATION_CONNECTION_REGISTRY) == before


@pytest.mark.asyncio
async def test_mutation_facade_exposes_no_handle_and_closes_after_callback(service) -> None:
    created = await service.create_session("alice", "Bounded facade", "local")
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    captured: list[object] = []

    def capture_all_capabilities(transaction) -> None:
        captured.extend(
            (
                transaction,
                transaction.session,
                transaction.composition_states,
                transaction.runs,
                transaction.blobs,
                transaction.composer_progress,
                transaction.composer_completion,
            )
        )

    authority.mutate(context, capture_all_capabilities)

    transaction, session, composition_states, runs, blobs, composer_progress, composer_completion = captured
    assert {name for name in dir(transaction) if not name.startswith("_")} == {
        "blobs",
        "composer_completion",
        "composer_progress",
        "composition_states",
        "database_now",
        "runs",
        "session",
    }
    with pytest.raises(RuntimeError, match="closed"):
        _ = transaction.database_now  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        session.decide_and_soft_archive(archived_at=datetime.now(UTC))  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        composition_states.append_state(object())  # type: ignore[attr-defined, arg-type]
    with pytest.raises(RuntimeError, match="closed"):
        runs.list_run_events_after(run_id=uuid4(), after_sequence=0)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        blobs.list_blob_run_links(blob_id=uuid4())  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        composer_progress.retire_session_progress()  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        composer_completion.record_yaml_export(  # type: ignore[attr-defined]
            composition_state_id=uuid4(),
            actor="alice",
            created_at=datetime.now(UTC),
        )

    private_states = []
    for capability in captured:
        state_names = [name for name in dir(capability) if name.endswith("__state")]
        assert state_names
        private_states.append(getattr(capability, state_names[0]))
    assert len({id(state) for state in private_states}) == 1
    state = private_states[0]
    assert not hasattr(state, "_connection")
    assert state._connection_token not in coordination_repository._MUTATION_CONNECTION_REGISTRY


@pytest.mark.asyncio
async def test_composer_progress_facet_rejects_detached_thread_before_any_dml(service, engine) -> None:
    """A callback cannot return between paired progress-table mutations."""
    created = await service.create_session("alice", "Thread-bound progress", "local")
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(composer_inflight_requests_table).values(
                request_id="thread-bound-request",
                session_id=str(created.id),
                user_id="alice",
                operation_id=str(uuid4()),
                operation_epoch=2,
                started_at=now,
                updated_at=now,
                completed_at=None,
                expires_at=now + timedelta(days=1),
            )
        )
        connection.execute(
            insert(composer_progress_snapshots_table).values(
                session_id=str(created.id),
                request_id="thread-bound-request",
                user_id="alice",
                phase="starting",
                headline="Starting",
                evidence=[],
                likely_next=None,
                reason=None,
                operation_id=str(uuid4()),
                operation_epoch=2,
                updated_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
    authority = service.session_operation_authority
    archive = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    worker_errors: list[BaseException] = []

    def attempt_retirement(facet) -> None:
        try:
            facet.retire_session_progress()
        except BaseException as error:
            worker_errors.append(error)

    def detach_worker(transaction) -> None:
        worker = Thread(target=attempt_retirement, args=(transaction.composer_progress,))
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()

    authority.mutate(archive, detach_worker)

    assert len(worker_errors) == 1
    assert isinstance(worker_errors[0], RuntimeError)
    assert "owning callback thread" in str(worker_errors[0])
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(composer_inflight_requests_table.c.request_id).where(
                    composer_inflight_requests_table.c.session_id == str(created.id)
                )
            ).scalar_one()
            == "thread-bound-request"
        )
        assert (
            connection.execute(
                select(composer_progress_snapshots_table.c.request_id).where(
                    composer_progress_snapshots_table.c.session_id == str(created.id)
                )
            ).scalar_one()
            == "thread-bound-request"
        )


@pytest.mark.asyncio
async def test_mutation_facade_cannot_rewrite_its_own_authority_row(service) -> None:
    created = await service.create_session("alice", "Protected authority", "local")
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    def assert_no_authority_rewrite_surface(transaction) -> None:
        assert not hasattr(transaction, "execute")
        assert not hasattr(transaction, "session_id")
        assert not hasattr(transaction.session, "execute")
        assert not hasattr(transaction.session, "update")

    authority.mutate(context, assert_no_authority_rewrite_surface)

    authority.compare_and_swap(context)


@pytest.mark.asyncio
async def test_stale_renew_and_release_preserve_current_authority(service, engine) -> None:
    created = await service.create_session("alice", "Stale renew", "local")
    authority = service.session_operation_authority
    current = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    stale = SessionOperationFence(
        session_id=current.fence.session_id,
        operation_id=str(uuid4()),
        lease_token=current.fence.lease_token,
        operation_epoch=current.fence.operation_epoch,
    )
    stale_context = SessionOperationContext(
        fence=stale,
        operation_kind=current.operation_kind,
    )
    with engine.connect() as conn:
        before = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        before = dict(before)

    with pytest.raises(SessionOperationFenceLost):
        authority.renew(stale_context, lease_seconds=60)
    with pytest.raises(SessionOperationFenceLost):
        authority.release(stale_context)

    with engine.connect() as conn:
        after = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        after = dict(after)
    assert after == before


@pytest.mark.asyncio
async def test_current_archive_fence_deletes_parent_and_fence_by_cascade(service, engine) -> None:
    created = await service.create_session("alice", "Physical archive", "local")
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    authority.archive_delete(context)

    with engine.connect() as conn:
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
        authority.compare_and_swap(context)
    assert exc_info.value.reason is FenceLossReason.MISSING


def test_creation_api_has_no_caller_selectable_authority(service) -> None:
    import inspect

    parameters = inspect.signature(service.session_operation_authority.create_session_with_initial_fence).parameters
    assert "session_id" not in parameters
    assert "operation_id" not in parameters
    assert "lease_token" not in parameters
    assert "connection" not in parameters
    assert "engine" not in parameters


@pytest.mark.asyncio
async def test_composer_completion_mutations_write_fixed_shapes_under_exact_blob_read(service, engine) -> None:
    created = await service.create_session("alice", "Completion authority", "local")
    state_id = uuid4()
    created_at = datetime.now(UTC)
    expires_at = created_at + timedelta(hours=1)
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id),
                session_id=str(created.id),
                version=1,
                source=None,
                sources=None,
                nodes=[],
                edges=[],
                outputs=[],
                metadata_={"name": "Completion authority", "description": ""},
                is_valid=True,
                validation_errors=None,
                composer_meta=None,
                created_at=created_at,
                derived_from_state_id=None,
                provenance="session_seed",
            )
        )
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    authority.mutate(
        context,
        lambda transaction: transaction.composer_completion.mark_ready_for_review(
            composition_state_id=state_id,
            actor="alice",
            created_at=created_at,
            payload_digest="sha256:" + "a" * 64,
            expires_at=expires_at,
        ),
    )
    authority.mutate(
        context,
        lambda transaction: transaction.composer_completion.record_yaml_export(
            composition_state_id=state_id,
            actor="alice",
            created_at=created_at,
        ),
    )

    with engine.connect() as conn:
        rows = conn.execute(select(composer_completion_events_table).order_by(composer_completion_events_table.c.event_type)).all()
    assert len(rows) == 2
    export, ready = rows
    assert UUID(export.id)
    assert export.session_id == str(created.id)
    assert export.composition_state_id == str(state_id)
    assert export.event_type == "export_yaml"
    assert export.actor == "alice"
    assert export.created_at.replace(tzinfo=UTC) == created_at
    assert export.payload_digest is None
    assert export.expires_at is None
    assert UUID(ready.id)
    assert ready.id != export.id
    assert ready.session_id == str(created.id)
    assert ready.composition_state_id == str(state_id)
    assert ready.event_type == "mark_ready_for_review"
    assert ready.actor == "alice"
    assert ready.created_at.replace(tzinfo=UTC) == created_at
    assert ready.payload_digest == "sha256:" + "a" * 64
    assert ready.expires_at.replace(tzinfo=UTC) == expires_at


@pytest.mark.asyncio
async def test_composer_completion_mutations_enforce_kind_session_and_latest_state(service, engine) -> None:
    owned = await service.create_session("alice", "Owned completion", "local")
    foreign = await service.create_session("alice", "Foreign completion", "local")
    old_state_id = uuid4()
    current_state_id = uuid4()
    foreign_state_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for state_id, session_id, version in (
            (old_state_id, owned.id, 1),
            (current_state_id, owned.id, 2),
            (foreign_state_id, foreign.id, 1),
        ):
            conn.execute(
                insert(composition_states_table).values(
                    id=str(state_id),
                    session_id=str(session_id),
                    version=version,
                    source=None,
                    sources=None,
                    nodes=[],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "State", "description": ""},
                    is_valid=True,
                    validation_errors=None,
                    composer_meta=None,
                    created_at=now,
                    derived_from_state_id=None,
                    provenance="session_seed",
                )
            )
    authority = service.session_operation_authority
    wrong_kind = authority.acquire(
        session_id=owned.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    with pytest.raises(AuditIntegrityError, match="operation kind"):
        authority.mutate(
            wrong_kind,
            lambda transaction: transaction.composer_completion.record_yaml_export(
                composition_state_id=current_state_id,
                actor="alice",
                created_at=now,
            ),
        )
    authority.release(wrong_kind)

    context = authority.acquire(
        session_id=owned.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    with pytest.raises(coordination_repository.SessionDerivedCustodyError):
        authority.mutate(
            context,
            lambda transaction: transaction.composer_completion.record_yaml_export(
                composition_state_id=foreign_state_id,
                actor="alice",
                created_at=now,
            ),
        )
    with pytest.raises(coordination_repository.SessionDerivedCustodyError):
        authority.mutate(
            context,
            lambda transaction: transaction.composer_completion.mark_ready_for_review(
                composition_state_id=old_state_id,
                actor="alice",
                created_at=now,
                payload_digest="sha256:" + "b" * 64,
                expires_at=now + timedelta(hours=1),
            ),
        )
    authority.mutate(
        context,
        lambda transaction: transaction.composer_completion.record_yaml_export(
            composition_state_id=old_state_id,
            actor="alice",
            created_at=now,
        ),
    )
    with engine.connect() as conn:
        rows = conn.execute(select(composer_completion_events_table)).all()
    assert len(rows) == 1
    assert rows[0].composition_state_id == str(old_state_id)
    assert rows[0].event_type == "export_yaml"


@pytest.mark.asyncio
async def test_composer_completion_released_authority_writes_zero_and_successor_writes_once(service, engine) -> None:
    created = await service.create_session("alice", "Completion takeover", "local")
    state_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id),
                session_id=str(created.id),
                version=1,
                source=None,
                sources=None,
                nodes=[],
                edges=[],
                outputs=[],
                metadata_={"name": "Completion takeover", "description": ""},
                is_valid=True,
                validation_errors=None,
                composer_meta=None,
                created_at=now,
                derived_from_state_id=None,
                provenance="session_seed",
            )
        )
    authority = service.session_operation_authority
    first = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    authority.release(first)
    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(
            first,
            lambda transaction: transaction.composer_completion.record_yaml_export(
                composition_state_id=state_id,
                actor="alice",
                created_at=now,
            ),
        )
    successor = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(
            first,
            lambda transaction: transaction.composer_completion.record_yaml_export(
                composition_state_id=state_id,
                actor="alice",
                created_at=now,
            ),
        )
    authority.mutate(
        successor,
        lambda transaction: transaction.composer_completion.record_yaml_export(
            composition_state_id=state_id,
            actor="alice",
            created_at=now,
        ),
    )
    with engine.connect() as conn:
        rows = conn.execute(select(composer_completion_events_table)).all()
    assert len(rows) == 1
    assert rows[0].composition_state_id == str(state_id)
