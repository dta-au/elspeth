"""Run admission must share the same-session custody lock domain on PostgreSQL.

``_execute_update_blob`` / ``BlobServiceImpl.delete_blob`` evaluate their
active-run guards inside ``locked_session_transaction`` (transaction-scoped
``pg_advisory_xact_lock`` on PostgreSQL). Those guards are only sound if run
admission is mutually exclusive with the same lock: a ``create_run`` that
commits outside it never touches the advisory lock, so the blob mutation and
the run INSERT can each pass their guards concurrently and a run can capture
a blob mid-mutation (elspeth-3d1d1fcb6c).

Since the multi-replica recovery, ``create_run`` routes through the
session-operation authority (``transaction.runs.create_pending_run``), whose
``mutate`` takes ``transaction_session_lock`` — the SAME advisory key. That
equivalence is asserted here, not assumed (ruling 5, elspeth-4d6c0dd0f5):
each proof parks one side after its guard has already evaluated, proves the
other side is a genuine PostgreSQL lock waiter, and then proves the committed
state the late side observes.

SQLite masks all of this because its ``engine.begin()`` issues ``BEGIN
IMMEDIATE`` (whole-database writer exclusion); PostgreSQL is the dialect
where a missing session lock is observable, hence the testcontainer proof.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest
import structlog
from sqlalchemy import Engine, event
from testcontainers.postgres import PostgresContainer
from tests.unit.web.composer.test_tools import _empty_state, _insert_user_message, _trained_tool_context

from elspeth.contracts.blobs import BlobActiveRunError, BlobNotFoundError, BlobRecord
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.tools.blobs import _execute_update_blob
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer

_BLOB_CONTENT = b"id,value\n1,alpha\n"


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url()
        engine = create_session_engine(url)
        try:
            initialize_session_schema(engine)
        finally:
            engine.dispose()
        yield url


@pytest.fixture
def postgres_engine(postgres_url: str) -> Iterator[Engine]:
    engine = create_session_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def second_engine(postgres_url: str) -> Iterator[Engine]:
    """A second connection pool — a second web replica's view of the database."""
    engine = create_session_engine(postgres_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _service(engine: Engine, tmp_path: Path, name: str) -> SessionServiceImpl:
    return SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger(f"test.run.admission.custody.lock.{name}"),
    )


@pytest.fixture
def postgres_service(postgres_engine: Engine, tmp_path: Path) -> SessionServiceImpl:
    return _service(postgres_engine, tmp_path, "first")


@pytest.fixture
def second_service(second_engine: Engine, tmp_path: Path) -> SessionServiceImpl:
    return _service(second_engine, tmp_path, "second")


@pytest.fixture
def blob_service(postgres_engine: Engine, tmp_path: Path) -> BlobServiceImpl:
    return BlobServiceImpl(postgres_engine, tmp_path)


@asynccontextmanager
async def _operation(service: SessionServiceImpl, session_id: uuid.UUID, kind: SessionOperationKind):
    """Mint one real session-operation context through the production authority."""
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=kind,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        yield context
    finally:
        await service._run_sync(service.session_operation_authority.release, context)


async def _seed_session_with_blob(
    service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
    *,
    reference_blob: bool,
) -> tuple[Any, Any, BlobRecord]:
    """One session, one blob, and one saved state that does or does not reference it."""
    session = await service.create_session(f"alice-{uuid.uuid4().hex[:8]}", "Lock domain", "local")
    async with _operation(service, session.id, SessionOperationKind.CREATE) as create:
        blob = await blob_service.create_blob(session.id, "tickets.csv", _BLOB_CONTENT, "text/csv", session_operation_context=create)
    sources = {
        "tickets": {
            "plugin": "csv",
            "on_success": "output",
            "on_validation_failure": "quarantine",
            "options": {"path": blob.storage_path if reference_blob else "elsewhere.csv"},
        }
    }
    async with _operation(service, session.id, SessionOperationKind.COMPOSE) as compose:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(sources=sources, metadata_={"name": "lock-domain", "description": ""}, is_valid=True),
            provenance="session_seed",
            session_operation_context=compose,
        )
    return session, state, blob


def _admit_run(service: SessionServiceImpl, session_id: uuid.UUID, state_id: uuid.UUID) -> Any:
    async def _go() -> Any:
        async with _operation(service, session_id, SessionOperationKind.EXECUTE) as execute:
            return await service.create_run(session_id, state_id, session_operation_context=execute)

    return asyncio.run(_go())


async def _delete_blob(service: SessionServiceImpl, blob_service: BlobServiceImpl, session_id: uuid.UUID, blob_id: uuid.UUID) -> None:
    """Delete one blob the way the route does: under a real COMPOSE context."""
    async with _operation(service, session_id, SessionOperationKind.COMPOSE) as compose:
        await blob_service.delete_blob(blob_id, session_operation_context=compose)


async def _get_blob(service: SessionServiceImpl, blob_service: BlobServiceImpl, session_id: uuid.UUID, blob_id: uuid.UUID) -> BlobRecord:
    """Read one blob record under a real BLOB_READ context."""
    async with _operation(service, session_id, SessionOperationKind.BLOB_READ) as read:
        return await blob_service.get_blob(blob_id, session_operation_context=read)


def _install_advisory_lock_probe(engine: Engine) -> tuple[threading.Event, dict[str, int], Callable[..., None]]:
    """Capture the backend pid of the connection that asks for the session advisory lock."""
    attempted = threading.Event()
    backend: dict[str, int] = {}

    def before_cursor_execute(conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if "pg_advisory_xact_lock" not in statement:
            return
        backend["pid"] = conn.connection.driver_connection.info.backend_pid
        attempted.set()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    return attempted, backend, before_cursor_execute


def _assert_backend_waits_on_lock(engine: Engine, backend_pid: int) -> None:
    deadline = monotonic() + 5
    activity = None
    with engine.connect() as observer:
        while monotonic() < deadline:
            activity = observer.exec_driver_sql(
                "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                (backend_pid,),
            ).one()
            if activity.wait_event_type == "Lock":
                return
            sleep(0.01)
    pytest.fail(f"PostgreSQL backend {backend_pid} never entered a lock wait; last activity={activity!r}")


def _park_after_statement(engine: Engine, marker: str) -> tuple[threading.Event, threading.Event, Callable[..., None]]:
    """Park the executing thread right after the first statement containing ``marker``.

    The statement has already executed inside the caller's open transaction,
    so the caller is provably past its guard and still holding every lock the
    transaction took.
    """
    parked = threading.Event()
    release = threading.Event()

    def after_cursor_execute(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if parked.is_set() or marker not in statement:
            return
        parked.set()
        assert release.wait(timeout=15), "parked blob mutation was never released"

    event.listen(engine, "after_cursor_execute", after_cursor_execute)
    return parked, release, after_cursor_execute


def test_create_run_waits_for_session_custody_lock_postgres(
    postgres_engine: Engine,
    postgres_service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
) -> None:
    """The bare custody lock excludes run admission (the original 3d1d1fcb6c proof)."""
    service = postgres_service
    session, state, _blob = asyncio.run(_seed_session_with_blob(service, blob_service, reference_blob=False))

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
            _admit_run(service, session.id, state.id)
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


def test_delete_blob_past_its_guard_excludes_run_admission_on_another_replica(
    postgres_engine: Engine,
    postgres_service: SessionServiceImpl,
    second_engine: Engine,
    second_service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
) -> None:
    """delete-vs-run: the delete has passed its active-run guard; the run must wait for its commit.

    Parked after the deletion-ledger INSERT (the first write after both
    active-run guards evaluated "no active run"), the delete still holds the
    advisory lock. A second replica admitting a run against a state that
    references the blob must enter a real PostgreSQL lock wait, and may only
    commit after the delete commits — never inside the guard window.
    """
    service = postgres_service
    session, state, blob = asyncio.run(_seed_session_with_blob(service, blob_service, reference_blob=True))

    parked, release, park_listener = _park_after_statement(postgres_engine, "INSERT INTO blob_deletion_cleanups")
    attempted, backend, probe_listener = _install_advisory_lock_probe(second_engine)
    run_created = threading.Event()
    failures: list[BaseException] = []
    delete_error: list[BaseException] = []

    def delete_blob() -> None:
        try:
            asyncio.run(_delete_blob(service, blob_service, session.id, blob.id))
        except BaseException as exc:
            delete_error.append(exc)
            parked.set()

    def admit_run() -> None:
        try:
            _admit_run(second_service, session.id, state.id)
        except BaseException as exc:
            failures.append(exc)
        finally:
            run_created.set()

    deleter = threading.Thread(target=delete_blob, name="blob-deleter")
    deleter.start()
    try:
        assert parked.wait(timeout=10), "delete never reached its post-guard write"
        assert delete_error == []
        runner = threading.Thread(target=admit_run, name="run-admitter")
        runner.start()
        assert attempted.wait(timeout=10), "run admission never asked for the session advisory lock"
        _assert_backend_waits_on_lock(postgres_engine, backend["pid"])
        assert not run_created.wait(timeout=1.5), "create_run committed inside the delete's guard window"
    finally:
        release.set()
        deleter.join(timeout=15)
        event.remove(postgres_engine, "after_cursor_execute", park_listener)
        event.remove(second_engine, "before_cursor_execute", probe_listener)

    assert run_created.wait(timeout=15)
    runner.join(timeout=15)
    assert delete_error == []
    # The run's acquire queued on the advisory lock behind the delete's
    # transaction and, once through, met the delete's still-live COMPOSE
    # fence: the session-operation authority refuses a second operation on
    # the session. Admission succeeds only after the delete's operation has
    # ended, and it then observes a world in which the blob is already gone,
    # never one mid-mutation.
    assert [type(exc) for exc in failures] == [SessionOperationConflictError], failures
    run = _admit_run(second_service, session.id, state.id)
    assert run.status == "pending"
    with pytest.raises(BlobNotFoundError):
        asyncio.run(_get_blob(second_service, blob_service, session.id, blob.id))
    active = asyncio.run(second_service.get_active_run(session.id))
    assert active is not None
    assert active.status == "pending"


def test_update_blob_past_its_guard_excludes_run_admission_on_another_replica(
    postgres_engine: Engine,
    postgres_service: SessionServiceImpl,
    second_engine: Engine,
    second_service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
) -> None:
    """update-vs-run: same proof through the composer's update_blob custody transaction."""
    service = postgres_service
    session, state, blob = asyncio.run(_seed_session_with_blob(service, blob_service, reference_blob=True))
    new_content = "id,value\n1,beta\n"
    message_content = f"Use this exact content:\n{new_content}"
    user_message_id = _insert_user_message(postgres_engine, str(session.id), message_content)

    parked, release, park_listener = _park_after_statement(postgres_engine, "UPDATE blobs SET")
    attempted, backend, probe_listener = _install_advisory_lock_probe(second_engine)
    run_created = threading.Event()
    failures: list[BaseException] = []
    update_outcome: list[Any] = []

    def update_blob() -> None:
        try:
            update_outcome.append(
                _execute_update_blob(
                    {"blob_id": str(blob.id), "content": new_content},
                    _empty_state(),
                    _trained_tool_context(
                        session_engine=postgres_engine,
                        session_id=str(session.id),
                        user_message_id=user_message_id,
                        user_message_content=message_content,
                    ),
                )
            )
        except BaseException as exc:
            update_outcome.append(exc)
            parked.set()

    def admit_run() -> None:
        try:
            _admit_run(second_service, session.id, state.id)
        except BaseException as exc:
            failures.append(exc)
        finally:
            run_created.set()

    updater = threading.Thread(target=update_blob, name="blob-updater")
    updater.start()
    try:
        assert parked.wait(timeout=10), "update never reached its post-guard write"
        assert update_outcome == []
        runner = threading.Thread(target=admit_run, name="run-admitter")
        runner.start()
        assert attempted.wait(timeout=10), "run admission never asked for the session advisory lock"
        _assert_backend_waits_on_lock(postgres_engine, backend["pid"])
        assert not run_created.wait(timeout=1.5), "create_run committed inside the update's guard window"
    finally:
        release.set()
        updater.join(timeout=15)
        event.remove(postgres_engine, "after_cursor_execute", park_listener)
        event.remove(second_engine, "before_cursor_execute", probe_listener)

    assert run_created.wait(timeout=15)
    runner.join(timeout=15)
    assert failures == []
    assert len(update_outcome) == 1
    result = update_outcome[0]
    assert not isinstance(result, BaseException), result
    assert result.success is True, result.to_dict()
    updated = asyncio.run(_get_blob(second_service, blob_service, session.id, blob.id))
    assert updated is not None
    assert Path(updated.storage_path).read_bytes() == new_content.encode()
    active = asyncio.run(second_service.get_active_run(session.id))
    assert active is not None
    assert active.status == "pending"


def test_run_admitted_first_is_observed_by_blob_delete_and_update(
    postgres_engine: Engine,
    postgres_service: SessionServiceImpl,
    second_service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
) -> None:
    """The mirror order: a committed pending run is visible to both guards on the other replica."""
    service = postgres_service
    session, state, blob = asyncio.run(_seed_session_with_blob(service, blob_service, reference_blob=True))
    run = _admit_run(second_service, session.id, state.id)
    assert run.status == "pending"

    with pytest.raises(BlobActiveRunError):
        asyncio.run(_delete_blob(service, blob_service, session.id, blob.id))

    new_content = "id,value\n1,gamma\n"
    message_content = f"Use this exact content:\n{new_content}"
    user_message_id = _insert_user_message(postgres_engine, str(session.id), message_content)
    result = _execute_update_blob(
        {"blob_id": str(blob.id), "content": new_content},
        _empty_state(),
        _trained_tool_context(
            session_engine=postgres_engine,
            session_id=str(session.id),
            user_message_id=user_message_id,
            user_message_content=message_content,
        ),
    )
    assert result.success is False, result.to_dict()
    assert Path(blob.storage_path).read_bytes() == _BLOB_CONTENT
