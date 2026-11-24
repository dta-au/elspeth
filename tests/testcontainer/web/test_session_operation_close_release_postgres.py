"""PostgreSQL: a lease releases its fence at close even after a failed renewal, and its replica can start the next operation.

The app is built by ``create_app`` with production pool settings. Each case
runs a COMPOSE lease, lets its renewal fail (a renewal that reaches the
database after the deadline, or one non-fence database error), closes it, and
starts a new operation on the same session from the same live replica. Each
case has a control in which the renewal does not fail.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from psycopg import sql
from sqlalchemy import Connection, Engine, create_engine, func, select
from sqlalchemy.exc import OperationalError
from tests.fixtures.identities import ensure_test_identity
from tests.testcontainer.web.test_external_deployment_postgres import _DatabasePair, _identifier
from tests.testcontainer.web.test_lease_upkeep_pool_postgres import _build_app, _prepare

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator
from elspeth.web.schema_probe import postgres_engine_kwargs
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, web_instances_table

pytestmark = pytest.mark.testcontainer

_LEASE_SECONDS = 3
_HOLD_SECONDS = 6.0  # longer than the lease, shorter than the 30 s pool_timeout


@pytest.fixture
def databases(external_deployment_postgres_url: str) -> Iterator[_DatabasePair]:
    pair = _DatabasePair(
        postgres_url=external_deployment_postgres_url,
        session_database=_identifier("close_release_session"),
        landscape_database=_identifier("close_release_landscape"),
        runtime_role=_identifier("close_release_runtime"),
        runtime_password=uuid4().hex,
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


@pytest.fixture
def observer(databases: _DatabasePair) -> Iterator[Engine]:
    engine = create_session_engine(databases.session_owner_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _fence(engine: Engine, session_id: UUID) -> dict[str, Any]:
    with engine.connect() as conn:
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        row = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id))
        ).one()
        return {
            "operation_id": row.operation_id,
            "released": row.released_at is not None,
            "expired": row.lease_expires_at <= now,
            "lease_expires_at": row.lease_expires_at,
        }


def _member_live(engine: Engine, instance_id: str) -> bool:
    with engine.connect() as conn:
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        expiry: datetime = conn.execute(
            select(web_instances_table.c.lease_expires_at).where(web_instances_table.c.instance_id == instance_id)
        ).scalar_one()
        return bool(expiry > now)


class _FailingRenewal:
    """Lease authority stand-in: every renewal raises a non-fence database error."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def renew(self, context: SessionOperationContext, *, lease_seconds: int) -> SessionOperationContext:
        raise OperationalError("renew", {}, ConnectionError("database unavailable"))

    def release_at_close(self, context: SessionOperationContext) -> object:
        result: object = self._real.release_at_close(context)
        return result


async def _acquire_compose(app: FastAPI, session_id: UUID) -> SessionOperationLease:
    session_service = app.state.session_service
    return await SessionOperationLease.acquire(
        session_service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=session_service.session_operation_owner_instance_id,
        lease_seconds=_LEASE_SECONDS,
        renew_interval_seconds=0.2,
    )


def _run(
    databases: _DatabasePair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observer: Engine,
    *,
    hold_pool: bool,
    required: bool,
    fail_renewal: bool,
) -> dict[str, Any]:
    _prepare(databases, tmp_path, monkeypatch)
    pool_kwargs = postgres_engine_kwargs(databases.session_runtime_url)
    main_capacity = pool_kwargs["pool_size"] + pool_kwargs["max_overflow"]
    record: dict[str, Any] = {}

    async def scenario() -> None:
        app = _build_app(databases, tmp_path)
        async with app.router.lifespan_context(app):
            session_service = app.state.session_service
            authority = session_service.session_operation_authority
            instance_id = session_service.session_operation_owner_instance_id
            main_engine = app.state.session_engine
            with main_engine.begin() as conn:
                ensure_test_identity(conn, identity_id="alice")
            session = authority.create_session_with_initial_fence(
                user_id="alice",
                title="Close release",
                auth_provider_type="local",
                owner_instance_id=instance_id,
                lease_seconds=_LEASE_SECONDS,
            )
            session_id = UUID(str(session.id))
            lease = await _acquire_compose(app, session_id)
            if required:
                lease.bind_required_work(
                    RequiredWorkCoordinator(
                        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, lease.context, invocation_id=str(uuid4()))
                    )
                )
            if fail_renewal:
                monkeypatch.setattr(lease, "_upkeep_authority", _FailingRenewal(lease._upkeep_authority))

            holders: list[Connection] = []
            started = time.monotonic()
            try:
                if hold_pool:
                    holders.extend(main_engine.connect() for _ in range(main_capacity))
                await asyncio.sleep(_HOLD_SECONDS)
            finally:
                for holder in holders:
                    holder.close()
            record["held_seconds"] = time.monotonic() - started
            await asyncio.sleep(1.0)  # the in-flight renewal lands
            record["renewal_error"] = lease.renewal_error
            record["fence_before_close"] = _fence(observer, session_id)
            try:
                await asyncio.wait_for(lease.close(), timeout=30)
                record["close_error"] = None
            except Exception as close_error:
                record["close_error"] = close_error
            record["disposition"] = lease.disposition
            record["fence_after_close"] = _fence(observer, session_id)

            attempts: list[dict[str, Any]] = []
            for delay in (0.0, float(_LEASE_SECONDS) + 1.0):
                await asyncio.sleep(delay)
                entry: dict[str, Any] = {"member_live": _member_live(observer, instance_id)}
                try:
                    following = await _acquire_compose(app, session_id)
                except SessionOperationConflictError as conflict:
                    entry["acquire"] = conflict
                else:
                    entry["acquire"] = "ok"
                    await asyncio.wait_for(following.close(), timeout=30)
                attempts.append(entry)
            record["new_acquire"] = attempts

    asyncio.run(scenario())
    return record


def _assert_released_and_reacquired(record: dict[str, Any]) -> None:
    assert record["fence_after_close"]["released"] is True, record
    assert all(attempt["member_live"] for attempt in record["new_acquire"]), record
    assert [attempt["acquire"] for attempt in record["new_acquire"]] == ["ok", "ok"], record


@pytest.mark.timeout(240, method="thread")
def test_late_required_work_renewal_then_close_releases_and_the_live_replica_reacquires(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=True, required=True, fail_renewal=False)
    assert type(record["renewal_error"]) is SessionOperationFenceLost, record
    assert record["fence_before_close"]["expired"] is True, record
    assert str(record["disposition"]) == "lost", record
    assert type(record["close_error"]) is SessionOperationFenceLost, record
    # The deadline is never moved later by the close-time release.
    assert record["fence_after_close"]["lease_expires_at"] == record["fence_before_close"]["lease_expires_at"], record
    _assert_released_and_reacquired(record)


@pytest.mark.timeout(240, method="thread")
def test_required_work_renewal_with_the_pool_free_releases_normally(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=False, required=True, fail_renewal=False)
    assert record["renewal_error"] is None, record
    assert record["fence_before_close"]["expired"] is False, record
    assert (str(record["disposition"]), record["close_error"]) == ("released", None), record
    _assert_released_and_reacquired(record)


@pytest.mark.timeout(240, method="thread")
def test_one_renewal_error_then_close_releases_and_the_live_replica_reacquires(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=False, required=False, fail_renewal=True)
    assert type(record["renewal_error"]) is OperationalError, record
    assert record["fence_before_close"]["expired"] is True, record
    assert str(record["disposition"]) == "lost", record
    assert type(record["close_error"]) is OperationalError, record
    assert record["fence_after_close"]["lease_expires_at"] == record["fence_before_close"]["lease_expires_at"], record
    _assert_released_and_reacquired(record)


@pytest.mark.timeout(240, method="thread")
def test_upkeep_renewal_without_an_error_releases_normally(
    databases: _DatabasePair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observer: Engine
) -> None:
    record = _run(databases, tmp_path, monkeypatch, observer, hold_pool=False, required=False, fail_renewal=False)
    assert record["renewal_error"] is None, record
    assert record["fence_before_close"]["expired"] is False, record
    assert (str(record["disposition"]), record["close_error"]) == ("released", None), record
    _assert_released_and_reacquired(record)
