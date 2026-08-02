"""Fenced session-fork staging, takeover, settlement, and archive exclusion."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.blobs.protocol import BlobForkFenceLostError, BlobInProgressForkError, fork_blob_id
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
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
from elspeth.web.sessions.service import SessionServiceImpl, _fork_blob_plan_from_content, _GuidedSessionMutations
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness


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
            session_operation_context=context,
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
        await blob_service.delete_blob(
            blob_id,
            session_operation_context=context,
        )
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
    """Hold blob deletion's source lock while fork reservation contends on it."""

    reserve_authority = reserve_service.session_operation_authority
    original_authority_lock = reserve_authority._locked_transaction
    original_release = reserve_authority.release
    held = threading.Event()
    release = threading.Event()
    reserve_waiting = threading.Event()
    reserve_acquired = threading.Event()
    release_thread = threading.local()

    @contextlib.contextmanager
    def controlled_authority_lock(locked_session_id: str):
        target = locked_session_id == str(session_id)
        if target and getattr(release_thread, "active", False):
            with original_authority_lock(locked_session_id) as conn:
                held.set()
                assert release.wait(timeout=10)
                yield conn
            return

        if target and held.is_set():
            reserve_waiting.set()
        with original_authority_lock(locked_session_id) as conn:
            if target and held.is_set():
                reserve_acquired.set()
            yield conn

    def controlled_release(context: Any) -> None:
        release_thread.active = True
        try:
            original_release(context)
        finally:
            release_thread.active = False

    with (
        patch.object(reserve_authority, "_locked_transaction", new=controlled_authority_lock),
        patch.object(reserve_authority, "release", new=controlled_release),
    ):
        delete_task = asyncio.create_task(delete_first())
        assert await asyncio.to_thread(held.wait, 10)
        reserve_task = asyncio.create_task(reserve_second())
        assert await asyncio.to_thread(reserve_waiting.wait, 10)
        was_blocked = not reserve_acquired.is_set()
        release.set()
        results = tuple(await asyncio.gather(delete_task, reserve_task, return_exceptions=True))
        assert was_blocked
        return results  # type: ignore[return-value]


async def _fork_first_blob_contention(
    fork_service: SessionServiceImpl,
    session_id: UUID,
    fork_first: Callable[[], Awaitable[Any]],
    delete_second: Callable[[], Awaitable[Any]],
) -> tuple[Any, Any]:
    """Hold fork reservation's source lock while blob deletion contends on it."""

    fork_authority = fork_service.session_operation_authority
    original_authority_lock = fork_authority._locked_transaction
    original_acquire = fork_authority.acquire
    held = threading.Event()
    release = threading.Event()
    delete_waiting = threading.Event()
    delete_acquired = threading.Event()
    paused = False
    acquire_thread = threading.local()

    @contextlib.contextmanager
    def controlled_authority_lock(locked_session_id: str):
        nonlocal paused
        target = locked_session_id == str(session_id)
        fork_acquire = getattr(acquire_thread, "operation_kind", None) is SessionOperationKind.SESSION_FORK
        if target and fork_acquire and not paused:
            paused = True
            with original_authority_lock(locked_session_id) as conn:
                paused = True
                held.set()
                assert release.wait(timeout=10)
                yield conn
            return

        if target and held.is_set():
            delete_waiting.set()
        with original_authority_lock(locked_session_id) as conn:
            if target and held.is_set():
                delete_acquired.set()
            yield conn

    def controlled_acquire(**kwargs: Any) -> Any:
        acquire_thread.operation_kind = kwargs.get("operation_kind")
        try:
            return original_acquire(**kwargs)
        finally:
            acquire_thread.operation_kind = None

    with (
        patch.object(fork_authority, "_locked_transaction", new=controlled_authority_lock),
        patch.object(fork_authority, "acquire", new=controlled_acquire),
    ):
        fork_task = asyncio.create_task(fork_first())
        assert await asyncio.to_thread(held.wait, 10)
        delete_task = asyncio.create_task(delete_second())
        assert await asyncio.to_thread(delete_waiting.wait, 10)
        was_blocked = not delete_acquired.is_set()
        release.set()
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


@pytest.mark.asyncio
async def test_fork_blob_copy_requires_live_parent_child_and_guided_composite(
    service: SessionServiceImpl,
    engine,
    tmp_path: Path,
) -> None:
    parent = await service.create_session("alice", "Parent", "local")
    blob_service = BlobServiceImpl(engine, tmp_path / "dual-fenced-blobs")
    source = await _create_test_blob(service, blob_service, parent.id, "source.csv", b"a,b\n1,2\n", "text/csv")
    fork_message = await service.add_message(
        parent.id,
        "user",
        "fork here",
        writer_principal="route_user_message",
    )
    parent_authority = await _claim_dual_fenced_fork(service, parent.id)
    staged = await service.fork_session(
        parent_authority,
        fork_message_id=fork_message.id,
        new_message_content="edited",
    )

    async def checkpoint() -> None:
        return None

    copied = await blob_service.copy_blobs_for_fork(
        parent.id,
        staged.session.id,
        staged.blob_plan,
        staged.authority,
        checkpoint=checkpoint,
    )
    assert copied[source.id].session_id == staged.session.id

    stale_parent_context = replace(
        staged.authority.parent.parent_context,
        fence=replace(
            staged.authority.parent.parent_context.fence,
            lease_token="stale-parent-token",
        ),
    )
    stale = replace(
        staged.authority,
        parent=replace(
            staged.authority.parent,
            parent_context=stale_parent_context,
        ),
    )
    with pytest.raises(BlobForkFenceLostError):
        await blob_service.copy_blobs_for_fork(
            parent.id,
            staged.session.id,
            staged.blob_plan,
            stale,
            checkpoint=checkpoint,
        )


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
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(session_id),
                filename=f"{blob_id}.bin",
                mime_type="application/octet-stream",
                size_bytes=size_bytes,
                content_hash=content_hash,
                storage_path=storage_path or f"/tmp/{blob_id}.bin",
                created_at=datetime.now(UTC),
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
                    "source_storage_path": f"/data/blobs/{source_session_id}/{source_blob_id}_source.csv",
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
    source_blob = await _create_test_blob(
        race_service,
        blob_service,
        parent.id,
        "source.csv",
        b"a,b\n1,2\n",
        "text/csv",
    )
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
            staged.authority,
            checkpoint=checkpoint,
        )
        return staged, copied, fence

    try:
        if winner == "delete":
            deleted, staged_result = await _blob_delete_first_contention(
                race_service,
                parent.id,
                lambda: _delete_test_blob(race_service, blob_service, parent.id, source_blob.id),
                lambda: reserve_stage_copy(race_service),
            )
            assert deleted is None
            assert not isinstance(staged_result, BaseException)
            staged, copied, _fence = staged_result
            assert staged.blob_plan == ()
            assert copied == {}
            with pytest.raises(AuditIntegrityError, match="absent from the frozen fork plan"):
                _rewrite_fork_state_blob_custody(
                    staged.state,
                    copied,
                    {},
                    data_dir=tmp_path,
                    parent_session_id=parent.id,
                    child_session_id=staged.session.id,
                )
            await race_service.fail_guided_fork_operation(
                staged.authority,
                failure_code="integrity_error",
                actor="composer_route",
            )
            await blob_service.cleanup_blobs_for_fork(staged.authority)
            assert [item.id for item in await race_service.list_sessions(user_id, "local")] == [parent.id]
            assert (await race_service.get_session(staged.session.id)).archived_at is not None
        else:
            (staged, copied, _fence), delete_error = await _fork_first_blob_contention(
                race_service,
                parent.id,
                lambda: reserve_stage_copy(race_service),
                lambda: _delete_test_blob(race_service, blob_service, parent.id, source_blob.id),
            )
            assert isinstance(delete_error, (BlobInProgressForkError, SessionOperationConflictError))
            assert len(staged.blob_plan) == len(copied) == 1
            rewritten = _rewrite_fork_state_blob_custody(
                staged.state,
                copied,
                {source_blob.storage_path: copied[source_blob.id]},
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
            _release_fork_authority(race_service, staged.authority)
            await _delete_test_blob(race_service, blob_service, parent.id, source_blob.id)
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
