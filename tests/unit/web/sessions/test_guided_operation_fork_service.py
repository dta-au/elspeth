"""Fenced session-fork staging, takeover, settlement, and archive exclusion."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.blobs.protocol import BlobForkWriteFence, BlobInProgressForkError, fork_blob_id
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    guided_operation_events_table,
    guided_operations_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    CompositionStateProvenance,
    CompositionStateRecord,
    GuidedForkSettlementCommand,
    GuidedOperationActive,
    GuidedOperationClaimed,
    GuidedOperationCompleted,
    GuidedOperationFailed,
    GuidedOperationFenceLostError,
    GuidedOperationTakenOver,
    GuidedSessionResult,
    SessionForkParentAuthority,
)
from elspeth.web.sessions.routes.sessions import _rewrite_fork_state_blob_custody
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import (
    SessionServiceImpl,
    _fork_blob_plan_from_content,
    _GuidedSessionMutations,
    _value_references_parent_blob,
)
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness
from tests.unit.web.sessions.test_fork import _complete_guided_start_authority, _make_fork_app


@pytest.fixture()
def engine():
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    return engine


@pytest.fixture()
def service(engine) -> SessionServiceImpl:
    return DualFencedSessionServiceHarness(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )


@pytest.fixture(params=("sqlite", "postgres"))
def durable_engine(request: pytest.FixtureRequest, tmp_path: Path):
    """Exercise lock races against production-shaped file SQLite and opt-in PG."""

    if request.param == "postgres":
        url = os.environ.get("ELSPETH_TEST_POSTGRES_URL")
        if url is None:
            pytest.skip("ELSPETH_TEST_POSTGRES_URL is required for the PostgreSQL fork race matrix")
        race_engine = create_session_engine(url)
    else:
        race_engine = create_session_engine(f"sqlite:///{tmp_path / 'fork-races.db'}")
    initialize_session_schema(race_engine)
    try:
        yield race_engine
    finally:
        race_engine.dispose()


def _service_for(engine: Any) -> SessionServiceImpl:
    return DualFencedSessionServiceHarness(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.fork-race"),
    )


async def _create_test_blob(
    session_service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
    session_id: UUID,
    filename: str,
    content: bytes,
    mime_type: str,
):
    context = await session_service._run_sync(
        lambda: session_service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
    )
    try:
        return await blob_service.create_blob(
            session_id,
            filename,
            content,
            mime_type,
        )
    finally:
        await session_service._run_sync(session_service.session_operation_authority.release, context)


async def _delete_test_blob(
    session_service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
    session_id: UUID,
    blob_id: UUID,
) -> None:
    context = await session_service._run_sync(
        lambda: session_service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
    )
    try:
        await blob_service.delete_blob(blob_id)
    finally:
        await session_service._run_sync(session_service.session_operation_authority.release, context)


async def _save_composition_state(
    service: SessionServiceImpl,
    session_id: UUID,
    state: CompositionStateData,
    *,
    provenance: CompositionStateProvenance,
) -> CompositionStateRecord:
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        return await service.save_composition_state(
            session_id,
            state,
            provenance=provenance,
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)


async def _service_lock_contention(
    first_service: SessionServiceImpl,
    second_service: SessionServiceImpl,
    session_id: UUID,
    first: Callable[[], Awaitable[Any]],
    second: Callable[[], Awaitable[Any]],
) -> tuple[Any, Any]:
    """Pause the winner inside the operation authority, then contend."""

    first_authority = first_service.session_operation_authority
    second_authority = second_service.session_operation_authority
    original_first_single = first_authority._locked_transaction
    original_first_pair = first_authority._locked_pair_transaction
    original_second_single = second_authority._locked_transaction
    original_second_pair = second_authority._locked_pair_transaction
    original_second_begin = second_service._session_process_locked_begin
    original_second_lock = second_service._session_write_lock
    held = threading.Event()
    release = threading.Event()
    contender_waiting = threading.Event()
    contender_acquired = threading.Event()
    paused = False
    first_single_calls = 0

    def pause_first(locked_session_ids: tuple[str, ...]) -> None:
        nonlocal paused
        if str(session_id) in locked_session_ids and not paused:
            paused = True
            held.set()
            assert release.wait(timeout=10)

    @contextlib.contextmanager
    def controlled_first_single(locked_session_id: str):
        nonlocal first_single_calls
        with original_first_single(locked_session_id) as conn:
            first_single_calls += 1
            # Archive uses three short authority transactions: acquire,
            # decision, then terminal cascade. Pause the terminal transaction
            # so "archive first" means the contender observes the committed
            # cascade, not merely the earlier lease acquisition.
            if first_single_calls >= 3:
                pause_first((locked_session_id,))
            yield conn

    @contextlib.contextmanager
    def controlled_first_pair(first_session_id: str, second_session_id: str):
        with original_first_pair(first_session_id, second_session_id) as conn:
            pause_first((first_session_id, second_session_id))
            yield conn

    @contextlib.contextmanager
    def observed_second_single(locked_session_id: str):
        if locked_session_id == str(session_id):
            contender_waiting.set()
        with original_second_single(locked_session_id) as conn:
            if locked_session_id == str(session_id):
                contender_acquired.set()
            yield conn

    @contextlib.contextmanager
    def observed_second_pair(first_session_id: str, second_session_id: str):
        if str(session_id) in {first_session_id, second_session_id}:
            contender_waiting.set()
        with original_second_pair(first_session_id, second_session_id) as conn:
            if str(session_id) in {first_session_id, second_session_id}:
                contender_acquired.set()
            yield conn

    @contextlib.contextmanager
    def observed_second_begin(locked_session_id: str):
        if locked_session_id == str(session_id):
            contender_waiting.set()
        with original_second_begin(locked_session_id) as conn:
            yield conn

    @contextlib.contextmanager
    def observed_second_lock(conn: Any, locked_session_id: str):
        with original_second_lock(conn, locked_session_id):
            if locked_session_id == str(session_id):
                contender_acquired.set()
            yield

    with (
        patch.object(first_authority, "_locked_transaction", new=controlled_first_single),
        patch.object(first_authority, "_locked_pair_transaction", new=controlled_first_pair),
        patch.object(second_authority, "_locked_transaction", new=observed_second_single),
        patch.object(second_authority, "_locked_pair_transaction", new=observed_second_pair),
        patch.object(second_service, "_session_process_locked_begin", new=observed_second_begin),
        patch.object(second_service, "_session_write_lock", new=observed_second_lock),
    ):
        first_task = asyncio.create_task(first())
        assert await asyncio.to_thread(held.wait, 10)
        second_task = asyncio.create_task(second())
        assert await asyncio.to_thread(contender_waiting.wait, 10)
        was_blocked = not contender_acquired.is_set()
        release.set()
        results = tuple(await asyncio.gather(first_task, second_task, return_exceptions=True))
        assert was_blocked
        return results  # type: ignore[return-value]


async def _blob_delete_first_contention(
    reserve_service: SessionServiceImpl,
    session_id: UUID,
    delete_first: Callable[[], Awaitable[Any]],
    reserve_second: Callable[[], Awaitable[Any]],
) -> tuple[Any, Any]:
    """Hold blob deletion's custody lock while fork reservation contends on it.

    ``delete_blob`` holds ``_blob_custody_session_lock`` across its phase
    transaction; on SQLite that is the same-session process mutex every other
    same-session writer (the operation authority's reservation, fork staging)
    must take first. The contender must be observed asking for that mutex and
    must hold it only after the delete releases.
    """

    from elspeth.web.blobs import service as blob_service_module
    from elspeth.web.sessions import locking as session_locking

    del reserve_service  # the contender is observed at the lock, not on a service seam
    original_blob_lock = blob_service_module._blob_custody_session_lock
    original_mutex = session_locking.sqlite_session_mutex
    held = threading.Barrier(2)
    release = threading.Barrier(2)
    reserve_waiting = threading.Event()
    reserve_acquired = threading.Event()
    delete_thread: list[threading.Thread] = []

    @contextlib.contextmanager
    def controlled_blob_lock(engine: Any, locked_session_id: str):
        with original_blob_lock(engine, locked_session_id) as conn:
            if locked_session_id == str(session_id):
                delete_thread.append(threading.current_thread())
                held.wait(timeout=5)
                release.wait(timeout=5)
            yield conn

    @contextlib.contextmanager
    def observed_mutex(engine: Any, locked_session_id: str):
        contender = locked_session_id == str(session_id) and delete_thread and threading.current_thread() is not delete_thread[0]
        if contender:
            reserve_waiting.set()
        with original_mutex(engine, locked_session_id):
            if contender:
                reserve_acquired.set()
            yield

    with (
        patch.object(blob_service_module, "_blob_custody_session_lock", new=controlled_blob_lock),
        patch.object(session_locking, "sqlite_session_mutex", new=observed_mutex),
    ):
        delete_task = asyncio.create_task(delete_first())
        await asyncio.to_thread(held.wait, 5)
        reserve_task = asyncio.create_task(reserve_second())
        assert await asyncio.to_thread(reserve_waiting.wait, 5)
        was_blocked = not reserve_acquired.is_set()
        await asyncio.to_thread(release.wait, 5)
        results = tuple(await asyncio.gather(delete_task, reserve_task, return_exceptions=True))
        assert was_blocked
        return results  # type: ignore[return-value]


async def _fork_first_blob_contention(
    fork_service: SessionServiceImpl,
    session_id: UUID,
    fork_first: Callable[[], Awaitable[Any]],
    delete_second: Callable[[], Awaitable[Any]],
) -> tuple[Any, Any]:
    """Hold fork staging's custody transaction while blob deletion contends on it."""

    from elspeth.web.blobs import service as blob_service_module
    from elspeth.web.coordination import repository as coordination_repository

    original_blob_lock = blob_service_module._blob_custody_session_lock
    original_transaction_lock = coordination_repository.transaction_session_lock
    held = threading.Barrier(2)
    release = threading.Barrier(2)
    delete_waiting = threading.Event()
    delete_acquired = threading.Event()
    paused = False

    @contextlib.contextmanager
    def controlled_transaction_lock(conn: Any, engine: Any, locked_session_id: str):
        nonlocal paused
        with original_transaction_lock(conn, engine, locked_session_id):
            if locked_session_id == str(session_id) and not paused:
                paused = True
                held.wait(timeout=5)
                release.wait(timeout=5)
            yield

    @contextlib.contextmanager
    def observed_blob_lock(engine: Any, locked_session_id: str):
        if locked_session_id == str(session_id):
            delete_waiting.set()
        with original_blob_lock(engine, locked_session_id) as conn:
            if locked_session_id == str(session_id):
                delete_acquired.set()
            yield conn

    with (
        patch.object(coordination_repository, "transaction_session_lock", new=controlled_transaction_lock),
        patch.object(blob_service_module, "_blob_custody_session_lock", new=observed_blob_lock),
    ):
        fork_task = asyncio.create_task(fork_first())
        await asyncio.to_thread(held.wait, 5)
        delete_task = asyncio.create_task(delete_second())
        assert await asyncio.to_thread(delete_waiting.wait, 5)
        was_blocked = not delete_acquired.is_set()
        await asyncio.to_thread(release.wait, 5)
        results = tuple(await asyncio.gather(fork_task, delete_task, return_exceptions=True))
        assert was_blocked
        return results  # type: ignore[return-value]


def _cleanup_race_user(engine: Any, user_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.user_id == user_id))


async def _claim_fork(service: SessionServiceImpl, parent_id: UUID, *, operation_id: str | None = None):
    return await _claim_dual_fenced_fork(
        service,
        parent_id,
        operation_id=operation_id,
    )


async def _claim_dual_fenced_fork(
    service: SessionServiceImpl,
    parent_id: UUID,
    *,
    operation_id: str | None = None,
) -> SessionForkParentAuthority:
    parent_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=parent_id,
            operation_kind=SessionOperationKind.SESSION_FORK,
            owner_instance_id=service._owner_instance_id,
            lease_seconds=service._session_operation_lease_seconds,
        )
    )
    claimed = await service.reserve_guided_operation(
        session_id=parent_id,
        operation_id=operation_id or str(uuid4()),
        kind="session_fork",
        request_hash="a" * 64,
        actor="composer_route",
        lease_seconds=service._session_operation_lease_seconds,
        session_operation_context=parent_context,
    )
    assert type(claimed) in {GuidedOperationClaimed, GuidedOperationTakenOver}
    return SessionForkParentAuthority(
        parent_context=parent_context,
        guided_fence=claimed.fence,
    )


def _release_fork_authority(
    service: SessionServiceImpl,
    authority,
) -> None:
    service.session_operation_authority.release(authority.child_context)
    service.session_operation_authority.release(authority.parent.parent_context)


@pytest.mark.asyncio
async def test_dual_fenced_fork_server_mints_child_epoch_two_and_takeover_rotates_both(
    service: SessionServiceImpl,
    engine,
) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    operation_id = str(uuid4())
    first_authority = await _claim_dual_fenced_fork(service, parent.id, operation_id=operation_id)
    first = await service.fork_session(
        first_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )

    assert first.authority.parent is first_authority
    assert first.authority.parent.guided_fence.operation_id == operation_id
    assert first.authority.child_context.operation_kind is SessionOperationKind.SESSION_FORK
    assert first.authority.child_context.fence.operation_epoch == 2
    assert first.authority.child_context.fence.operation_id != first.authority.parent.parent_context.fence.operation_id != operation_id

    expired = datetime.now(UTC) - timedelta(seconds=1)
    with engine.begin() as conn:
        conn.execute(
            update(session_operation_fences_table)
            .where(
                session_operation_fences_table.c.session_id.in_(
                    [
                        str(parent.id),
                        str(first.session.id),
                    ]
                )
            )
            .values(lease_expires_at=expired)
        )
        conn.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
            .values(lease_expires_at=expired)
        )

    takeover_authority = await _claim_dual_fenced_fork(service, parent.id, operation_id=operation_id)
    resumed = await service.fork_session(
        takeover_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )

    assert resumed.session.id == first.session.id
    assert resumed.authority.parent.guided_fence.operation_id == operation_id
    assert resumed.authority.parent.parent_context.fence.operation_epoch > first_authority.parent_context.fence.operation_epoch
    assert resumed.authority.child_context.fence.operation_epoch > first.authority.child_context.fence.operation_epoch
    assert resumed.authority.parent.parent_context.fence.lease_token != first_authority.parent_context.fence.lease_token
    assert resumed.authority.child_context.fence.lease_token != first.authority.child_context.fence.lease_token


@pytest.mark.asyncio
async def test_hidden_fork_child_lease_uses_only_exact_composite_validation_and_renewal(
    service: SessionServiceImpl,
    engine,
) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    parent_authority = await _claim_dual_fenced_fork(service, parent.id)
    staged = await service.fork_session(
        parent_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    authority = service.session_operation_authority

    assert authority.validate_fork_child_lease(staged.authority) == staged.authority.child_context
    with pytest.raises(SessionOperationFenceLost) as generic_cas:
        authority.compare_and_swap(staged.authority.child_context)
    assert generic_cas.value.reason is FenceLossReason.OWNER_INACTIVE

    with engine.connect() as conn:
        before = conn.execute(
            select(session_operation_fences_table.c.lease_expires_at).where(
                session_operation_fences_table.c.session_id == str(staged.session.id)
            )
        ).scalar_one()
    assert authority.renew_fork_child_lease(staged.authority, lease_seconds=600) == staged.authority.child_context
    with engine.connect() as conn:
        after = conn.execute(
            select(session_operation_fences_table.c.lease_expires_at).where(
                session_operation_fences_table.c.session_id == str(staged.session.id)
            )
        ).scalar_one()
    assert after > before

    with engine.begin() as conn:
        conn.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == staged.authority.parent.guided_fence.operation_id,
            )
            .values(result_session_id=None)
        )
    with pytest.raises(AuditIntegrityError, match="exact bound child"):
        authority.validate_fork_child_lease(staged.authority)

    authority.release(staged.authority.child_context)
    authority.release(staged.authority.parent.parent_context)


async def _parent_with_fork_message(service: SessionServiceImpl):
    parent = await service.create_session("alice", "Parent", "local")
    await service.add_message(parent.id, "user", "root", writer_principal="route_user_message")
    fork_message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        writer_principal="route_user_message",
    )
    return parent, fork_message


def _insert_blob_row(
    engine,
    *,
    blob_id: UUID,
    session_id: UUID,
    content_hash: str = "c" * 64,
    size_bytes: int = 3,
    status: str = "ready",
    storage_path: str | None = None,
    created_at: datetime | None = None,
    mime_type: str = "application/octet-stream",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(session_id),
                filename=f"{blob_id}.bin",
                mime_type=mime_type,
                size_bytes=size_bytes,
                content_hash=content_hash,
                storage_path=storage_path or f"/tmp/{blob_id}.bin",
                created_at=created_at if created_at is not None else datetime.now(UTC),
                created_by="user",
                source_description=None,
                status=status,
                creation_modality="verbatim",
            )
        )


@pytest.mark.parametrize("noncanonical", ["upper", "braced"])
def test_frozen_blob_plan_rejects_noncanonical_uuid_spellings(noncanonical: str) -> None:
    source_session_id = uuid4()
    child_session_id = uuid4()
    source_blob_id = uuid4()
    target_blob_id = fork_blob_id(target_session_id=child_session_id, source_blob_id=source_blob_id)

    def spelling(value: UUID) -> str:
        return str(value).upper() if noncanonical == "upper" else f"{{{value}}}"

    content = json.dumps(
        {
            "schema": "session-fork-blob-plan.v1",
            "source_session_id": str(source_session_id),
            "child_session_id": str(child_session_id),
            "operation_id": "fork-operation",
            "source_blobs": [
                {
                    "source_blob_id": spelling(source_blob_id),
                    "target_blob_id": spelling(target_blob_id),
                    "content_hash": "a" * 64,
                    "size_bytes": 1,
                }
            ],
        }
    )

    with pytest.raises(AuditIntegrityError, match="non-canonical blob id"):
        _fork_blob_plan_from_content(
            content,
            expected_source_session_id=source_session_id,
            expected_child_session_id=child_session_id,
            expected_operation_id="fork-operation",
        )


@pytest.mark.asyncio
async def test_fork_stages_one_hidden_bound_child_and_takeover_reuses_it(service, engine) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    operation_id = str(uuid4())
    first_fence = await _claim_fork(service, parent.id, operation_id=operation_id)

    first = await service.fork_session(
        first_fence,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )

    assert first.session.archived_at is not None
    assert first.state is None
    assert first.messages[-1].content == "edited"
    assert [session.id for session in await service.list_sessions("alice", "local", include_archived=True)] == [parent.id]
    with engine.connect() as conn:
        initial_fence = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(first.session.id))
        ).one()
    assert initial_fence.operation_kind == SessionOperationKind.SESSION_FORK.value
    assert initial_fence.operation_epoch == 2
    assert initial_fence.released_at is None
    with engine.begin() as conn:
        expired = datetime.now(UTC) - timedelta(seconds=1)
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id.in_([str(parent.id), str(first.session.id)]))
            .values(lease_expires_at=expired)
        )
        conn.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
            .values(lease_expires_at=expired)
        )
    takeover_fence = await _claim_fork(service, parent.id, operation_id=operation_id)

    second = await service.fork_session(
        takeover_fence,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )

    assert second.session.id == first.session.id
    assert [message.id for message in second.messages] == [message.id for message in first.messages]
    assert second.state == first.state
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(sessions_table)).scalar_one() == 2


@pytest.mark.asyncio
async def test_takeover_fails_closed_when_bound_child_lineage_drifted(service, engine) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    parent_authority = await _claim_dual_fenced_fork(service, parent.id)
    staged = await service.fork_session(
        parent_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    with engine.begin() as conn:
        conn.execute(
            update(sessions_table).where(sessions_table.c.id == str(staged.session.id)).values(forked_from_session_id=str(uuid4()))
        )

    with pytest.raises(AuditIntegrityError, match="bound child"):
        await service.fork_session(
            parent_authority,
            fork_message_id=fork_message.id,
            new_message_content="edited",
        )


@pytest.mark.asyncio
async def test_settlement_rewrites_state_activates_child_and_completes_locator_atomically(service) -> None:
    parent = await service.create_session("alice", "Parent", "local")
    original_state = await _save_composition_state(
        service,
        parent.id,
        CompositionStateData(
            sources={"orders": {"plugin": "csv", "options": {"blob_ref": str(uuid4())}}},
            is_valid=True,
        ),
        provenance="session_seed",
    )
    fork_message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=original_state.id,
        writer_principal="route_user_message",
    )
    parent_authority = await _claim_dual_fenced_fork(service, parent.id)
    staged = await service.fork_session(
        parent_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    assert staged.state is not None
    rewritten_blob_id = uuid4()
    response_hash = "b" * 64

    settled = await service.settle_guided_fork_operation(
        GuidedForkSettlementCommand(
            authority=staged.authority,
            expected_current_state_id=staged.state.id,
            edited_message_id=staged.messages[-1].id,
            rewritten_state_id=uuid4(),
            rewritten_state=CompositionStateData(
                sources={"orders": {"plugin": "csv", "options": {"blob_ref": str(rewritten_blob_id)}}},
                is_valid=True,
            ),
            response_hash=response_hash,
            actor="composer_route",
        )
    )

    assert settled.id == staged.session.id
    assert settled.archived_at is None
    current_state = await service.get_current_state(staged.session.id)
    assert current_state is not None
    assert current_state.version == 1
    assert current_state.sources["orders"]["options"]["blob_ref"] == str(rewritten_blob_id)
    child_messages = await service.get_messages(staged.session.id, limit=None)
    edited_message = next(message for message in child_messages if message.id == staged.messages[-1].id)
    assert edited_message.composition_state_id == current_state.id
    operation = await service.get_guided_operation(
        session_id=parent.id,
        operation_id=parent_authority.guided_fence.operation_id,
        kind="session_fork",
        request_hash="a" * 64,
    )
    assert operation == GuidedOperationCompleted(
        result=GuidedSessionResult(session_id=staged.session.id),
        response_hash=response_hash,
    )


@pytest.mark.asyncio
async def test_settlement_validates_composite_and_terminalizes_guided_before_child_activation(
    service: SessionServiceImpl,
) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    parent_authority = await _claim_dual_fenced_fork(service, parent.id)
    staged = await service.fork_session(
        parent_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    observed_child_archived: list[bool] = []
    original_complete = _GuidedSessionMutations.complete

    def observed_complete(facet, *args, **kwargs):
        observed_child_archived.append(staged.session.archived_at is not None)
        return original_complete(facet, *args, **kwargs)

    with patch.object(
        _GuidedSessionMutations,
        "complete",
        new=observed_complete,
    ):
        settled = await service.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=staged.authority,
                expected_current_state_id=None,
                edited_message_id=staged.messages[-1].id,
                rewritten_state_id=None,
                rewritten_state=None,
                response_hash="c" * 64,
                actor="composer_route",
            )
        )

    assert observed_child_archived == [True]
    assert settled.archived_at is None

    stale_parent = replace(
        staged.authority.parent.parent_context,
        fence=replace(
            staged.authority.parent.parent_context.fence,
            lease_token="stale-parent-token",
        ),
    )
    stale_authority = replace(
        staged.authority,
        parent=replace(staged.authority.parent, parent_context=stale_parent),
    )
    with pytest.raises(GuidedOperationFenceLostError):
        await service.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=stale_authority,
                expected_current_state_id=None,
                edited_message_id=staged.messages[-1].id,
                rewritten_state_id=None,
                rewritten_state=None,
                response_hash="d" * 64,
                actor="composer_route",
            )
        )


@pytest.mark.asyncio
async def test_settlement_rejects_missing_retained_frozen_blob_plan(service, engine) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(fence, fork_message_id=fork_message.id, new_message_content="edited")
    with engine.begin() as conn:
        conn.execute(
            update(chat_messages_table)
            .where(
                chat_messages_table.c.session_id == str(staged.session.id),
                chat_messages_table.c.role == "audit",
                chat_messages_table.c.writer_principal == "session_fork",
            )
            .values(writer_principal="route_system_message")
        )

    with pytest.raises(AuditIntegrityError, match="exactly one retained frozen blob plan"):
        await service.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=staged.authority,
                expected_current_state_id=None,
                edited_message_id=staged.messages[-1].id,
                rewritten_state_id=None,
                rewritten_state=None,
                response_hash="b" * 64,
                actor="composer_route",
            )
        )

    assert (await service.get_session(staged.session.id)).archived_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["missing", "extra", "pending", "hash", "size"])
async def test_settlement_requires_exact_ready_child_blob_cohort(service, engine, drift: str) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    parent_blob_id = uuid4()
    _insert_blob_row(engine, blob_id=parent_blob_id, session_id=parent.id)
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(fence, fork_message_id=fork_message.id, new_message_content="edited")
    assert len(staged.blob_plan) == 1
    expected = staged.blob_plan[0]
    if drift != "missing":
        _insert_blob_row(
            engine,
            blob_id=expected.target_blob_id,
            session_id=staged.session.id,
            content_hash="d" * 64 if drift == "hash" else expected.content_hash,
            size_bytes=expected.size_bytes + 1 if drift == "size" else expected.size_bytes,
            status="pending" if drift == "pending" else "ready",
        )
    if drift == "extra":
        _insert_blob_row(engine, blob_id=uuid4(), session_id=staged.session.id)

    with pytest.raises(AuditIntegrityError, match="child blob"):
        await service.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=staged.authority,
                expected_current_state_id=None,
                edited_message_id=staged.messages[-1].id,
                rewritten_state_id=None,
                rewritten_state=None,
                response_hash="b" * 64,
                actor="composer_route",
            )
        )

    assert (await service.get_session(staged.session.id)).archived_at is not None
    operation = await service.get_guided_operation(
        session_id=parent.id,
        operation_id=fence.guided_fence.operation_id,
        kind="session_fork",
        request_hash="a" * 64,
    )
    assert isinstance(operation, GuidedOperationActive)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["id", "sentinel", "storage_path"])
async def test_settlement_rejects_rewritten_state_with_parent_blob_custody(
    service,
    engine,
    reference_kind: str,
) -> None:
    parent = await service.create_session("alice", "Parent", "local")
    parent_blob_id = uuid4()
    parent_storage_path = f"/tmp/{parent_blob_id}.bin"
    _insert_blob_row(
        engine,
        blob_id=parent_blob_id,
        session_id=parent.id,
        storage_path=parent_storage_path,
    )
    original_state = await _save_composition_state(
        service,
        parent.id,
        CompositionStateData(sources={"orders": {"plugin": "csv", "options": {"path": "old.csv"}}}),
        provenance="session_seed",
    )
    fork_message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=original_state.id,
        writer_principal="route_user_message",
    )
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(fence, fork_message_id=fork_message.id, new_message_content="edited")
    assert staged.state is not None and len(staged.blob_plan) == 1
    expected = staged.blob_plan[0]
    _insert_blob_row(
        engine,
        blob_id=expected.target_blob_id,
        session_id=staged.session.id,
        content_hash=expected.content_hash,
        size_bytes=expected.size_bytes,
    )
    stale_reference = {
        "id": str(parent_blob_id),
        "sentinel": f"blob:{parent_blob_id}",
        "storage_path": parent_storage_path,
    }[reference_kind]

    with pytest.raises(AuditIntegrityError, match="retains parent blob custody"):
        await service.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=staged.authority,
                expected_current_state_id=staged.state.id,
                edited_message_id=staged.messages[-1].id,
                rewritten_state_id=uuid4(),
                rewritten_state=CompositionStateData(sources={"orders": {"plugin": "csv", "options": {"blob_ref": stale_reference}}}),
                response_hash="b" * 64,
                actor="composer_route",
            )
        )

    assert (await service.get_session(staged.session.id)).archived_at is not None


@pytest.mark.asyncio
async def test_settlement_rejects_parent_blob_reference_excluded_from_ready_plan(service, engine) -> None:
    parent = await service.create_session("alice", "Parent", "local")
    pending_parent_blob_id = uuid4()
    _insert_blob_row(
        engine,
        blob_id=pending_parent_blob_id,
        session_id=parent.id,
        status="pending",
    )
    original_state = await _save_composition_state(
        service,
        parent.id,
        CompositionStateData(
            sources={
                "orders": {
                    "plugin": "csv",
                    "options": {"blob_ref": str(pending_parent_blob_id)},
                }
            }
        ),
        provenance="session_seed",
    )
    fork_message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=original_state.id,
        writer_principal="route_user_message",
    )
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(fence, fork_message_id=fork_message.id, new_message_content="edited")
    assert staged.state is not None and staged.blob_plan == ()

    with pytest.raises(AuditIntegrityError, match="retains parent blob custody"):
        await service.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=staged.authority,
                expected_current_state_id=staged.state.id,
                edited_message_id=staged.messages[-1].id,
                rewritten_state_id=uuid4(),
                rewritten_state=CompositionStateData(
                    sources={
                        "orders": {
                            "plugin": "csv",
                            "options": {"blob_ref": str(pending_parent_blob_id)},
                        }
                    }
                ),
                response_hash="b" * 64,
                actor="composer_route",
            )
        )

    assert (await service.get_session(staged.session.id)).archived_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_point", ["state_replace", "message_repoint", "activation", "operation_complete"])
async def test_settlement_fault_rolls_back_every_surface_and_child_remains_takeover_safe(
    service,
    engine,
    fault_point: str,
) -> None:
    parent = await service.create_session("alice", "Parent", "local")
    state = await _save_composition_state(
        service,
        parent.id,
        CompositionStateData(sources={"source": {"plugin": "csv", "options": {"path": "old.csv"}}}, is_valid=True),
        provenance="session_seed",
    )
    message = await service.add_message(
        parent.id,
        "user",
        "fork",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(fence, fork_message_id=message.id, new_message_content="edited")
    assert staged.state is not None
    chat_updates = 0

    def inject(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal chat_updates
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update chat_messages"):
            chat_updates += 1
        should_fail = (
            (fault_point == "state_replace" and normalized.startswith("delete from composition_states"))
            # Settlement rewrites the edited message across two chat_messages
            # UPDATEs (detach-to-NULL -> delete staged state -> insert
            # replacement -> repoint), ordered so the replacement reclaims
            # composition-state version 1. The detach is the 1st ``update
            # chat_messages`` statement and the repoint is the 2nd, so the
            # repoint fault probe targets ``chat_updates == 2``.
            or (fault_point == "message_repoint" and normalized.startswith("update chat_messages") and chat_updates == 2)
            or (fault_point == "activation" and normalized.startswith("update sessions") and "archived_at" in normalized)
            or (fault_point == "operation_complete" and normalized.startswith("update guided_operations") and "status" in normalized)
        )
        if should_fail:
            raise RuntimeError(f"injected {fault_point}")

    event.listen(engine, "before_cursor_execute", inject)
    try:
        with pytest.raises(RuntimeError, match=fault_point):
            await service.settle_guided_fork_operation(
                GuidedForkSettlementCommand(
                    authority=staged.authority,
                    expected_current_state_id=staged.state.id,
                    edited_message_id=staged.messages[-1].id,
                    rewritten_state_id=uuid4(),
                    rewritten_state=CompositionStateData(
                        sources={"source": {"plugin": "csv", "options": {"path": "new.csv"}}},
                        is_valid=True,
                    ),
                    response_hash="b" * 64,
                    actor="composer_route",
                )
            )
    finally:
        event.remove(engine, "before_cursor_execute", inject)

    retained = await service.get_session(staged.session.id)
    assert retained.archived_at is not None
    retained_state = await service.get_current_state(staged.session.id)
    assert retained_state is not None and retained_state.id == staged.state.id
    retained_messages = await service.get_messages(staged.session.id, limit=None)
    retained_edited = next(item for item in retained_messages if item.id == staged.messages[-1].id)
    assert retained_edited.composition_state_id == staged.state.id
    operation = await service.get_guided_operation(
        session_id=parent.id,
        operation_id=fence.guided_fence.operation_id,
        kind="session_fork",
        request_hash="a" * 64,
    )
    assert isinstance(operation, GuidedOperationActive)
    resumed = await service.fork_session(fence, fork_message_id=message.id, new_message_content="edited")
    assert resumed.session.id == staged.session.id


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("ELSPETH_TEST_POSTGRES_URL"),
    reason="ELSPETH_TEST_POSTGRES_URL is required for the PostgreSQL settlement rollback matrix",
)
async def test_postgres_settlement_fault_matrix() -> None:
    postgres_engine = create_session_engine(os.environ["ELSPETH_TEST_POSTGRES_URL"])
    initialize_session_schema(postgres_engine)
    with postgres_engine.connect() as conn:
        before_ids = {row.id for row in conn.execute(select(sessions_table.c.id)).all()}
    postgres_service = DualFencedSessionServiceHarness(
        postgres_engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.postgres-fork-settlement"),
    )
    try:
        for fault_point in ("state_replace", "message_repoint", "activation", "operation_complete"):
            await test_settlement_fault_rolls_back_every_surface_and_child_remains_takeover_safe(
                postgres_service,
                postgres_engine,
                fault_point,
            )
    finally:
        with postgres_engine.begin() as conn:
            conn.execute(delete(sessions_table).where(sessions_table.c.id.not_in(before_ids)))
        postgres_engine.dispose()


@pytest.mark.asyncio
async def test_failed_binding_clear_retains_hidden_archived_child_and_plan(service, engine) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(
        fence,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    assert staged.session.id not in {session.id for session in await service.list_sessions("alice", "local", include_archived=True)}

    await service.fail_guided_fork_operation(staged.authority, failure_code="operation_failed", actor="composer_route")

    listed = await service.list_sessions("alice", "local", include_archived=True)
    assert staged.session.id not in {session.id for session in listed}
    assert staged.session.id not in {session.id for session in await service.list_sessions("alice", "local")}
    retained = await service.get_session(staged.session.id)
    assert retained.archived_at is not None
    with engine.connect() as conn:
        audit_rows = conn.execute(
            select(chat_messages_table.c.content).where(
                chat_messages_table.c.session_id == str(staged.session.id),
                chat_messages_table.c.role == "audit",
                chat_messages_table.c.writer_principal == "session_fork",
            )
        ).all()
    assert len(audit_rows) == 1
    assert '"schema":"session-fork-blob-plan.v1"' in audit_rows[0].content


@pytest.mark.asyncio
async def test_archiving_completed_fork_child_soft_archives_due_to_result_binding(service, engine) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(
        fence,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    await service.settle_guided_fork_operation(
        GuidedForkSettlementCommand(
            authority=staged.authority,
            expected_current_state_id=None,
            edited_message_id=staged.messages[-1].id,
            rewritten_state_id=None,
            rewritten_state=None,
            response_hash="b" * 64,
            actor="composer_route",
        )
    )
    _release_fork_authority(service, staged.authority)

    await service.archive_session(staged.session.id)

    retained = await service.get_session(staged.session.id)
    assert retained.archived_at is not None
    assert staged.session.id in {session.id for session in await service.list_sessions("alice", "local", include_archived=True)}
    with engine.connect() as conn:
        archive_fence = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(staged.session.id))
        ).one()
    assert archive_fence.operation_kind == SessionOperationKind.ARCHIVE.value
    assert archive_fence.operation_epoch == 3
    assert archive_fence.released_at is not None


@pytest.mark.asyncio
async def test_archiving_in_progress_fork_child_is_refused_and_releases_archive_context(service, engine) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    fence = await _claim_fork(service, parent.id)
    staged = await service.fork_session(
        fence,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )

    with pytest.raises(SessionOperationConflictError):
        await service.archive_session(staged.session.id)

    assert (await service.get_session(staged.session.id)).archived_at is not None
    with engine.connect() as conn:
        released_fence = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(staged.session.id))
        ).one()
    assert released_fence.operation_kind == SessionOperationKind.SESSION_FORK.value
    assert released_fence.operation_epoch == 2
    assert released_fence.released_at is None


@pytest.mark.asyncio
async def test_archiving_completed_fork_parent_preserves_child_and_terminal_evidence(service) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    operation_id = str(uuid4())
    fence = await _claim_fork(service, parent.id, operation_id=operation_id)
    staged = await service.fork_session(
        fence,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    await service.settle_guided_fork_operation(
        GuidedForkSettlementCommand(
            authority=staged.authority,
            expected_current_state_id=None,
            edited_message_id=staged.messages[-1].id,
            rewritten_state_id=None,
            rewritten_state=None,
            response_hash="b" * 64,
            actor="composer_route",
        )
    )
    _release_fork_authority(service, staged.authority)

    await service.archive_session(parent.id)
    with pytest.raises(SessionOperationFenceLost) as inactive:
        service.session_operation_authority.acquire(
            session_id=parent.id,
            operation_kind=SessionOperationKind.SESSION_FORK,
            owner_instance_id=service._owner_instance_id,
            lease_seconds=service._session_operation_lease_seconds,
        )
    assert inactive.value.reason is FenceLossReason.OWNER_INACTIVE
    replay = await service.get_guided_operation(
        session_id=parent.id,
        operation_id=operation_id,
        kind="session_fork",
        request_hash="a" * 64,
    )

    assert replay == GuidedOperationCompleted(
        result=GuidedSessionResult(session_id=staged.session.id),
        response_hash="b" * 64,
    )
    assert (await service.get_session(parent.id)).archived_at is not None
    assert staged.session.id in {session.id for session in await service.list_sessions("alice", "local")}


@pytest.mark.asyncio
async def test_archiving_failed_fork_parent_preserves_failed_operation_and_child_evidence(service) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    operation_id = str(uuid4())
    fence = await _claim_fork(service, parent.id, operation_id=operation_id)
    staged = await service.fork_session(
        fence,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    await service.fail_guided_fork_operation(staged.authority, failure_code="operation_failed", actor="composer_route")
    _release_fork_authority(service, staged.authority)

    await service.archive_session(parent.id)
    with pytest.raises(SessionOperationFenceLost) as inactive:
        service.session_operation_authority.acquire(
            session_id=parent.id,
            operation_kind=SessionOperationKind.SESSION_FORK,
            owner_instance_id=service._owner_instance_id,
            lease_seconds=service._session_operation_lease_seconds,
        )
    assert inactive.value.reason is FenceLossReason.OWNER_INACTIVE
    replay = await service.get_guided_operation(
        session_id=parent.id,
        operation_id=operation_id,
        kind="session_fork",
        request_hash="a" * 64,
    )

    assert replay == GuidedOperationFailed(failure_code="operation_failed")
    assert (await service.get_session(parent.id)).archived_at is not None
    assert (await service.get_session(staged.session.id)).archived_at is not None


@pytest.mark.asyncio
async def test_archive_parent_rejects_in_progress_fork_under_database_guard(service) -> None:
    parent, _fork_message = await _parent_with_fork_message(service)
    await _claim_fork(service, parent.id)

    with pytest.raises(SessionOperationConflictError):
        await service.archive_session(parent.id)

    assert (await service.get_session(parent.id)).id == parent.id


@pytest.mark.asyncio
async def test_reservation_rejects_parent_already_removed_without_operation_or_child(service, engine) -> None:
    parent, _fork_message = await _parent_with_fork_message(service)
    await service.archive_session(parent.id)

    with pytest.raises(SessionOperationFenceLost):
        await _claim_fork(service, parent.id)

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(guided_operations_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(sessions_table)).scalar_one() == 0


@pytest.mark.asyncio
async def test_fork_of_fork_preserves_historical_plan_but_selects_current_binding(service) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    first_fence = await _claim_fork(service, parent.id)
    first = await service.fork_session(
        first_fence,
        fork_message_id=fork_message.id,
        new_message_content="first edit",
    )
    await service.settle_guided_fork_operation(
        GuidedForkSettlementCommand(
            authority=first.authority,
            expected_current_state_id=None,
            edited_message_id=first.messages[-1].id,
            rewritten_state_id=None,
            rewritten_state=None,
            response_hash="b" * 64,
            actor="composer_route",
        )
    )
    _release_fork_authority(service, first.authority)
    second_fork_message = await service.add_message(
        first.session.id,
        "user",
        "fork child",
        writer_principal="route_user_message",
    )
    second_fence = await _claim_fork(service, first.session.id)

    second = await service.fork_session(
        second_fence,
        fork_message_id=second_fork_message.id,
        new_message_content="second edit",
    )

    assert second.session.forked_from_session_id == first.session.id
    assert second.session.archived_at is not None
    assert second.messages[-1].content == "second edit"


@pytest.mark.asyncio
async def test_fork_of_fork_rejects_malformed_retained_historical_plan(service) -> None:
    parent, fork_message = await _parent_with_fork_message(service)
    first_fence = await _claim_fork(service, parent.id)
    first = await service.fork_session(
        first_fence,
        fork_message_id=fork_message.id,
        new_message_content="first edit",
    )
    await service.settle_guided_fork_operation(
        GuidedForkSettlementCommand(
            authority=first.authority,
            expected_current_state_id=None,
            edited_message_id=first.messages[-1].id,
            rewritten_state_id=None,
            rewritten_state=None,
            response_hash="b" * 64,
            actor="composer_route",
        )
    )
    _release_fork_authority(service, first.authority)
    await service.add_message(
        first.session.id,
        "audit",
        "{",
        writer_principal="session_fork",
    )
    second_fork_message = await service.add_message(
        first.session.id,
        "user",
        "fork child",
        writer_principal="route_user_message",
    )
    second_fence = await _claim_fork(service, first.session.id)

    with pytest.raises(AuditIntegrityError, match="not valid JSON"):
        await service.fork_session(
            second_fence,
            fork_message_id=second_fork_message.id,
            new_message_content="second edit",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ("archive", "stage"))
async def test_parent_archive_and_fork_staging_serialize_under_lock_contention(
    durable_engine,
    winner: str,
) -> None:
    """The parent lock admits either archive or one hidden staged child, never both."""

    race_service = _service_for(durable_engine)
    other_service = _service_for(durable_engine)
    user_id = f"fork-archive-race-{uuid4()}"
    parent = await race_service.create_session(user_id, "Parent", "local")
    message = await race_service.add_message(
        parent.id,
        "user",
        "fork here",
        writer_principal="route_user_message",
    )
    operation_id = str(uuid4())

    async def archive(target_service: SessionServiceImpl) -> None:
        await target_service.archive_session(parent.id)

    async def reserve_and_stage(target_service: SessionServiceImpl) -> Any:
        fence = await _claim_fork(target_service, parent.id, operation_id=operation_id)
        return await target_service.fork_session(
            fence,
            fork_message_id=message.id,
            new_message_content="edited",
        )

    try:
        if winner == "archive":
            archive_result, stage_result = await _service_lock_contention(
                race_service,
                other_service,
                parent.id,
                lambda: archive(race_service),
                lambda: reserve_and_stage(other_service),
            )
            assert archive_result is None
            assert isinstance(stage_result, SessionOperationFenceLost)
            assert stage_result.reason is FenceLossReason.MISSING
            with durable_engine.connect() as conn:
                assert (
                    conn.execute(
                        select(func.count())
                        .select_from(guided_operations_table)
                        .where(guided_operations_table.c.session_id == str(parent.id))
                    ).scalar_one()
                    == 0
                )
                assert (
                    conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.user_id == user_id)).scalar_one()
                    == 0
                )
        else:
            staged, archive_error = await _service_lock_contention(
                race_service,
                other_service,
                parent.id,
                lambda: reserve_and_stage(race_service),
                lambda: archive(other_service),
            )
            assert not isinstance(staged, BaseException)
            assert isinstance(archive_error, SessionOperationConflictError)
            assert staged.session.archived_at is not None
            assert (await race_service.get_session(parent.id)).archived_at is None
            with durable_engine.connect() as conn:
                operation = conn.execute(
                    select(guided_operations_table).where(
                        guided_operations_table.c.session_id == str(parent.id),
                        guided_operations_table.c.operation_id == operation_id,
                    )
                ).one()
                assert operation.result_session_id == str(staged.session.id)
                assert (
                    conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.user_id == user_id)).scalar_one()
                    == 2
                )
    finally:
        _cleanup_race_user(durable_engine, user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ("delete", "copy"))
async def test_source_blob_delete_and_planned_copy_serialize_under_lock_contention(
    durable_engine,
    tmp_path: Path,
    winner: str,
) -> None:
    """Deletion wins before planning, or the frozen fork blocks it through settlement."""

    race_service = _service_for(durable_engine)
    blob_service = BlobServiceImpl(durable_engine, tmp_path / f"blob-race-{uuid4()}")
    user_id = f"fork-blob-race-{uuid4()}"
    parent = await race_service.create_session(user_id, "Parent", "local")
    source_blob = await blob_service.create_blob(parent.id, "source.csv", b"a,b\n1,2\n", "text/csv")
    state = await _save_composition_state(
        race_service,
        parent.id,
        CompositionStateData(
            sources={
                "source": {
                    "plugin": "csv",
                    "options": {"blob_ref": str(source_blob.id), "path": source_blob.storage_path},
                }
            }
        ),
        provenance="session_seed",
    )
    message = await race_service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )
    operation_id = str(uuid4())

    async def reserve_stage_copy(target_service: SessionServiceImpl) -> tuple[Any, dict[UUID, Any], Any]:
        fence = await _claim_fork(target_service, parent.id, operation_id=operation_id)
        staged = await target_service.fork_session(
            fence,
            fork_message_id=message.id,
            new_message_content="edited",
        )

        async def checkpoint() -> None:
            return None

        copied = await blob_service.copy_blobs_for_fork(
            parent.id,
            staged.session.id,
            staged.blob_plan,
            BlobForkWriteFence(
                source_session_id=parent.id,
                target_session_id=staged.session.id,
                operation_id=fence.guided_fence.operation_id,
                lease_token=fence.guided_fence.lease_token,
                attempt=fence.guided_fence.attempt,
            ),
            checkpoint=checkpoint,
        )
        return staged, copied, fence

    try:
        if winner == "delete":
            deleted, staged_result = await _blob_delete_first_contention(
                race_service,
                parent.id,
                lambda: blob_service.delete_blob(source_blob.id),
                lambda: reserve_stage_copy(race_service),
            )
            assert deleted is None
            assert not isinstance(staged_result, BaseException)
            staged, copied, fence = staged_result
            assert staged.blob_plan == ()
            assert copied == {}
            with pytest.raises(AuditIntegrityError, match="absent from the frozen fork plan"):
                _rewrite_fork_state_blob_custody(
                    staged.state,
                    copied,
                    {},
                    # The source blob was deleted before staging: the parent
                    # holds no blob rows, so the verifier's scope is empty too.
                    parent_blob_refs=frozenset(),
                    data_dir=tmp_path,
                    parent_session_id=parent.id,
                    child_session_id=staged.session.id,
                )
            await race_service.fail_guided_operation(
                fence.guided_fence,
                failure_code="integrity_error",
                actor="composer_route",
                session_operation_context=fence.parent_context,
            )
            await blob_service.cleanup_blobs_for_fork(parent.id, staged.session.id, operation_id)
            assert [item.id for item in await race_service.list_sessions(user_id, "local")] == [parent.id]
            assert (await race_service.get_session(staged.session.id)).archived_at is not None
        else:
            (staged, copied, fence), delete_error = await _fork_first_blob_contention(
                race_service,
                parent.id,
                lambda: reserve_stage_copy(race_service),
                lambda: blob_service.delete_blob(source_blob.id),
            )
            assert isinstance(delete_error, BlobInProgressForkError)
            assert len(staged.blob_plan) == len(copied) == 1
            rewritten = _rewrite_fork_state_blob_custody(
                staged.state,
                copied,
                {source_blob.storage_path: copied[source_blob.id]},
                parent_blob_refs=frozenset({str(source_blob.id), source_blob.storage_path}),
                data_dir=tmp_path,
                parent_session_id=parent.id,
                child_session_id=staged.session.id,
            )
            response_hash = "b" * 64
            settled = await race_service.settle_guided_fork_operation(
                GuidedForkSettlementCommand(
                    authority=staged.authority,
                    expected_current_state_id=staged.state.id,
                    edited_message_id=staged.messages[-1].id,
                    rewritten_state_id=uuid4(),
                    rewritten_state=rewritten,
                    response_hash=response_hash,
                    actor="composer_route",
                )
            )
            assert settled.archived_at is None
            await blob_service.delete_blob(source_blob.id)
            assert await blob_service.list_blobs(parent.id, limit=None) == []
    finally:
        _cleanup_race_user(durable_engine, user_id)


@pytest.mark.asyncio
async def test_fork_rebases_parent_session_sink_paths_into_child_namespace(
    service: SessionServiceImpl,
    tmp_path: Path,
) -> None:
    parent = await service.create_session("alice", "Parent", "local")
    child_id = uuid4()
    parent_output = tmp_path / "outputs" / str(parent.id) / "result.jsonl"
    foreign_id = uuid4()
    foreign_output = tmp_path / "outputs" / str(foreign_id) / "foreign.jsonl"
    state = await _save_composition_state(
        service,
        parent.id,
        CompositionStateData(
            sources={},
            outputs=[
                {"name": "owned", "plugin": "json", "options": {"path": str(parent_output)}},
                {"name": "foreign", "plugin": "json", "options": {"path": str(foreign_output)}},
            ],
        ),
        provenance="session_seed",
    )

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        {},
        {},
        parent_blob_refs=frozenset(),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_id,
    )

    assert rewritten is not None
    assert rewritten.outputs[0]["options"]["path"] == str(tmp_path / "outputs" / str(child_id) / "result.jsonl")
    assert rewritten.outputs[1]["options"]["path"] == str(foreign_output)


@pytest.mark.asyncio
async def test_current_fence_and_concurrent_takeover_reuse_one_hidden_child(durable_engine) -> None:
    """Repeated workers race on one operation but can never publish a second child."""

    race_service = _service_for(durable_engine)
    user_id = f"fork-takeover-race-{uuid4()}"
    parent = await race_service.create_session(user_id, "Parent", "local")
    message = await race_service.add_message(
        parent.id,
        "user",
        "fork here",
        writer_principal="route_user_message",
    )
    operation_id = str(uuid4())
    fence = await _claim_fork(race_service, parent.id, operation_id=operation_id)
    initial = await race_service.fork_session(
        fence,
        fork_message_id=message.id,
        new_message_content="edited",
    )

    async def repeated_stage() -> Any:
        return await race_service.fork_session(
            fence,
            fork_message_id=message.id,
            new_message_content="edited",
        )

    current_barrier = asyncio.Barrier(2)

    async def current_worker() -> Any:
        await current_barrier.wait()
        return await repeated_stage()

    try:
        current_results = await asyncio.gather(current_worker(), current_worker())
        assert {item.session.id for item in current_results} == {initial.session.id}
        _release_fork_authority(race_service, initial.authority)

        with durable_engine.begin() as conn:
            conn.execute(
                update(guided_operations_table)
                .where(
                    guided_operations_table.c.session_id == str(parent.id),
                    guided_operations_table.c.operation_id == operation_id,
                )
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        takeover_parent_context = await race_service._run_sync(
            lambda: race_service.session_operation_authority.acquire(
                session_id=parent.id,
                operation_kind=SessionOperationKind.SESSION_FORK,
                owner_instance_id=race_service._owner_instance_id,
                lease_seconds=race_service._session_operation_lease_seconds,
            )
        )

        takeover_barrier = asyncio.Barrier(2)

        async def takeover_worker() -> Any:
            await takeover_barrier.wait()
            return await race_service.reserve_guided_operation(
                session_id=parent.id,
                operation_id=operation_id,
                kind="session_fork",
                request_hash="a" * 64,
                actor="composer_route",
                lease_seconds=300,
                session_operation_context=takeover_parent_context,
            )

        outcomes = await asyncio.gather(takeover_worker(), takeover_worker())
        takeover = next(item for item in outcomes if isinstance(item, GuidedOperationTakenOver))
        assert sum(isinstance(item, GuidedOperationTakenOver) for item in outcomes) == 1
        assert sum(isinstance(item, GuidedOperationActive) for item in outcomes) == 1
        takeover_authority = SessionForkParentAuthority(
            parent_context=takeover_parent_context,
            guided_fence=takeover.fence,
        )
        resumed = await race_service.fork_session(
            takeover_authority,
            fork_message_id=message.id,
            new_message_content="edited",
        )
        assert resumed.session.id == initial.session.id
        assert resumed.authority.parent.parent_context.fence.operation_epoch > initial.authority.parent.parent_context.fence.operation_epoch
        assert resumed.authority.child_context.fence.operation_epoch > initial.authority.child_context.fence.operation_epoch
        with durable_engine.connect() as conn:
            assert (
                conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.user_id == user_id)).scalar_one() == 2
            )
            operation = conn.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == str(parent.id),
                    guided_operations_table.c.operation_id == operation_id,
                )
            ).one()
            assert operation.result_session_id == str(initial.session.id)
    finally:
        _cleanup_race_user(durable_engine, user_id)


# ---------------------------------------------------------------------------
# elspeth-f478b01787 / elspeth-d178282593 — fork blob custody over the OPEN
# ``composer_meta`` envelope.
#
# ``composer_meta`` is a contractually open JSON envelope: ``merge_composer_meta_updates``
# is required to carry forward keys owned by other subsystems, and no schema
# constrains its key set. The settlement verifier
# (``_verify_fork_settlement_blob_custody``) walks that envelope EXHAUSTIVELY,
# while the fork rewriter enumerates named fields — so the rewriter is
# structurally guaranteed to go stale as new keys appear.
#
# These tests pin the three-way split the rewriter must honour:
#   derived keys  -> RE-DERIVED from the already-rewritten child state
#   owned keys    -> targeted rewrite (already covered above)
#   unknown keys  -> cannot be rewritten safely; fork must FAIL AT THE
#                    REWRITE BOUNDARY naming the key, rather than passing the
#                    leak through to a settlement abort that names nothing.
#
# Honest scope of that last arm: the route has ALREADY committed the staged
# child before the rewriter runs, so a rewrite-boundary failure retains the
# same archived child any failed fork does. What it buys is failing BEFORE
# blob settlement and NAMING the key -- in the error, and in the route's
# ``session.fork_rewrite_integrity_error`` last-resort record.
# ---------------------------------------------------------------------------


def _child_blob_record(*, blob_id: UUID, session_id: UUID, storage_path: str) -> BlobRecord:
    """A minimal ready child-copy blob row, as the fork blob plan produces."""
    return BlobRecord(
        id=blob_id,
        session_id=session_id,
        filename="orders.csv",
        mime_type="text/csv",
        size_bytes=3,
        content_hash="c" * 64,
        storage_path=storage_path,
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
        created_by="user",
        source_description=None,
        status="ready",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


async def _state_for_custody_rewrite(service, *, composer_meta, sources):
    """Persist a parent state carrying ``composer_meta``, ready to be forked.

    ``metadata_`` is populated because a persisted row without it is corruption
    by the codebase's own rule (``converters.state_from_record`` crashes on
    ``None``), so a state lacking it could not represent a forkable session.
    """
    parent = await service.create_session("alice", "Parent", "local")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources=sources,
            metadata_={"name": "Parent pipeline", "description": None},
            composer_meta=composer_meta,
        ),
        provenance="session_seed",
    )
    return parent, state


@pytest.mark.asyncio
async def test_fork_rederives_implicit_decisions_rather_than_copying_the_parent_report(service, tmp_path) -> None:
    """elspeth-f478b01787: ``implicit_decisions`` is a pure PROJECTION of the
    composition state (``build_implicit_decisions_report(state)``), regenerated
    unconditionally on every save. The fork mints a NEW state row, so copying
    the parent's report onto it violates the atomicity contract those saves
    declare — and strands parent blob ids the settlement verifier then rejects.

    Fails before the fix because nothing in the fork path touches
    ``composer_meta.implicit_decisions``: the bare and ``blob:``-prefixed parent
    ids survive verbatim into the child.
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"
    child_storage_path = f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        # Full SourceSpec shape, as ``CompositionState.to_dict`` persists it: the
        # re-derivation reconstructs through the same authority that every normal
        # state read uses (``converters.state_from_record``), so a minimal stub
        # would not represent a state the composer can actually have written.
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob_id), "path": parent_storage_path},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta={
            "implicit_decisions": {
                "schema_version": 1,
                "entries": [
                    {"path": "source.orders.blob_ref", "value": str(parent_blob_id), "category": "blob"},
                    {"path": "source.orders.sentinel", "value": f"blob:{parent_blob_id}", "category": "blob"},
                    {"path": "source.orders.path", "value": parent_storage_path, "category": "blob"},
                ],
                "normalization_events": [],
            }
        },
    )
    child = _child_blob_record(blob_id=child_blob_id, session_id=child_session_id, storage_path=child_storage_path)

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        {parent_blob_id: child},
        {parent_storage_path: child},
        parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_session_id,
    )

    assert rewritten is not None
    assert rewritten.composer_meta is not None
    # Asserted through the production authority, so this test cannot drift from
    # the guard it protects.
    forbidden = frozenset({str(parent_blob_id), parent_storage_path})
    assert not _value_references_parent_blob(rewritten.composer_meta, forbidden)
    # Deleting the key would also satisfy the line above, so pin that the report
    # SURVIVES and names the child's own blob: the contract is re-derivation,
    # not suppression.
    report = rewritten.composer_meta["implicit_decisions"]
    assert report["entries"], "implicit_decisions must be re-derived, not dropped"
    assert _value_references_parent_blob(report, frozenset({str(child_blob_id)}))


@pytest.mark.asyncio
async def test_fork_rewrites_parent_blob_path_nested_inside_source_options(service, tmp_path) -> None:
    """elspeth-f478b01787 (custody-surface sweep): ``_rewrite_source_blob_options``
    iterates ``("path", "file")`` over the TOP LEVEL of a source's options only,
    with no recursion — so a blob path carried by a NESTED option mapping (the
    shape S3-style sources use) is never rebased and leaks parent custody.

    Fails before the fix: ``options.dataset.path`` still names the parent blob.
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"
    child_storage_path = f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        # Full persistable SourceSpec shape, so adding ``implicit_decisions`` to
        # this fixture later exercises re-derivation instead of crashing in
        # ``SourceSpec.from_dict`` for a reason unrelated to the mechanism.
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"dataset": {"path": parent_storage_path}},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta=None,
    )
    child = _child_blob_record(blob_id=child_blob_id, session_id=child_session_id, storage_path=child_storage_path)

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        {parent_blob_id: child},
        {parent_storage_path: child},
        parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_session_id,
    )

    # ``None`` means "nothing needed rebasing" and makes the caller keep the
    # PARENT state — so a None here is the leak itself, not a neutral result.
    assert rewritten is not None, "nested blob path went unrecognised, so the parent state is carried into the child"
    assert not _value_references_parent_blob(rewritten.sources, frozenset({parent_storage_path}))


@pytest.mark.asyncio
async def test_fork_aborts_naming_an_unrecognised_composer_meta_key_that_retains_parent_custody(service, tmp_path) -> None:
    """``composer_meta`` is contractually OPEN — another subsystem may own a key
    the rewriter has never heard of, and the merge contract requires carrying it
    forward. Such a key cannot be rewritten safely (we do not own its meaning),
    so the fork must fail AT THE REWRITE BOUNDARY and NAME the key.

    Before the fix the unknown key sails through untouched and the leak surfaces
    only at settlement, with an error that names nothing. The staged child is
    already committed either way and is retained archived like any failed fork;
    the difference this pins is WHERE the fork fails (before blob settlement)
    and THAT the key is named. The wording must not say "has no fork rewriter":
    a modelled key such as ``guided_session`` can reach here too, through a field
    its rewriter does not rebase.
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob_id)},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta={"some_future_subsystem": {"remembered_blob": str(parent_blob_id)}},
    )
    child = _child_blob_record(
        blob_id=child_blob_id,
        session_id=child_session_id,
        storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv",
    )

    with pytest.raises(
        AuditIntegrityError,
        match="'some_future_subsystem' retains parent blob custody the fork rewriter did not rebase",
    ):
        _rewrite_fork_state_blob_custody(
            state,
            {parent_blob_id: child},
            {parent_storage_path: child},
            parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
            data_dir=tmp_path,
            parent_session_id=parent.id,
            child_session_id=child_session_id,
        )


@pytest.mark.asyncio
async def test_fork_backstop_sees_every_parent_blob_not_only_the_planned_ones(service, tmp_path) -> None:
    """The backstop's needle set is the SETTLEMENT VERIFIER's scope: every parent
    blob row (id and storage_path) regardless of status. The fork plan admits only
    ``status == "ready"`` blobs, so a needle set derived from ``blob_map`` alone
    was blind to a non-ready parent blob referenced from ``composer_meta`` -- that
    residue sailed through and was rejected only at settlement, after staging.

    The route now supplies ``parent_blob_refs`` from ``list_blobs(limit=None)``;
    this pins that a reference the plan does NOT know is still named here.
    """
    planned_parent_id = uuid4()
    unplanned_parent_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    planned_parent_path = f"/var/lib/elspeth/blobs/{uuid4()}/{planned_parent_id}.csv"
    unplanned_parent_path = f"/var/lib/elspeth/blobs/{uuid4()}/{unplanned_parent_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(planned_parent_id)},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta={"some_future_subsystem": {"remembered_blob": str(unplanned_parent_id)}},
    )
    child = _child_blob_record(
        blob_id=child_blob_id,
        session_id=child_session_id,
        storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv",
    )

    with pytest.raises(AuditIntegrityError, match="some_future_subsystem"):
        _rewrite_fork_state_blob_custody(
            state,
            {planned_parent_id: child},
            {planned_parent_path: child},
            parent_blob_refs=frozenset({str(planned_parent_id), planned_parent_path, str(unplanned_parent_id), unplanned_parent_path}),
            data_dir=tmp_path,
            parent_session_id=parent.id,
            child_session_id=child_session_id,
        )


@pytest.mark.parametrize("key_shape", ["id", "sentinel", "storage_path"])
def test_value_references_parent_blob_inspects_mapping_keys(key_shape: str) -> None:
    """Mapping KEYS are custody carriers too. A parent blob id, ``blob:`` sentinel
    or raw storage path used as a dict key names the parent exactly as a value
    does; a walk over ``.values()`` alone lets it cross into the child and be
    served on a 200 (red-team finding B2 on ee1ae108b).
    """
    parent_blob_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"
    key = {
        "id": str(parent_blob_id),
        "sentinel": f"blob:{parent_blob_id}",
        "storage_path": parent_storage_path,
    }[key_shape]
    forbidden = frozenset({str(parent_blob_id), parent_storage_path})

    assert _value_references_parent_blob({"notes_by_blob": {key: "note"}}, forbidden)
    # Same predicate, unrelated key: the key check must not over-match.
    assert not _value_references_parent_blob({"notes_by_blob": {"unrelated": "note"}}, forbidden)


@pytest.mark.asyncio
async def test_fork_backstop_names_a_composer_meta_key_whose_mapping_KEY_is_a_parent_blob(service, tmp_path) -> None:
    """The backstop's detection walk must inspect dict keys with the same shape
    predicate as values, otherwise a parent id used as a KEY inside an unknown
    ``composer_meta`` key passes the rewrite boundary unnamed.
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob_id)},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta={"notes_by_blob": {str(parent_blob_id): "note"}},
    )
    child = _child_blob_record(
        blob_id=child_blob_id,
        session_id=child_session_id,
        storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv",
    )

    with pytest.raises(AuditIntegrityError, match="notes_by_blob"):
        _rewrite_fork_state_blob_custody(
            state,
            {parent_blob_id: child},
            {parent_storage_path: child},
            parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
            data_dir=tmp_path,
            parent_session_id=parent.id,
            child_session_id=child_session_id,
        )


@pytest.mark.asyncio
async def test_fork_rewrites_parent_blob_refs_used_as_mapping_keys_inside_source_options(service, tmp_path) -> None:
    """Detection == correction, for KEYS as well as values: a bare parent id, a
    ``blob:`` sentinel and a raw storage path used as dict keys nested in a
    source options tree are rebased onto the child's values, and the production
    settlement predicate -- itself key-aware -- finds no parent residue.
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"
    child_storage_path = f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "blob_ref": str(parent_blob_id),
                    "per_blob": {
                        str(parent_blob_id): {"weight": 1},
                        f"blob:{parent_blob_id}": {"weight": 2},
                        parent_storage_path: {"weight": 3},
                    },
                },
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta=None,
    )
    child = _child_blob_record(blob_id=child_blob_id, session_id=child_session_id, storage_path=child_storage_path)

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        {parent_blob_id: child},
        {parent_storage_path: child},
        parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_session_id,
    )

    assert rewritten is not None
    assert not _value_references_parent_blob(rewritten.sources, frozenset({str(parent_blob_id), parent_storage_path}))
    per_blob = rewritten.sources["orders"]["options"]["per_blob"]
    assert per_blob == {
        str(child_blob_id): {"weight": 1},
        f"blob:{child_blob_id}": {"weight": 2},
        child_storage_path: {"weight": 3},
    }


@pytest.mark.asyncio
async def test_fork_rewrites_blob_sentinel_over_parent_storage_path_nested_inside_source_options(service, tmp_path) -> None:
    """Detection == correction: ``blob:<parent storage_path>`` is a shape both
    detectors flag (their needle sets include storage paths), so the rebase walk
    must rewrite it to ``blob:<child storage_path>`` rather than leave a detected
    shape for settlement to reject (red-team finding B4 on ee1ae108b).
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"
    child_storage_path = f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob_id), "dataset": {"ref": f"blob:{parent_storage_path}"}},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta=None,
    )
    child = _child_blob_record(blob_id=child_blob_id, session_id=child_session_id, storage_path=child_storage_path)

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        {parent_blob_id: child},
        {parent_storage_path: child},
        parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_session_id,
    )

    assert rewritten is not None
    assert not _value_references_parent_blob(rewritten.sources, frozenset({str(parent_blob_id), parent_storage_path}))
    assert rewritten.sources["orders"]["options"]["dataset"]["ref"] == f"blob:{child_storage_path}"


@pytest.mark.asyncio
async def test_fork_promotes_a_legacy_single_source_row_before_rewriting_and_rederiving(tmp_path) -> None:
    """Mirror ``converters.state_from_record``: a pre-migration row carries its
    source in the legacy ``source`` column with ``sources`` None. Without the
    same promotion the re-derived report loses every source entry, and because
    re-derivation forces ``rewritten``, the child is settled from a
    ``CompositionStateData`` that has no source at all -- the legacy column is
    dropped silently (peer-review Important 3 on ee1ae108b).

    ``save_composition_state`` cannot produce this row (``CompositionStateData``
    promotes ``source`` to ``sources`` on write), so the record is built directly
    with the same dataclass the persistence layer returns.
    """
    parent_session_id = uuid4()
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{parent_session_id}/{parent_blob_id}.csv"
    child_storage_path = f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv"

    record = CompositionStateRecord(
        id=uuid4(),
        session_id=parent_session_id,
        version=1,
        nodes=None,
        edges=None,
        outputs=None,
        metadata_={"name": "Legacy pipeline", "description": None},
        is_valid=False,
        validation_errors=None,
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
        derived_from_state_id=None,
        composer_meta={
            "implicit_decisions": {
                "schema_version": 1,
                "entries": [{"path": "source.blob_ref", "value": str(parent_blob_id), "category": "blob"}],
                "normalization_events": [],
            }
        },
        sources=None,
        source={
            "plugin": "csv",
            "on_success": "rows",
            "options": {"blob_ref": str(parent_blob_id), "path": parent_storage_path},
            "on_validation_failure": "quarantine",
        },
    )
    child = _child_blob_record(blob_id=child_blob_id, session_id=child_session_id, storage_path=child_storage_path)

    rewritten = _rewrite_fork_state_blob_custody(
        record,
        {parent_blob_id: child},
        {parent_storage_path: child},
        parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
        data_dir=tmp_path,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
    )

    assert rewritten is not None
    # The promoted source is what the child carries -- under the converter's key.
    assert rewritten.sources is not None
    assert set(rewritten.sources) == {"source"}
    assert rewritten.sources["source"]["options"]["blob_ref"] == str(child_blob_id)
    forbidden = frozenset({str(parent_blob_id), parent_storage_path})
    assert not _value_references_parent_blob(rewritten.sources, forbidden)
    assert not _value_references_parent_blob(rewritten.composer_meta, forbidden)
    # And the re-derived report was computed FROM the promoted source.
    report = rewritten.composer_meta["implicit_decisions"]
    assert any(entry["path"].startswith("source.") for entry in report["entries"])
    assert _value_references_parent_blob(report, frozenset({str(child_blob_id)}))


@pytest.mark.asyncio
async def test_fork_route_records_the_offending_composer_meta_key_when_custody_detection_refuses_the_fork(tmp_path) -> None:
    """Custody detection's only observable benefit is the KEY it names: the client
    body is the same fixed integrity-error envelope a settlement abort produces,
    and ``fail_guided_operation`` records a code, not a message. So the route
    must write the named key to the last-resort log, or the name is dropped on
    the floor (red-team finding B1 on ee1ae108b). Since pre-staging detection
    landed, an unknown key is refused inside ``fork_session`` before any child
    row exists; the record's fields are the same.
    """
    app, service, blob_service = _make_fork_app(tmp_path)
    parent = await service.create_session("alice", "Parent", "local")
    blob = await blob_service.create_blob(parent.id, "orders.csv", b"id\n1\n", "text/csv")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "orders": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(blob.id), "path": blob.storage_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "Parent pipeline", "description": None},
            composer_meta={"some_future_subsystem": {"remembered_blob": str(blob.id)}},
        ),
        provenance="session_seed",
    )
    message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )
    client = SyncASGITestClient(app, raise_server_exceptions=False)

    with capture_logs() as cap_logs:
        response = client.post(
            f"/api/sessions/{parent.id}/fork",
            json={"operation_id": str(uuid4()), "from_message_id": str(message.id), "new_message_content": "edited"},
        )

    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    records = [entry for entry in cap_logs if entry.get("event") == "session.fork_rewrite_integrity_error"]
    assert len(records) == 1, [entry.get("event") for entry in cap_logs]
    assert records[0]["session_id"] == str(parent.id)
    assert records[0]["exc_class"] == "AuditIntegrityError"
    assert "some_future_subsystem" in records[0]["message"]


# ---------------------------------------------------------------------------
# Mutation pins for the nested rebase walk, the backstop needle set and the
# re-derivation (red-team matrix M5-M8, M11, M12 on ee1ae108b). Every mutation
# below survived the suite before its test landed; each docstring names the
# mutation it kills so a later "simplification" of the walker cannot pass with
# a green suite while breaking the real fork.
# ---------------------------------------------------------------------------


def _blob_rows_entry(blob: BlobRecord) -> dict[str, Any]:
    """One ``options.blobs`` entry exactly as ``BlobRowsEntry`` persists it."""
    return {
        "blob_id": str(blob.id),
        "payload_ref": blob.content_hash,
        "filename": blob.filename,
        "mime_type": blob.mime_type,
        "size_bytes": blob.size_bytes,
    }


def _blob_rows_source(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A persistable ``blob_rows`` SourceSpec carrying ``entries`` in authoring order."""
    return {
        "plugin": "blob_rows",
        "on_success": "rows",
        "options": {"schema": {"mode": "observed"}, "blobs": entries},
        "on_validation_failure": "quarantine",
    }


def _stale_report(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A parent-side ``implicit_decisions`` report the fork must NOT copy."""
    return {"schema_version": 1, "entries": entries, "normalization_events": []}


async def _fork_via_route(app: Any, service: SessionServiceImpl, parent_id: UUID, state_id: UUID) -> tuple[Any, list[dict[str, Any]]]:
    """POST the real fork route from a message bound to ``state_id``; return (response, captured logs)."""
    message = await service.add_message(
        parent_id,
        "user",
        "fork here",
        composition_state_id=state_id,
        writer_principal="route_user_message",
    )
    client = SyncASGITestClient(app, raise_server_exceptions=False)
    with capture_logs() as cap_logs:
        response = client.post(
            f"/api/sessions/{parent_id}/fork",
            json={"operation_id": str(uuid4()), "from_message_id": str(message.id), "new_message_content": "edited"},
        )
    return response, cap_logs


@pytest.mark.asyncio
async def test_fork_rebases_every_blob_id_in_a_blob_rows_source_blob_list(service, tmp_path) -> None:
    """``blob_rows`` persists its custody as ``options.blobs``: a LIST of dicts each
    carrying a bare ``blob_id`` (``plugins/sources/blob_rows.py::BlobRowsEntry``).
    ``_rewrite_source_blob_options`` enumerates top-level carriers only, so the
    nested walk's list branch and bare-id branch are the ONLY thing that rebases
    a blob_rows source. Dropping either (red-team M7: lists returned unchanged;
    M6: bare-id branch removed) kept the suite green while the real blob_rows
    fork died at settlement -- this pins both, with TWO blobs so a walk that
    rebases only the first entry is caught too.
    """
    parent_a, parent_b = uuid4(), uuid4()
    child_a, child_b = uuid4(), uuid4()
    child_session_id = uuid4()
    parent_dir = uuid4()
    parent_paths = {pid: f"/var/lib/elspeth/blobs/{parent_dir}/{pid}.csv" for pid in (parent_a, parent_b)}
    children = {
        parent_a: _child_blob_record(
            blob_id=child_a, session_id=child_session_id, storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_a}.csv"
        ),
        parent_b: _child_blob_record(
            blob_id=child_b, session_id=child_session_id, storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_b}.csv"
        ),
    }
    entries = [
        {"blob_id": str(parent_a), "payload_ref": "a" * 64, "filename": "orders-a.csv", "mime_type": "text/csv", "size_bytes": 3},
        {"blob_id": str(parent_b), "payload_ref": "b" * 64, "filename": "orders-b.csv", "mime_type": "text/csv", "size_bytes": 4},
    ]
    parent, state = await _state_for_custody_rewrite(
        service,
        sources={"docs": _blob_rows_source(entries)},
        composer_meta={"implicit_decisions": _stale_report([{"path": "source.blobs", "value": entries, "category": "blob"}])},
    )

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        children,
        {parent_paths[pid]: children[pid] for pid in children},
        parent_blob_refs=frozenset({str(parent_a), str(parent_b), *parent_paths.values()}),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_session_id,
    )

    assert rewritten is not None, "a blob_rows source went unrecognised, so the parent state is carried into the child"
    forbidden = frozenset({str(parent_a), str(parent_b), *parent_paths.values()})
    assert not _value_references_parent_blob(rewritten.sources, forbidden)
    assert not _value_references_parent_blob(rewritten.composer_meta, forbidden)
    # Order, cardinality and every non-custody field are preserved; ONLY the
    # blob_id is rebased, and each entry lands on its own child copy. (The
    # returned CompositionStateData is deep-frozen: lists come back as tuples.)
    assert deep_thaw(rewritten.sources)["docs"]["options"]["blobs"] == [
        {**entries[0], "blob_id": str(child_a)},
        {**entries[1], "blob_id": str(child_b)},
    ]
    # A nested carrier does not re-bind the source: blob_rows has no blob_ref.
    assert "blob_ref" not in rewritten.sources["docs"]["options"]
    # And the re-derived report names both child blobs, not the parent's.
    report = rewritten.composer_meta["implicit_decisions"]
    assert _value_references_parent_blob(report, frozenset({str(child_a)}))
    assert _value_references_parent_blob(report, frozenset({str(child_b)}))


@pytest.mark.asyncio
async def test_fork_route_settles_a_blob_rows_source_with_two_parent_blobs(tmp_path) -> None:
    """The same blob_rows pin through the REAL route: staging, blob copy, rewrite
    and the settlement verifier. Under red-team M6 / M7 this POST answers 500
    ``integrity_error`` (settlement finds the parent ids the walker left behind);
    on the fixed tree it settles and the child names only its own copies.
    """
    app, service, blob_service = _make_fork_app(tmp_path)
    parent = await service.create_session("alice", "Parent", "local")
    blob_a = await blob_service.create_blob(parent.id, "orders-a.csv", b"id\n1\n", "text/csv")
    blob_b = await blob_service.create_blob(parent.id, "orders-b.csv", b"id\n2\n", "text/csv")
    entries = [_blob_rows_entry(blob_a), _blob_rows_entry(blob_b)]
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={"docs": _blob_rows_source(entries)},
            metadata_={"name": "Parent pipeline", "description": None},
            composer_meta={"implicit_decisions": _stale_report([{"path": "source.blobs", "value": entries, "category": "blob"}])},
        ),
        provenance="session_seed",
    )

    response, _ = await _fork_via_route(app, service, parent.id, state.id)

    assert response.status_code == 201, response.text
    child_id = UUID(response.json()["session_id"])
    child_blobs = {blob.filename: blob for blob in await blob_service.list_blobs(child_id, limit=None)}
    assert set(child_blobs) == {"orders-a.csv", "orders-b.csv"}
    child_state = await service.get_current_state(child_id)
    assert child_state is not None
    forbidden = frozenset({str(blob_a.id), blob_a.storage_path, str(blob_b.id), blob_b.storage_path})
    assert not _value_references_parent_blob(child_state.sources, forbidden)
    assert not _value_references_parent_blob(child_state.composer_meta, forbidden)
    assert deep_thaw(child_state.sources)["docs"]["options"]["blobs"] == [
        {**entries[0], "blob_id": str(child_blobs["orders-a.csv"].id)},
        {**entries[1], "blob_id": str(child_blobs["orders-b.csv"].id)},
    ]


@pytest.mark.asyncio
async def test_fork_rewrites_blob_sentinel_over_parent_id_nested_inside_source_options(service, tmp_path) -> None:
    """A ``blob:<parent id>`` sentinel carried as a nested option VALUE is a shape
    both detectors flag, so the rebase walk must rewrite it to ``blob:<child id>``.
    Red-team M5 (sentinel-id branch dropped) survived because the only sentinel
    fixtures sat at the top level, where ``_rewrite_source_blob_options`` handles
    them by name.
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"
    child_storage_path = f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob_id), "dataset": {"ref": f"blob:{parent_blob_id}"}},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta=None,
    )
    child = _child_blob_record(blob_id=child_blob_id, session_id=child_session_id, storage_path=child_storage_path)

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        {parent_blob_id: child},
        {parent_storage_path: child},
        parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_session_id,
    )

    assert rewritten is not None
    assert not _value_references_parent_blob(rewritten.sources, frozenset({str(parent_blob_id), parent_storage_path}))
    assert rewritten.sources["orders"]["options"]["dataset"]["ref"] == f"blob:{child_blob_id}"


@pytest.mark.asyncio
async def test_fork_backstop_names_a_key_that_retains_only_a_parent_storage_path(service, tmp_path) -> None:
    """The backstop's needles are ids AND storage paths. A residue that carries
    only the parent's raw path (no id anywhere) must still be named at the
    rewrite boundary; with an ids-only needle set (red-team M8) it fell through
    to settlement, which names nothing.
    """
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob_id}.csv"

    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob_id)},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta={"some_future_subsystem": {"remembered_path": parent_storage_path}},
    )
    child = _child_blob_record(
        blob_id=child_blob_id,
        session_id=child_session_id,
        storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv",
    )

    with pytest.raises(AuditIntegrityError, match="'some_future_subsystem' retains parent blob custody"):
        _rewrite_fork_state_blob_custody(
            state,
            {parent_blob_id: child},
            {parent_storage_path: child},
            parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
            data_dir=tmp_path,
            parent_session_id=parent.id,
            child_session_id=child_session_id,
        )


@pytest.mark.asyncio
async def test_fork_route_refuses_a_path_only_residue_in_an_unknown_key_by_name(tmp_path) -> None:
    """Red-team M8 at its post-F1 home: the forbidden set is every parent blob's
    id AND storage_path. Drop the path and a path-only residue in an unknown key
    passes detection and is rejected only at settlement -- same 500, but the
    last-resort record then carries the settlement message instead of the
    offending key's name. The key name is the assertion.
    """
    app, service, blob_service = _make_fork_app(tmp_path)
    parent = await service.create_session("alice", "Parent", "local")
    blob = await blob_service.create_blob(parent.id, "orders.csv", b"id\n1\n", "text/csv")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "orders": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(blob.id), "path": blob.storage_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "Parent pipeline", "description": None},
            composer_meta={"some_future_subsystem": {"remembered_path": blob.storage_path}},
        ),
        provenance="session_seed",
    )

    response, cap_logs = await _fork_via_route(app, service, parent.id, state.id)

    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    records = [entry for entry in cap_logs if entry.get("event") == "session.fork_rewrite_integrity_error"]
    assert len(records) == 1, [entry.get("event") for entry in cap_logs]
    assert "'some_future_subsystem' retains parent blob custody" in records[0]["message"]


@pytest.mark.asyncio
async def test_fork_route_backstop_needles_span_more_parent_blobs_than_one_list_page(tmp_path) -> None:
    """Red-team MK5 on the F1 route hunk, kept alive after pre-staging detection
    took over the unknown-key class. ``list_blobs`` defaults to the 50 NEWEST
    rows and the route must ask for ``limit=None``. The residue here sits inside
    a MODELLED key -- ``guided_session.reviewed_sources[...].options.dataset.path``
    naming the OLDEST parent blob, non-ready so outside the plan -- which
    pre-staging detection deliberately skips (the guided rewriter owns that key)
    and the nested rebase cannot correct (no child copy exists). Only the
    rewrite-boundary backstop can name it, and only if its needles span every
    parent row: with the default page the 54th row is absent, the backstop
    passes it, and settlement rejects it after staging, unnamed. Every other pin
    uses fewer than 50 parent blobs, so that mutation survived the suite. The
    fixture depends on pre-staging detection SKIPPING ``guided_session``: if that
    skip were removed, this test would fail for the wrong reason (refused before
    staging, no ``'guided_session'`` backstop text).
    """
    from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
    from elspeth.web.composer.guided.resolved import SourceResolved
    from elspeth.web.composer.guided.state_machine import GuidedSession, TurnRecord

    app, service, blob_service = _make_fork_app(tmp_path)
    parent = await service.create_session("alice", "Parent", "local")
    root = await service.add_message(parent.id, "user", "root", writer_principal="route_user_message")
    ready_blob = await blob_service.create_blob(parent.id, "orders.csv", b"id\n1\n", "text/csv")
    oldest_pending_blob = await blob_service.create_blob(parent.id, "oldest.csv", b"id\n2\n", "text/csv")
    with service._engine.begin() as conn:
        conn.execute(
            update(blobs_table)
            .where(blobs_table.c.id == str(oldest_pending_blob.id))
            .values(status="pending", created_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    newer_than_any_upload = datetime.now(UTC) + timedelta(hours=1)
    for offset in range(52):
        _insert_blob_row(
            service._engine,
            blob_id=uuid4(),
            session_id=parent.id,
            status="error",
            created_at=newer_than_any_upload + timedelta(seconds=offset),
            mime_type="text/csv",
        )
    default_page = await blob_service.list_blobs(parent.id)
    assert len(default_page) == 50
    assert oldest_pending_blob.id not in {blob.id for blob in default_page}
    assert len(await blob_service.list_blobs(parent.id, limit=None)) == 54
    stable_id = str(uuid4())
    # The reviewed snapshot binds the READY blob (rebased by the plan) and nests
    # the OLDEST non-ready blob's path where only the walkers can see it.
    snapshot_options = {
        "path": ready_blob.storage_path,
        "blob_ref": str(ready_blob.id),
        "schema": {"mode": "observed"},
        "dataset": {"path": oldest_pending_blob.storage_path},
    }
    live_options = {"path": ready_blob.storage_path, "schema": {"mode": "observed"}}
    guided = GuidedSession(
        step=GuidedStep.STEP_2_SINK,
        history=(
            TurnRecord(
                step=GuidedStep.STEP_2_SINK,
                turn_type=TurnType.INSPECT_AND_CONFIRM,
                payload_hash="a" * 64,
                response_hash=None,
                emitter="server",
            ),
        ),
        source_order=(stable_id,),
        reviewed_sources={
            stable_id: SourceResolved(
                name="orders",
                plugin="csv",
                options=snapshot_options,
                observed_columns=("id",),
                sample_rows=({"id": 1},),
                on_validation_failure="discard",
            )
        },
        root_intent_message_id=str(root.id),
    )
    state_data = CompositionStateData(
        sources={"orders": {"plugin": "csv", "on_success": "out", "options": dict(live_options), "on_validation_failure": "discard"}},
        nodes=[],
        edges=[],
        outputs=[],
        metadata_={"name": "Guided", "description": ""},
        is_valid=True,
        composer_meta={"guided_session": guided.to_dict()},
    )
    state = await service.save_composition_state(parent.id, state_data, provenance="session_seed")
    await _complete_guided_start_authority(service, session_id=parent.id, root_message=root, state=state, state_data=state_data)

    response, cap_logs = await _fork_via_route(app, service, parent.id, state.id)

    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    records = [entry for entry in cap_logs if entry.get("event") == "session.fork_rewrite_integrity_error"]
    assert len(records) == 1, [entry.get("event") for entry in cap_logs]
    assert "'guided_session' retains parent blob custody" in records[0]["message"]


@pytest.mark.asyncio
async def test_fork_rederives_the_report_even_when_no_blob_custody_needed_rebasing(service, tmp_path) -> None:
    """Re-derivation is itself a rewrite. A parent with an ``implicit_decisions``
    report but NO blob-bearing source (empty plan, empty needle set) still mints
    a new state row on fork, so the child must carry a report derived from ITS
    state -- not the parent's staged copy. Returning ``None`` here (red-team M11:
    re-derivation does not set ``rewritten``) makes the caller keep the parent
    state verbatim, stale report included.
    """
    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "/srv/data/orders.csv"},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta={
            "validation_lane": "strict",
            "implicit_decisions": _stale_report([{"path": "source.stale", "value": "STALE-PARENT-ENTRY", "category": "blob"}]),
        },
    )

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        {},
        {},
        parent_blob_refs=frozenset(),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=uuid4(),
    )

    assert rewritten is not None, "None carries the PARENT state -- and its stale report -- into the child"
    assert rewritten.composer_meta is not None
    report = rewritten.composer_meta["implicit_decisions"]
    assert all(entry["value"] != "STALE-PARENT-ENTRY" for entry in report["entries"])
    assert any(entry["path"] == "source.path" and entry["value"] == "/srv/data/orders.csv" for entry in report["entries"])
    # Unrelated keys ride along untouched: re-derivation replaces one key, not the envelope.
    assert rewritten.composer_meta["validation_lane"] == "strict"


def test_fork_refuses_to_rederive_a_report_for_a_row_that_carries_no_metadata(tmp_path) -> None:
    """Tier-1 posture mirrors ``converters.state_from_record``: a persisted row
    with ``metadata_`` None is corruption, and fabricating metadata to re-derive
    the disclosure would hide it. Removing the guard (red-team M12) lets the
    reconstruction crash on its own -- an ``operation_failed`` with no audit
    name instead of the named ``integrity_error``.

    ``save_composition_state`` cannot produce this row, so the record is built
    directly with the dataclass the persistence layer returns.
    """
    parent_session_id = uuid4()
    parent_blob_id = uuid4()
    child_blob_id = uuid4()
    child_session_id = uuid4()
    parent_storage_path = f"/var/lib/elspeth/blobs/{parent_session_id}/{parent_blob_id}.csv"

    record = CompositionStateRecord(
        id=uuid4(),
        session_id=parent_session_id,
        version=1,
        nodes=None,
        edges=None,
        outputs=None,
        metadata_=None,
        is_valid=False,
        validation_errors=None,
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
        derived_from_state_id=None,
        composer_meta={
            "implicit_decisions": _stale_report([{"path": "source.blob_ref", "value": str(parent_blob_id), "category": "blob"}])
        },
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob_id), "path": parent_storage_path},
                "on_validation_failure": "quarantine",
            }
        },
        source=None,
    )
    child = _child_blob_record(
        blob_id=child_blob_id,
        session_id=child_session_id,
        storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob_id}.csv",
    )

    with pytest.raises(AuditIntegrityError, match="carries no metadata to re-derive its disclosure from"):
        _rewrite_fork_state_blob_custody(
            record,
            {parent_blob_id: child},
            {parent_storage_path: child},
            parent_blob_refs=frozenset({str(parent_blob_id), parent_storage_path}),
            data_dir=tmp_path,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
        )


# ---------------------------------------------------------------------------
# Round-two pins on d92f70d78 (red-team F1, systems A/C): the backstop tests
# the TOP-LEVEL composer_meta key itself, walks ``validation_errors``, and
# shares the settlement verifier's predicate instead of restating it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key_shape", ["id", "sentinel", "storage_path"])
@pytest.mark.asyncio
async def test_fork_backstop_names_a_top_level_composer_meta_key_that_is_itself_a_parent_blob_ref(service, tmp_path, key_shape) -> None:
    """Round-two red-team F1 on d92f70d78. The backstop iterated
    ``composer_meta.items()`` and handed only the VALUE to the walker, so a
    parent blob id, ``blob:`` sentinel or raw storage path used as the top-level
    key passed unnamed and died at settlement -- the exact class the commit
    claimed closed. The key must be tested like any nested key, and the error
    must name it.
    """
    parent_blob, child_blob, child_session_id = uuid4(), uuid4(), uuid4()
    parent_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob}.csv"
    children = {
        parent_blob: _child_blob_record(
            blob_id=child_blob, session_id=child_session_id, storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob}.csv"
        )
    }
    key = {"id": str(parent_blob), "sentinel": f"blob:{parent_blob}", "storage_path": parent_path}[key_shape]
    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob), "path": parent_path},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta={key: {"note": "x"}},
    )

    with pytest.raises(AuditIntegrityError, match=f"composer_meta key {re.escape(repr(key))} retains parent blob custody"):
        _rewrite_fork_state_blob_custody(
            state,
            children,
            {parent_path: children[parent_blob]},
            parent_blob_refs=frozenset({str(parent_blob), parent_path}),
            data_dir=tmp_path,
            parent_session_id=parent.id,
            child_session_id=child_session_id,
        )


@pytest.mark.parametrize("entry_shape", ["exact", "embedded"])
@pytest.mark.asyncio
async def test_fork_backstop_names_validation_errors_that_retain_a_parent_storage_path(service, tmp_path, entry_shape) -> None:
    """Round-two systems finding C: ``validation_errors`` is persisted and served
    on GET /state, is copied verbatim into the child, and was the only state
    column outside every custody walker. No rewriter can rebase free text, so
    the backstop must refuse the fork and NAME the column, exactly as it does
    for an unmodelled ``composer_meta`` key.
    """
    parent_blob, child_blob, child_session_id = uuid4(), uuid4(), uuid4()
    parent_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob}.csv"
    children = {
        parent_blob: _child_blob_record(
            blob_id=child_blob, session_id=child_session_id, storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob}.csv"
        )
    }
    parent = await service.create_session("alice", "Parent", "local")
    state = await service.save_composition_state(
        parent.id,
        CompositionStateData(
            sources={
                "orders": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"blob_ref": str(parent_blob), "path": parent_path},
                    "on_validation_failure": "quarantine",
                }
            },
            metadata_={"name": "Parent pipeline", "description": None},
            is_valid=False,
            validation_errors=[parent_path if entry_shape == "exact" else f"source file not found: {parent_path}"],
        ),
        provenance="session_seed",
    )

    with pytest.raises(AuditIntegrityError, match="validation_errors retains parent blob custody"):
        _rewrite_fork_state_blob_custody(
            state,
            children,
            {parent_path: children[parent_blob]},
            parent_blob_refs=frozenset({str(parent_blob), parent_path}),
            data_dir=tmp_path,
            parent_session_id=parent.id,
            child_session_id=child_session_id,
        )


def test_fork_backstop_shares_the_settlement_verifiers_reference_predicate() -> None:
    """Tripwire (round-two systems finding A). d92f70d78 made the backstop's
    needle set equal to the settlement verifier's, which removed the only
    signal that had revealed drift between the two -- and left two predicates
    that agreed by comment alone (no test called the route's copy). Drift
    becomes impossible rather than untested only if there is ONE predicate: the
    route imports the verifier's. A second definition of "references a parent
    blob" in the route module fails this test.
    """
    import elspeth.web.sessions.routes.sessions as fork_routes
    from elspeth.web.sessions import service as sessions_service

    assert fork_routes._value_references_parent_blob is sessions_service._value_references_parent_blob
    assert "_contains_exact_string" not in dir(fork_routes)


@pytest.mark.asyncio
async def test_fork_rewriter_changes_exactly_the_composer_meta_keys_declared_as_rewritten(service, tmp_path) -> None:
    """Tripwire (round-two systems sign-off, section 2). Pre-staging detection
    skips the keys in ``FORK_REWRITTEN_COMPOSER_META_KEYS`` because their
    rewriters run later; the rewriter identifies those same keys by literal in
    another module. Nothing bound the two, so a key declared without a rewriter
    would silently regress to the post-staging orphan, and a rewriter added
    without a declaration would refuse every fork that needs it. This test
    derives the set from BEHAVIOUR: a parent state whose every key names its
    blob is rewritten, and the keys whose value changed must equal the declared
    set exactly -- in both directions.
    """
    from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
    from elspeth.web.composer.guided.resolved import SourceResolved
    from elspeth.web.composer.guided.state_machine import GuidedSession, TurnRecord
    from elspeth.web.sessions.service import FORK_REWRITTEN_COMPOSER_META_KEYS

    parent_blob, child_blob, child_session_id = uuid4(), uuid4(), uuid4()
    parent_path = f"/var/lib/elspeth/blobs/{uuid4()}/{parent_blob}.csv"
    children = {
        parent_blob: _child_blob_record(
            blob_id=child_blob, session_id=child_session_id, storage_path=f"/var/lib/elspeth/blobs/{child_session_id}/{child_blob}.csv"
        )
    }
    stable_id = str(uuid4())
    guided = GuidedSession(
        step=GuidedStep.STEP_2_SINK,
        history=(
            TurnRecord(
                step=GuidedStep.STEP_2_SINK,
                turn_type=TurnType.INSPECT_AND_CONFIRM,
                payload_hash="a" * 64,
                response_hash=None,
                emitter="server",
            ),
        ),
        source_order=(stable_id,),
        reviewed_sources={
            stable_id: SourceResolved(
                name="orders",
                plugin="csv",
                options={"path": parent_path, "blob_ref": str(parent_blob), "schema": {"mode": "observed"}},
                observed_columns=("id",),
                sample_rows=({"id": 1},),
                on_validation_failure="discard",
            )
        },
        root_intent_message_id=str(uuid4()),
    )
    # Every key present, custody-bearing or inert, so the changed-key set is a
    # statement about the rewriter and not about which keys happened to exist.
    composer_meta = {
        "guided_session": guided.to_dict(),
        "implicit_decisions": _stale_report([{"path": "source.blob_ref", "value": str(parent_blob), "category": "source"}]),
        "validation_lane": "strict",
        "guided_completed_terminal_before_user_exit": False,
        "completion_gates": {"graph_fingerprint": "f" * 64},
    }
    parent, state = await _state_for_custody_rewrite(
        service,
        sources={
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"blob_ref": str(parent_blob), "path": parent_path},
                "on_validation_failure": "quarantine",
            }
        },
        composer_meta=composer_meta,
    )

    rewritten = _rewrite_fork_state_blob_custody(
        state,
        children,
        {parent_path: children[parent_blob]},
        parent_blob_refs=frozenset({str(parent_blob), parent_path}),
        data_dir=tmp_path,
        parent_session_id=parent.id,
        child_session_id=child_session_id,
    )

    assert rewritten is not None
    before = deep_thaw(state.composer_meta)
    after = deep_thaw(rewritten.composer_meta)
    assert set(before) == set(after) == set(composer_meta), "the rewriter must neither add nor drop composer_meta keys"
    changed = {key for key in composer_meta if before[key] != after[key]}
    assert changed == FORK_REWRITTEN_COMPOSER_META_KEYS


async def _parent_with_guided_root_fork_message(service: SessionServiceImpl):
    """A parent whose pre-send state carries a guided session bound to a root intent."""
    from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
    from elspeth.web.composer.guided.state_machine import GuidedSession, TurnRecord

    parent = await service.create_session("alice", "Parent", "local")
    root = await service.add_message(parent.id, "user", "root", writer_principal="route_user_message")
    guided = GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        history=(
            TurnRecord(
                step=GuidedStep.STEP_1_SOURCE,
                turn_type=TurnType.INSPECT_AND_CONFIRM,
                payload_hash="a" * 64,
                response_hash=None,
                emitter="server",
            ),
        ),
        root_intent_message_id=str(root.id),
    )
    from tests.unit.web.sessions.test_fork import _complete_guided_start_authority

    state_data = CompositionStateData(
        sources={},
        nodes=[],
        edges=[],
        outputs=[],
        metadata_={"name": "Guided", "description": ""},
        is_valid=True,
        composer_meta={"guided_session": guided.to_dict()},
    )
    state = await _save_composition_state(service, parent.id, state_data, provenance="session_seed")
    await _complete_guided_start_authority(
        service,
        session_id=parent.id,
        root_message=root,
        state=state,
        state_data=state_data,
    )
    fork_message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        composition_state_id=state.id,
        writer_principal="route_user_message",
    )
    return parent, fork_message


@pytest.mark.asyncio
async def test_settled_fork_authority_requires_completed_parent_bound_to_child(
    service: SessionServiceImpl,
) -> None:
    """The settled-authority check is the post-completion form: live session
    fences plus a parent fork row already ``completed`` for exactly this child.

    Before settlement the parent row is still ``in_progress``, so the settled
    check must refuse (fail closed) even though the live-lease check accepts.
    """
    parent, fork_message = await _parent_with_guided_root_fork_message(service)
    parent_authority = await _claim_dual_fenced_fork(service, parent.id)
    staged = await service.fork_session(
        parent_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    try:
        parent_id, child_id = str(parent.id), str(staged.session.id)

        def _check_before_settlement() -> None:
            with service._session_pair_locked_begin(parent_id, child_id) as conn:
                service._require_session_fork_authority_on_connection(conn, staged.authority)
                with pytest.raises(GuidedOperationFenceLostError):
                    service._require_settled_session_fork_authority_on_connection(conn, staged.authority)

        await service._run_sync(_check_before_settlement)
    finally:
        _release_fork_authority(service, staged.authority)


@pytest.mark.asyncio
async def test_settling_a_guided_root_fork_records_the_child_terminal_event_after_parent_completion(
    service: SessionServiceImpl,
    engine,
) -> None:
    """Settlement completes the parent fork row first (so a later failure rolls
    it back atomically) and then records the child's synthetic ``guided_start``
    completion under the settled form of the fork authority. Demanding a live
    guided lease there refused every guided-root fork and left replay joiners
    polling until lease expiry.
    """
    parent, fork_message = await _parent_with_guided_root_fork_message(service)
    parent_authority = await _claim_dual_fenced_fork(service, parent.id)
    staged = await service.fork_session(
        parent_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )
    assert staged.state is not None
    try:
        settled = await service.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=staged.authority,
                expected_current_state_id=staged.state.id,
                edited_message_id=staged.messages[-1].id,
                rewritten_state_id=None,
                rewritten_state=None,
                response_hash="b" * 64,
                actor="composer_route",
            )
        )
    finally:
        _release_fork_authority(service, staged.authority)
    assert settled.id == staged.session.id

    with engine.connect() as conn:
        parent_row = conn.execute(
            select(guided_operations_table).where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == parent_authority.guided_fence.operation_id,
            )
        ).one()
        assert parent_row.status == "completed"
        assert parent_row.result_session_id == str(settled.id)
        child_start = conn.execute(
            select(guided_operations_table).where(
                guided_operations_table.c.session_id == str(settled.id),
                guided_operations_table.c.kind == "guided_start",
            )
        ).one()
        assert child_start.status == "completed"
        events = conn.execute(
            select(guided_operation_events_table.c.event_kind).where(
                guided_operation_events_table.c.session_id == str(settled.id),
                guided_operation_events_table.c.operation_id == child_start.operation_id,
            )
        ).all()
        assert [event.event_kind for event in events] == ["completed"]
