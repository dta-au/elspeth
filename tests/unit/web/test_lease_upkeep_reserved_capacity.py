"""Lease upkeep makes progress while ordinary worker capacity is occupied."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerOwner
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.membership_authority import (
    RepositoryWebInstanceMembershipAuthority,
    WebInstanceIdentity,
    current_compatibility_key,
)
from elspeth.web.coordination.membership_lifecycle import RegisteredWebInstanceMembership
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, web_instances_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    session_engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(session_engine)
    try:
        yield session_engine
    finally:
        session_engine.dispose()


async def _wait_until(predicate: Callable[[], bool], *, seconds: float = 3) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def _occupied_shared_pool() -> AsyncIterator[None]:
    release = threading.Event()

    def held_work() -> None:
        assert release.wait(10)

    tasks = [asyncio.create_task(async_workers.run_sync_in_worker(held_work)) for _ in range(async_workers.ADMISSION_CAPACITY)]
    try:
        await _wait_until(lambda: async_workers.outstanding_admissions() == async_workers.ADMISSION_CAPACITY)
        yield
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)


def _fence_expiry(engine: Engine, session_id: UUID):
    with engine.connect() as conn:
        return conn.execute(
            select(session_operation_fences_table.c.lease_expires_at).where(session_operation_fences_table.c.session_id == str(session_id))
        ).scalar_one()


def _member_expiry(engine: Engine, instance_id: str):
    with engine.connect() as conn:
        return conn.execute(
            select(web_instances_table.c.lease_expires_at).where(web_instances_table.c.instance_id == instance_id)
        ).scalar_one()


async def _poll_until(predicate: Callable[[], bool], *, seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
@pytest.mark.timeout(30, method="thread")
async def test_session_renewal_advances_database_deadline_with_ordinary_pool_occupied(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Lease upkeep", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=3
    )
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=UUID(str(session.id)),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="test-owner",
        lease_seconds=3,
        renew_interval_seconds=0.1,
    )
    before = _fence_expiry(engine, UUID(str(session.id)))
    try:
        async with _occupied_shared_pool():
            await _poll_until(lambda: _fence_expiry(engine, UUID(str(session.id))) > before or lease.renewal_error is not None, seconds=2)
            assert _fence_expiry(engine, UUID(str(session.id))) > before
            assert lease.renewal_error is None
    finally:
        try:
            with suppress(async_workers.AsyncWorkerAdmissionTimeoutError):
                await asyncio.wait_for(lease.close(), timeout=5)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
@pytest.mark.timeout(30, method="thread")
async def test_membership_heartbeat_advances_database_deadline_with_ordinary_pool_occupied(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = RepositoryWebInstanceMembershipAuthority(engine)
    identity = WebInstanceIdentity(
        instance_id=f"sqlite-{uuid4()}",
        deployment_target="kubernetes",
        deployment_generation="generation-1",
        compatibility_key=current_compatibility_key(),
        image_digest="sha256:membership-test",
        revision_label="revision-1",
    )
    draining = threading.Event()
    recovery = ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining)
    owner = ApplicationFinalizerOwner()
    membership = RegisteredWebInstanceMembership(
        authority,
        identity,
        lease_seconds=3,
        interval_seconds=1,
        process_recovery=recovery,
        instance_draining=draining,
        finalizer_owner=owner,
    )
    owner.seal()
    monkeypatch.setattr(async_workers, "_APPLICATION_FINALIZER_OWNER", owner)
    monkeypatch.setattr(async_workers, "_INSTANCE_DRAINING", draining)
    await membership.start()
    before = _member_expiry(engine, identity.instance_id)
    try:
        async with _occupied_shared_pool():
            await _poll_until(lambda: _member_expiry(engine, identity.instance_id) > before, seconds=2.5)
            assert _member_expiry(engine, identity.instance_id) > before
    finally:
        try:
            await asyncio.wait_for(membership.begin_drain(), timeout=5)
        finally:
            try:
                with suppress(async_workers.AsyncWorkerAdmissionTimeoutError):
                    await asyncio.wait_for(membership.stop(), timeout=5)
            finally:
                await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.timeout(20, method="thread")
async def test_session_release_completes_with_ordinary_pool_occupied(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Lease release", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=UUID(str(session.id)),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="test-owner",
        lease_seconds=30,
    )
    try:
        async with _occupied_shared_pool():
            await asyncio.wait_for(lease.close(), timeout=2)
            with engine.connect() as conn:
                released_at = conn.execute(
                    select(session_operation_fences_table.c.released_at).where(
                        session_operation_fences_table.c.session_id == str(session.id)
                    )
                ).scalar_one()
            assert released_at is not None
    finally:
        try:
            if not lease.closed:
                await asyncio.wait_for(lease.close(), timeout=5)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.timeout(15, method="thread")
async def test_shutdown_joins_lease_upkeep_worker() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def held_upkeep() -> str:
        started.set()
        assert release.wait(5)
        finished.set()
        return "completed"

    work = asyncio.create_task(async_workers.run_lease_upkeep_in_worker(held_upkeep))
    try:
        await _wait_until(started.is_set)
        shutdown = asyncio.create_task(async_workers.shutdown_async_workers())
        await _wait_until(lambda: async_workers._SHARED_SHUTDOWN_STARTED)
        assert not shutdown.done()
        with pytest.raises(async_workers.RequiredGenerationUnavailable, match="Shared executor shutdown has started"):
            await async_workers.run_lease_upkeep_in_worker(lambda: "late")
        assert async_workers._LEASE_UPKEEP_WORKERS.executor is None
        assert async_workers._LEASE_UPKEEP_WORKERS.outstanding == 1
        release.set()
        await asyncio.wait_for(shutdown, timeout=2)
        assert await asyncio.wait_for(work, timeout=2) == "completed"
        assert finished.is_set()
        assert async_workers._LEASE_UPKEEP_WORKERS.executor is None
    finally:
        release.set()
        try:
            await asyncio.wait_for(work, timeout=5)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_running_upkeep_retains_admission_until_completion_after_repeated_cancellation(raises: bool) -> None:
    entered = threading.Event()
    release = threading.Event()
    baseline = async_workers._LEASE_UPKEEP_WORKERS.outstanding

    def work() -> str:
        entered.set()
        assert release.wait(5)
        if raises:
            raise ValueError("upkeep failed")
        return "completed"

    task = asyncio.create_task(async_workers.run_lease_upkeep_in_worker(work))
    try:
        await _wait_until(entered.is_set)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline + 1
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, timeout=5)
        if raises:
            assert isinstance(caught.value.__cause__, ValueError)
        await _wait_until(lambda: async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline)
    finally:
        release.set()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)


@pytest.mark.asyncio
async def test_queued_upkeep_cancellation_prevents_invocation() -> None:
    entered = threading.Event()
    release = threading.Event()
    invoked = threading.Event()
    baseline = async_workers._LEASE_UPKEEP_WORKERS.outstanding
    entered_count = 0
    entered_lock = threading.Lock()

    def held() -> None:
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count == async_workers.LEASE_UPKEEP_MAX_WORKERS:
                entered.set()
        assert release.wait(5)

    def queued() -> None:
        invoked.set()

    tasks = [asyncio.create_task(async_workers.run_lease_upkeep_in_worker(held)) for _ in range(2)]
    queued_task: asyncio.Task[None] | None = None
    try:
        await _wait_until(entered.is_set)
        queued_task = asyncio.create_task(async_workers.run_lease_upkeep_in_worker(queued))
        await _wait_until(lambda: async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline + 3)
        queued_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(queued_task, timeout=5)
        assert not invoked.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        await _wait_until(lambda: async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline)
        assert not invoked.is_set()
    finally:
        release.set()
        retained = [*tasks, *([queued_task] if queued_task is not None else [])]
        try:
            await asyncio.wait_for(asyncio.gather(*retained, return_exceptions=True), timeout=5)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)


@pytest.mark.asyncio
async def test_upkeep_admission_wait_can_be_cancelled() -> None:
    release = threading.Event()
    invoked = threading.Event()
    baseline = async_workers._LEASE_UPKEEP_WORKERS.outstanding

    def held() -> None:
        assert release.wait(5)

    tasks = [
        asyncio.create_task(async_workers.run_lease_upkeep_in_worker(held)) for _ in range(async_workers.LEASE_UPKEEP_ADMISSION_CAPACITY)
    ]
    waiting: asyncio.Task[None] | None = None
    try:
        await _wait_until(
            lambda: async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline + async_workers.LEASE_UPKEEP_ADMISSION_CAPACITY
        )
        waiting = asyncio.create_task(async_workers.run_lease_upkeep_in_worker(invoked.set))
        await asyncio.sleep(0.02)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=5)
        assert not invoked.is_set()
        assert async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline + async_workers.LEASE_UPKEEP_ADMISSION_CAPACITY
    finally:
        release.set()
        retained = [*tasks, *([waiting] if waiting is not None else [])]
        try:
            await asyncio.wait_for(asyncio.gather(*retained, return_exceptions=True), timeout=5)
            await _wait_until(lambda: async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline)
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)


@pytest.mark.asyncio
async def test_upkeep_submit_refusal_releases_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = async_workers._LEASE_UPKEEP_WORKERS.outstanding
    executor = ThreadPoolExecutor(max_workers=1)
    executor.shutdown(wait=True)
    monkeypatch.setattr(async_workers._LEASE_UPKEEP_WORKERS, "get_executor", lambda: executor)
    with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
        await async_workers.run_lease_upkeep_in_worker(lambda: None)
    await _wait_until(lambda: async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline)


@pytest.mark.asyncio
async def test_upkeep_callable_failure_propagates() -> None:
    baseline = async_workers._LEASE_UPKEEP_WORKERS.outstanding

    def fail() -> None:
        raise ValueError("upkeep failed")

    try:
        with pytest.raises(ValueError, match="upkeep failed"):
            await asyncio.wait_for(async_workers.run_lease_upkeep_in_worker(fail), timeout=5)
        await _wait_until(lambda: async_workers._LEASE_UPKEEP_WORKERS.outstanding == baseline)
    finally:
        await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)
