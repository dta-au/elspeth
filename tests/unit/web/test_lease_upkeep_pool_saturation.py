"""Lease renewal advances its database deadline while the main session pool is exhausted.

Driven through ``create_app`` so the production wiring decides which pool
renewal checks out from. Only names that also exist before the dedicated
upkeep pool are imported, so this file measures red on the earlier tree.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretBytes
from sqlalchemy import Engine, select

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import app as app_module
from elspeth.web import async_workers
from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from tests.fixtures.identities import ensure_test_identity


def _settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        operator_metrics_bearer_token="operator-metrics-token-for-tests-0001",
    )


@pytest.fixture()
def observer(tmp_path: Path) -> Iterator[Engine]:
    """A separate engine for reading the deadline, outside both pools under test."""
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    try:
        yield engine
    finally:
        engine.dispose()


def _fence_expiry(engine: Engine, session_id: UUID) -> datetime:
    with engine.connect() as conn:
        expiry: datetime = conn.execute(
            select(session_operation_fences_table.c.lease_expires_at).where(session_operation_fences_table.c.session_id == str(session_id))
        ).scalar_one()
        return expiry


async def _wait_until(predicate: Callable[[], bool], *, seconds: float = 3) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


async def _poll_until(predicate: Callable[[], bool], *, seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@asynccontextmanager
async def _occupied_shared_pool() -> AsyncIterator[None]:
    release = threading.Event()

    def held_work() -> None:
        assert release.wait(30)

    tasks = [asyncio.create_task(async_workers.run_sync_in_worker(held_work)) for _ in range(async_workers.ADMISSION_CAPACITY)]
    try:
        await _wait_until(lambda: async_workers.outstanding_admissions() == async_workers.ADMISSION_CAPACITY)
        yield
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
@pytest.mark.timeout(90, method="thread")
async def test_renewal_advances_deadline_with_main_session_pool_exhausted(
    tmp_path: Path, observer: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One main-pool connection, no overflow, and a checkout wait far longer
    # than the polling window: a renewal on the main pool cannot finish in time.
    monkeypatch.setattr(app_module, "postgres_engine_kwargs", lambda _url: {"pool_size": 1, "max_overflow": 0, "pool_timeout": 20})
    app = create_app(_settings(tmp_path))
    try:
        main_engine = app.state.session_engine
        authority = app.state.session_service.session_operation_authority
        with main_engine.begin() as conn:
            ensure_test_identity(conn, identity_id="alice")
        session = authority.create_session_with_initial_fence(
            user_id="alice", title="Pool saturation", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=3
        )
        session_id = UUID(str(session.id))
        lease = await SessionOperationLease.acquire(
            authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="test-owner",
            lease_seconds=3,
            renew_interval_seconds=0.1,
        )
        try:
            before = _fence_expiry(observer, session_id)
            # Holds the only main connection without a transaction, so no
            # database lock is involved: only the pool checkout can block.
            holder = main_engine.connect()
            try:
                async with _occupied_shared_pool():
                    await _poll_until(lambda: _fence_expiry(observer, session_id) > before or lease.renewal_error is not None, seconds=2)
                    assert lease.renewal_error is None
                    assert _fence_expiry(observer, session_id) > before
            finally:
                holder.close()
        finally:
            with suppress(async_workers.AsyncWorkerAdmissionTimeoutError):
                await asyncio.wait_for(lease.close(), timeout=30)
    finally:
        try:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=30)
        finally:
            app.state._session_engine_finalizer()
