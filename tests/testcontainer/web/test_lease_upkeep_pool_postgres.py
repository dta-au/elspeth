"""PostgreSQL: lease renewal and the membership heartbeat advance with the main session pool exhausted.

The app is built by ``create_app`` with the production pool settings, so the
production wiring decides which pool upkeep checks out from. Every main-pool
connection is held (no transaction, so no database lock is involved) and the
shared worker pool is full; both database deadlines must still move.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from psycopg import sql
from sqlalchemy import Connection, Engine, create_engine, select
from tests.fixtures.identities import ensure_test_identity
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.composer_pg_http import pg_http_settings
from tests.testcontainer.web.test_external_deployment_postgres import _DatabasePair, _identifier

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.app import create_app
from elspeth.web.coordination import membership_lifecycle
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.schema_probe import init_landscape_schema, init_session_schema, postgres_engine_kwargs
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, web_instances_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def databases(external_deployment_postgres_url: str) -> Iterator[_DatabasePair]:
    pair = _DatabasePair(
        postgres_url=external_deployment_postgres_url,
        session_database=_identifier("upkeep_pool_session"),
        landscape_database=_identifier("upkeep_pool_landscape"),
        runtime_role=_identifier("upkeep_pool_runtime"),
        runtime_password=uuid.uuid4().hex,
    )
    admin = create_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            for name in (pair.session_database, pair.landscape_database):
                connection.exec_driver_sql(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)).as_string())
        yield pair
    finally:
        with admin.connect() as connection:
            for name in (pair.session_database, pair.landscape_database):
                connection.exec_driver_sql(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)).as_string())
            if pair.role_created:
                connection.exec_driver_sql(sql.SQL("DROP ROLE {}").format(sql.Identifier(pair.runtime_role)).as_string())
        admin.dispose()


def _bootstrap(pair: _DatabasePair) -> None:
    session_owner = create_session_engine(pair.session_owner_url)
    landscape_owner = create_engine(pair.landscape_owner_url)
    try:
        init_session_schema(session_owner)
        init_landscape_schema(landscape_owner)
        pair.provision_runtime_role()
        for owner, permissions in (
            (session_owner, "SELECT, INSERT, UPDATE, DELETE"),
            (landscape_owner, "SELECT, INSERT, UPDATE"),
        ):
            with owner.begin() as connection:
                connection.exec_driver_sql(
                    sql.SQL("GRANT " + permissions + " ON ALL TABLES IN SCHEMA public TO {}")
                    .format(sql.Identifier(pair.runtime_role))
                    .as_string()
                )
                connection.exec_driver_sql(
                    sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}")
                    .format(sql.Identifier(pair.runtime_role))
                    .as_string()
                )
    finally:
        session_owner.dispose()
        landscape_owner.dispose()


def _fence_expiry(engine: Engine, session_id: UUID) -> datetime:
    with engine.connect() as conn:
        expiry: datetime = conn.execute(
            select(session_operation_fences_table.c.lease_expires_at).where(session_operation_fences_table.c.session_id == str(session_id))
        ).scalar_one()
        return expiry


def _member_expiry(engine: Engine, instance_id: str) -> datetime:
    with engine.connect() as conn:
        expiry: datetime = conn.execute(
            select(web_instances_table.c.lease_expires_at).where(web_instances_table.c.instance_id == instance_id)
        ).scalar_one()
        return expiry


async def _wait_until(predicate: Callable[[], bool], *, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


async def _poll_until(predicate: Callable[[], bool], *, seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@asynccontextmanager
async def _occupied_shared_pool() -> AsyncIterator[None]:
    release = threading.Event()

    def held_work() -> None:
        assert release.wait(60)

    tasks = [asyncio.create_task(async_workers.run_sync_in_worker(held_work)) for _ in range(async_workers.ADMISSION_CAPACITY)]
    try:
        await _wait_until(lambda: async_workers.outstanding_admissions() == async_workers.ADMISSION_CAPACITY)
        yield
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=60)


@pytest.mark.timeout(180, method="thread")
def test_renewal_and_heartbeat_advance_with_main_session_pool_exhausted(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(databases)
    for directory in (tmp_path, tmp_path / "blobs", tmp_path / "payloads"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    # A one-second heartbeat keeps the polling window short; the pool settings
    # stay the production ones.
    monkeypatch.setattr(membership_lifecycle, "heartbeat_interval_seconds", lambda _lease_seconds: 1)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-pg-upkeep-test-only-not-a-provider-credential")
    pool_kwargs = postgres_engine_kwargs(databases.session_runtime_url)
    main_capacity = pool_kwargs["pool_size"] + pool_kwargs["max_overflow"]
    observer = create_session_engine(databases.session_owner_url)

    async def scenario() -> None:
        app = create_app(
            settings=pg_http_settings(databases.session_runtime_url, databases.landscape_runtime_url, tmp_path),
            process_watchdog_factory=OwnedTestProcessWatchdog,
        )
        async with app.router.lifespan_context(app):
            session_service = app.state.session_service
            authority = session_service.session_operation_authority
            instance_id = session_service.session_operation_owner_instance_id
            main_engine = app.state.session_engine
            with main_engine.begin() as conn:
                ensure_test_identity(conn, identity_id="alice")
            session = authority.create_session_with_initial_fence(
                user_id="alice", title="Pool saturation", auth_provider_type="local", owner_instance_id=instance_id, lease_seconds=3
            )
            session_id = UUID(str(session.id))
            lease = await SessionOperationLease.acquire(
                authority,
                session_id=session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=instance_id,
                lease_seconds=3,
                renew_interval_seconds=0.2,
            )
            holders: list[Connection] = []
            try:
                fence_before = _fence_expiry(observer, session_id)
                member_before = _member_expiry(observer, instance_id)
                try:
                    holders.extend(main_engine.connect() for _ in range(main_capacity))
                    async with _occupied_shared_pool():
                        await _poll_until(
                            lambda: (
                                (
                                    _fence_expiry(observer, session_id) > fence_before
                                    and _member_expiry(observer, instance_id) > member_before
                                )
                                or lease.renewal_error is not None
                            ),
                            seconds=4,
                        )
                        advanced = {
                            "renewal_error": lease.renewal_error,
                            "fence_advanced": _fence_expiry(observer, session_id) > fence_before,
                            "heartbeat_advanced": _member_expiry(observer, instance_id) > member_before,
                        }
                        assert advanced == {"renewal_error": None, "fence_advanced": True, "heartbeat_advanced": True}
                finally:
                    for holder in holders:
                        holder.close()
            finally:
                with suppress(async_workers.AsyncWorkerAdmissionTimeoutError):
                    await asyncio.wait_for(lease.close(), timeout=60)

    try:
        asyncio.run(scenario())
    finally:
        observer.dispose()
