"""Lifecycle and exact-CAS proofs for persistent session-operation fences."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import event, select, update
from tests.unit.web.conftest import _make_session

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.sessions.models import session_operation_fences_table, sessions_table
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

    fence = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    assert fence.operation_epoch == 2
    with engine.connect() as conn:
        active = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()
    assert active.operation_id == fence.operation_id
    assert active.lease_token == fence.lease_token
    assert active.operation_kind == SessionOperationKind.COMPOSE.value
    assert active.released_at is None

    authority.release(fence)
    with engine.connect() as conn:
        released = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id))
        ).one()
    assert released.operation_id == fence.operation_id
    assert released.lease_token == fence.lease_token
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

    assert second.operation_epoch == first.operation_epoch + 1
    assert second.operation_id != first.operation_id
    assert second.lease_token != first.lease_token


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
        session_id=current.session_id,
        operation_id=current.operation_id,
        lease_token="stale-random-authority-token",
        operation_epoch=current.operation_epoch,
    )
    with engine.connect() as conn:
        before = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        before = dict(before)

    with pytest.raises(SessionOperationFenceLost) as exc_info:
        authority.compare_and_swap(stale)

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
    assert current.session_id not in rendered
    assert current.operation_id not in rendered
    assert current.lease_token not in rendered


@pytest.mark.asyncio
async def test_fenced_mutation_cas_and_state_write_share_commit_or_rollback(service, engine) -> None:
    created = await service.create_session("alice", "Before", "local")
    authority = service.session_operation_authority
    fence = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    observed: list[tuple[int, str]] = []

    def capture_statement(conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update session_operation_fences ") or normalized.startswith("update sessions "):
            observed.append((id(conn), normalized))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        result = authority.mutate(
            fence,
            lambda transaction: transaction.execute(
                update(sessions_table).where(sessions_table.c.id == str(created.id)).values(title="Committed")
            ),
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert result.rowcount == 1
    assert [statement.split()[1] for _, statement in observed] == ["session_operation_fences", "sessions"]
    assert len({connection_id for connection_id, _ in observed}) == 1

    def rollback_mutation(transaction) -> None:
        transaction.execute(update(sessions_table).where(sessions_table.c.id == str(created.id)).values(title="Must roll back"))
        raise RuntimeError("abort representative mutation")

    with pytest.raises(RuntimeError, match="abort representative mutation"):
        authority.mutate(fence, rollback_mutation)

    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one() == "Committed"
    authority.compare_and_swap(fence)


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
        session_id=current.session_id,
        operation_id=current.operation_id,
        lease_token="stale-mutation-token",
        operation_epoch=current.operation_epoch,
    )
    callback_called = False

    def mutation(transaction) -> None:
        nonlocal callback_called
        callback_called = True
        transaction.execute(update(sessions_table).where(sessions_table.c.id == str(created.id)).values(title="Forbidden"))

    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(stale, mutation)

    assert callback_called is False
    with engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(created.id))).scalar_one() == "Unchanged"


@pytest.mark.asyncio
async def test_mutation_facade_exposes_no_handle_and_closes_after_callback(service) -> None:
    created = await service.create_session("alice", "Bounded facade", "local")
    authority = service.session_operation_authority
    fence = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.PROGRESS,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )
    captured: list[object] = []
    authority.mutate(fence, lambda transaction: captured.append(transaction))

    transaction = captured[0]
    assert {name for name in dir(transaction) if not name.startswith("_")} == {"execute"}
    with pytest.raises(RuntimeError, match="closed"):
        transaction.execute(select(sessions_table.c.id))  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mutation_facade_cannot_rewrite_its_own_authority_row(service) -> None:
    created = await service.create_session("alice", "Protected authority", "local")
    authority = service.session_operation_authority
    fence = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    with pytest.raises(ValueError, match="authority tables"):
        authority.mutate(
            fence,
            lambda transaction: transaction.execute(
                update(session_operation_fences_table)
                .where(session_operation_fences_table.c.session_id == str(created.id))
                .values(lease_token="forged-token")
            ),
        )

    authority.compare_and_swap(fence)


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
        session_id=current.session_id,
        operation_id=str(uuid4()),
        lease_token=current.lease_token,
        operation_epoch=current.operation_epoch,
    )
    with engine.connect() as conn:
        before = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(created.id)))
            .one()
            ._mapping
        )
        before = dict(before)

    with pytest.raises(SessionOperationFenceLost):
        authority.renew(stale, lease_seconds=60)
    with pytest.raises(SessionOperationFenceLost):
        authority.release(stale)

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
    fence = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id="sqlite-test-instance",
        lease_seconds=30,
    )

    authority.archive_delete(fence)

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
        authority.compare_and_swap(fence)
    assert exc_info.value.reason is FenceLossReason.MISSING


def test_creation_api_has_no_caller_selectable_authority(service) -> None:
    import inspect

    parameters = inspect.signature(service.session_operation_authority.create_session_with_initial_fence).parameters
    assert "session_id" not in parameters
    assert "operation_id" not in parameters
    assert "lease_token" not in parameters
    assert "connection" not in parameters
    assert "engine" not in parameters
