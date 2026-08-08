"""Run admission must share the same-session custody lock domain on PostgreSQL.

``_execute_update_blob`` / ``_execute_delete_blob`` evaluate their active-run
guards inside ``locked_session_transaction`` (transaction-scoped
``pg_advisory_xact_lock`` on PostgreSQL). That guard is only sound if run
admission is mutually exclusive with the same lock: a ``create_run`` that
commits through a bare ``engine.begin()`` never touches the advisory lock, so
the blob mutation and the run INSERT can each pass their guards concurrently
and a run can capture a blob mid-mutation (elspeth-3d1d1fcb6c).

SQLite masks this hole because its ``engine.begin()`` issues ``BEGIN
IMMEDIATE`` (whole-database writer exclusion); PostgreSQL is the dialect where
the missing session lock is observable, hence the testcontainer proof.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy import Engine
from testcontainers.postgres import PostgresContainer

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        engine = create_session_engine(postgres.get_connection_url())
        initialize_session_schema(engine)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture
def postgres_service(postgres_engine: Engine, tmp_path: Path) -> SessionServiceImpl:
    return SessionServiceImpl(
        postgres_engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run.admission.custody.lock"),
    )


def test_create_run_waits_for_session_custody_lock_postgres(
    postgres_engine: Engine,
    postgres_service: SessionServiceImpl,
) -> None:
    service = postgres_service
    session = asyncio.run(service.create_session(f"alice-{uuid.uuid4().hex[:8]}", "Lock domain", "local"))
    state = asyncio.run(
        service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
    )

    lock_held = threading.Event()
    release = threading.Event()
    run_created = threading.Event()
    failures: list[BaseException] = []

    def hold_custody_lock() -> None:
        try:
            with locked_session_transaction(postgres_engine, str(session.id)):
                lock_held.set()
                if not release.wait(timeout=15):
                    raise TimeoutError("custody lock holder was never released")
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            failures.append(exc)
            lock_held.set()

    def admit_run() -> None:
        try:
            asyncio.run(service.create_run(session.id, state.id))
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            failures.append(exc)
        finally:
            run_created.set()

    holder = threading.Thread(target=hold_custody_lock, name="custody-lock-holder")
    holder.start()
    assert lock_held.wait(timeout=10)
    runner = threading.Thread(target=admit_run, name="run-admitter")
    runner.start()
    try:
        assert not run_created.wait(timeout=1.5), (
            "create_run committed while the session advisory lock was held; run admission escaped the blob/run lock domain"
        )
    finally:
        release.set()
        holder.join(timeout=15)
        runner.join(timeout=15)

    assert run_created.wait(timeout=15)
    assert failures == []
    active = asyncio.run(service.get_active_run(session.id))
    assert active is not None
    assert active.status == "pending"
