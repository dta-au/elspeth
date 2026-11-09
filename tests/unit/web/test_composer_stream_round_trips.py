"""Statements, clock samples and connections per delivered Composer stream frame.

Fix 3-T1 consolidated the reads behind each frame without changing what is
decided or when. These tests pin the per-frame cost the stream route pays on
the sessions database, counted with SQLAlchemy's ``before_cursor_execute``
event, so a regression that reintroduces a duplicate read turns red.

The negative control is this same file run against the route before fix
3-T1 (HEAD 24b9610f, PYTHONPATH into a worktree of it): there
``test_per_frame_statements_clocks_and_connections_on_sqlite`` fails with
(statements, clocks, connections, write locks) of pre-header (9, 3, 5, 1),
status (11, 4, 6, 1), progress (9, 3, 5, 1), heartbeat (9, 3, 5, 1) and
terminal (11, 4, 6, 1). The write lock was ``get_session_for_stream``'s
``engine.begin()``, which on SQLite is ``BEGIN IMMEDIATE`` on every send.

The progress registry is a stub that returns one snapshot: on SQLite the real
registry is process-local and issues no SQL, so the stub keeps the counts
honest while still driving the progress-frame branch and the authorization
side's progress re-check. The worker is never started, so nothing else uses
the engine while a frame is counted.
"""

from __future__ import annotations

import threading
import types
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy import Engine, event
from starlette.requests import Request

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.auth.models import UserIdentity
from elspeth.web.auth.session_token import SessionTokenIssuer
from elspeth.web.auth.sso import SsoAuthProvider
from elspeth.web.composer.progress import ComposerProgressSnapshot
from elspeth.web.composer_stream import BoundedComposerStreamResponse, ComposerStreamPermits
from elspeth.web.composer_stream_auth import ComposerStreamAuthServices
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.auth_session_authority import RepositoryAuthSessionAuthority
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.routes.composer import operations
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.unit.web.coordination.test_composer_operation_authority import admit

_PROVIDER: AuthProviderType = "vanguard"
_IDENTITY = "stream-reader"
_OWNER = "round-trip-owner"


@dataclass
class StatementLedger:
    """Every cursor execute on one engine, in order, from any thread."""

    statements: list[str] = field(default_factory=list)
    connections: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, statement: str) -> None:
        with self._lock:
            self.statements.append(statement)

    def connect(self) -> None:
        with self._lock:
            self.connections += 1

    def mark(self) -> tuple[int, int]:
        with self._lock:
            return len(self.statements), self.connections

    def since(self, mark: tuple[int, int]) -> FrameCost:
        with self._lock:
            window = self.statements[mark[0] :]
            taken = tuple(sql for sql in window if not _is_transaction_control(sql))
            return FrameCost(
                statements=len(taken),
                clocks=sum(1 for sql in taken if _is_clock_sample(sql)),
                connections=self.connections - mark[1],
                write_locks=sum(1 for sql in window if sql.strip().upper() == "BEGIN IMMEDIATE"),
                sql=taken,
            )


@dataclass(frozen=True)
class FrameCost:
    statements: int
    clocks: int
    connections: int
    write_locks: int
    sql: tuple[str, ...]

    def summary(self) -> tuple[int, int, int, int]:
        return self.statements, self.clocks, self.connections, self.write_locks


_TRANSACTION_CONTROL = frozenset({"BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"})


def _is_transaction_control(sql: str) -> bool:
    """The session engine emits SQLite's ``BEGIN`` itself; it is not a read.

    ``BEGIN IMMEDIATE`` (an ``engine.begin()`` block on SQLite) takes the
    database write lock, so it is counted separately as ``write_locks``.
    """
    return sql.strip().upper() in _TRANSACTION_CONTROL


def _is_clock_sample(sql: str) -> bool:
    upper = sql.upper()
    return "CLOCK_TIMESTAMP()" in upper or "CURRENT_TIMESTAMP" in upper


@contextmanager
def statement_ledger(engine: Engine) -> Iterator[StatementLedger]:
    ledger = StatementLedger()

    def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        ledger.record(statement)

    def engine_connect(_conn) -> None:
        ledger.connect()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine, "engine_connect", engine_connect)
    try:
        yield ledger
    finally:
        event.remove(engine, "engine_connect", engine_connect)
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


class _OneSnapshotProgressRegistry:
    """Stand-in for the process-local registry: answers without SQL."""

    def __init__(self) -> None:
        self.snapshot: ComposerProgressSnapshot | None = None

    async def get_for_operation(self, **_binding: object) -> ComposerProgressSnapshot | None:
        return self.snapshot


class _ShiftedLoop:
    """Expose ``time()`` shifted forward so the producer's heartbeat is due."""

    def __init__(self, loop, offset: list[float]) -> None:
        self._loop = loop
        self._offset = offset

    def time(self) -> float:
        return self._loop.time() + self._offset[0]


@dataclass
class StreamHarness:
    engine: Engine
    app: FastAPI
    token: str
    session_id: UUID
    operation_id: str
    running: ComposerOperationRunning
    service: SessionServiceImpl
    progress: _OneSnapshotProgressRegistry
    clock_offset: list[float]
    fence_id: str
    fence_epoch: int

    async def open(self) -> tuple[BoundedComposerStreamResponse, AsyncIterator[bytes]]:
        scope = {
            "type": "http",
            "app": self.app,
            "method": "GET",
            "path": f"/api/sessions/{self.session_id}/operations/{self.operation_id}/stream",
            "headers": [(b"authorization", f"Bearer {self.token}".encode())],
            "query_string": b"",
        }
        response = await operations.stream_operation(
            self.session_id, UUID(self.operation_id), Request(scope), UserIdentity(user_id=_IDENTITY, username=_IDENTITY)
        )
        return response, response._frames.__aiter__()

    def publish_progress(self) -> None:
        self.progress.snapshot = ComposerProgressSnapshot(
            session_id=str(self.session_id),
            request_id="correlation",
            operation_id=self.operation_id,
            session_operation_id=self.fence_id,
            session_operation_epoch=self.fence_epoch,
            request_token="progress-request-token",
            updated_at=datetime.now(UTC),
            phase="calling_model",
            headline="Working on the request",
        )

    async def settle_failed(self) -> None:
        failure = project_composer_operation_error(RuntimeError("settled by the round-trip harness"), request_id="correlation")
        await self.service.fail_composer_async_operation(self.running, failure=failure, authoritative_failure=True)


def build_stream_harness(engine: Engine, data_dir: Path, *, repository, monkeypatch: pytest.MonkeyPatch) -> StreamHarness:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id=_IDENTITY, provider=_PROVIDER)
        grant_test_pipeline_user(conn, identity_id=_IDENTITY)
    session = repository.create_session_with_initial_fence(
        user_id=_IDENTITY, title="Round trips", auth_provider_type=_PROVIDER, owner_instance_id=_OWNER, lease_seconds=60
    )
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id=_OWNER, claim_lease_seconds=60)
    record, _ = admit(authority, session.id, actor=_IDENTITY, provider=_PROVIDER)
    (claim,) = authority.claim_next(limit=1)
    context = repository.start_composer_async_operation(claim, owner_instance_id=_OWNER, lease_seconds=60, auth_provider_type=_PROVIDER)
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    started = authority.get(session_id=session.id, operation_id=record.operation_id)
    assert started is not None and started.status == "running"
    assert started.session_operation_id is not None and started.session_operation_epoch is not None
    identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    issuer = SessionTokenIssuer(
        signing_key=b"round-trip-session-signing-key-32b!",
        provider=_PROVIDER,
        audience="round-trip",
        token_expiry_hours=24,
        max_refresh_chain_hours=168,
        principal_is_active=lambda identity: identity_authority.is_active_human_identity(identity_id=identity, provider=_PROVIDER),
        sessions=RepositoryAuthSessionAuthority(engine),
    )
    token = issuer.mint(identity_id=_IDENTITY, username=_IDENTITY)
    service = SessionServiceImpl(
        engine,
        data_dir=data_dir,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.stream-round-trips"),
        session_operation_authority=repository,
        owner_instance_id=_OWNER,
    )
    progress = _OneSnapshotProgressRegistry()
    app = FastAPI()
    app.state.composer_async_operation_authority = authority
    app.state.composer_stream_auth = ComposerStreamAuthServices(
        provider=SsoAuthProvider(issuer=issuer, read_identity=lambda identity: identity_authority.read_identity(identity_id=identity)),
        decode=issuer.decode,
        identity_authority=identity_authority,
        sessions=service,
        provider_type=_PROVIDER,
    )
    app.state.composer_stream_permits = ComposerStreamPermits()
    app.state.composer_progress_registry = progress

    # The producer reads the loop clock for its 10 s heartbeat and sleeps 1 s
    # between iterations. Only the route module's view of asyncio changes:
    # its loop clock can be shifted forward and its sleep returns at once.
    import asyncio as real_asyncio

    offset = [0.0]
    shim = types.ModuleType("asyncio_round_trip_shim")
    shim.__dict__.update(real_asyncio.__dict__)

    async def no_wait(_seconds: float) -> None:
        return None

    shim.sleep = no_wait
    shim.get_running_loop = lambda: _ShiftedLoop(real_asyncio.get_running_loop(), offset)
    monkeypatch.setattr(operations, "asyncio", shim)
    return StreamHarness(
        engine=engine,
        app=app,
        token=token,
        session_id=session.id,
        operation_id=record.operation_id,
        running=running,
        service=service,
        progress=progress,
        clock_offset=offset,
        fence_id=started.session_operation_id,
        fence_epoch=started.session_operation_epoch,
    )


async def measure_frames(harness: StreamHarness) -> dict[str, FrameCost]:
    """Drive one stream as the response does: produce a frame, authorize, send."""
    harness.publish_progress()
    costs: dict[str, FrameCost] = {}
    with statement_ledger(harness.engine) as ledger:
        response, frames = await harness.open()
        mark = ledger.mark()
        assert await response._authorize()
        costs["pre_header"] = ledger.since(mark)

        async def deliver(name: str, expected_type: bytes) -> None:
            mark = ledger.mark()
            frame = await anext(frames)
            assert expected_type in frame, frame
            assert await response._authorize()
            costs[name] = ledger.since(mark)

        await deliver("status", b"event: status")
        await deliver("progress", b"event: progress")
        harness.clock_offset[0] = 11.0
        await deliver("heartbeat", b"event: heartbeat")
        await harness.settle_failed()
        await deliver("terminal", b"event: terminal")
        with pytest.raises(StopAsyncIteration):
            await anext(frames)
    return costs


# Per-frame cost on SQLite: (statements, clock samples, connections, write locks).
EXPECTED_SQLITE = {
    "pre_header": (5, 2, 2, 0),
    "status": (6, 3, 3, 0),
    "progress": (5, 2, 2, 0),
    "heartbeat": (5, 2, 2, 0),
    "terminal": (6, 3, 3, 0),
}


@pytest.fixture
def sqlite_harness(tmp_path, monkeypatch) -> Iterator[StreamHarness]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    try:
        yield build_stream_harness(engine, tmp_path, repository=SQLiteLocalSessionOperationAuthority(engine), monkeypatch=monkeypatch)
    finally:
        engine.dispose()


def test_statement_ledger_counts_a_known_read_and_misses_nothing(sqlite_harness) -> None:
    """Instrument control: one known statement is one count; an extra one is seen."""
    with statement_ledger(sqlite_harness.engine) as ledger:
        mark = ledger.mark()
        with sqlite_harness.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1").scalar_one()
        assert ledger.since(mark).summary() == (1, 0, 1, 0)
        mark = ledger.mark()
        with sqlite_harness.engine.connect() as conn:
            conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
            conn.exec_driver_sql("SELECT 1").scalar_one()
        assert ledger.since(mark).summary() == (2, 1, 1, 0)
        mark = ledger.mark()
        with sqlite_harness.engine.begin() as conn:
            conn.exec_driver_sql("SELECT 1").scalar_one()
        assert ledger.since(mark).summary() == (1, 0, 1, 1)


@pytest.mark.asyncio
async def test_per_frame_statements_clocks_and_connections_on_sqlite(sqlite_harness) -> None:
    costs = await measure_frames(sqlite_harness)
    assert {name: cost.summary() for name, cost in costs.items()} == EXPECTED_SQLITE, {name: cost.sql for name, cost in costs.items()}


@pytest.mark.asyncio
async def test_frame_count_detects_a_reintroduced_duplicate_read(sqlite_harness, monkeypatch) -> None:
    """Negative control: one extra clock read per authorization is caught."""
    from elspeth.web.coordination import database_clock

    authority = sqlite_harness.app.state.composer_async_operation_authority
    original = type(authority).get_with_database_now

    def with_extra_clock(self, *, session_id, operation_id):
        with self._engine.connect() as conn:
            database_clock.database_now(conn)
        return original(self, session_id=session_id, operation_id=operation_id)

    monkeypatch.setattr(type(authority), "get_with_database_now", with_extra_clock)
    costs = await measure_frames(sqlite_harness)
    assert costs["status"].summary() != EXPECTED_SQLITE["status"]
    assert costs["status"].statements == EXPECTED_SQLITE["status"][0] + 2


def test_one_statement_clock_matches_database_now_on_the_same_connection(sqlite_harness) -> None:
    """The select-list clock and ``database_now`` admit to the same type, zone and precision."""
    from sqlalchemy import select

    from elspeth.web.coordination.database_clock import database_now, database_now_column, database_now_value

    with sqlite_harness.engine.connect() as conn:
        raw = conn.execute(select(database_now_column(conn.dialect.name))).scalar_one()
        combined = database_now_value(raw)
        separate = database_now(conn)
        driver = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
    assert type(raw) is type(driver) is str
    assert type(combined) is type(separate) is datetime
    assert combined.tzinfo is UTC and separate.tzinfo is UTC
    assert combined.microsecond == separate.microsecond == 0
    assert abs((separate - combined).total_seconds()) <= 1
    with pytest.raises(NotImplementedError):
        database_now_column("mysql")


def test_record_and_clock_read_is_one_statement_and_keeps_the_record(sqlite_harness) -> None:
    authority = sqlite_harness.app.state.composer_async_operation_authority
    with statement_ledger(sqlite_harness.engine) as ledger:
        mark = ledger.mark()
        record, now = authority.get_with_database_now(session_id=sqlite_harness.session_id, operation_id=sqlite_harness.operation_id)
        full = ledger.since(mark)
        mark = ledger.mark()
        missing, missing_now = authority.get_with_database_now(session_id=sqlite_harness.session_id, operation_id=str(uuid4()))
        absent = ledger.since(mark)
    assert full.summary() == (1, 1, 1, 0)
    # No row means no select list to carry the clock: two statements, as before.
    assert absent.summary() == (2, 2, 1, 0)
    assert missing is None and missing_now.tzinfo is UTC
    assert record is not None and record.status == "running" and record.session_operation_id == sqlite_harness.fence_id
    assert now.tzinfo is UTC
    assert "result_json" in full.sql[0] and "request_json" not in full.sql[0]


@pytest.mark.asyncio
async def test_terminal_frame_still_verifies_the_settled_result_digest(sqlite_harness) -> None:
    """Every poll is the full Tier-1 read, so a forged settled digest stops the stream."""
    from sqlalchemy import text, update

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.models import composer_async_operations_table as jobs

    await sqlite_harness.settle_failed()
    with sqlite_harness.engine.begin() as conn:
        # Forge the settled row: the immutability trigger is what stops this
        # in production, so the test removes it to reach the reader's check.
        conn.execute(text("DROP TRIGGER trg_composer_async_operations_transition_guard"))
        conn.execute(update(jobs).where(jobs.c.operation_id == sqlite_harness.operation_id).values(result_sha256="0" * 64))
    _response, frames = await sqlite_harness.open()
    with pytest.raises(AuditIntegrityError):
        await anext(frames)


@pytest.mark.asyncio
async def test_principal_read_refuses_revoked_family_disabled_identity_and_wrong_provider(sqlite_harness) -> None:
    from sqlalchemy import update

    from elspeth.web.sessions.models import auth_session_families_table, identities_table

    services = sqlite_harness.app.state.composer_stream_auth
    principal = UserIdentity(user_id=_IDENTITY, username=_IDENTITY)

    async def allowed() -> bool:
        return await services.authorize(token=sqlite_harness.token, principal=principal, session_id=sqlite_harness.session_id)

    assert await allowed()
    authority = services.identity_authority
    family_id = services.decode(sqlite_harness.token).binding.family_id
    assert (
        authority.read_stream_session_scope(
            identity_id=_IDENTITY, provider="local", family_id=family_id, role="user", session_id=sqlite_harness.session_id
        )
        is None
    )
    assert (
        authority.read_stream_session_scope(
            identity_id=_IDENTITY, provider=_PROVIDER, family_id="another-family", role="user", session_id=sqlite_harness.session_id
        )
        is None
    )
    assert (
        authority.read_stream_session_scope(
            identity_id=_IDENTITY, provider=_PROVIDER, family_id=family_id, role="admin", session_id=sqlite_harness.session_id
        )
        is None
    )
    with pytest.raises(ValueError):
        authority.read_stream_session_scope(
            identity_id=" ", provider=_PROVIDER, family_id=family_id, role="user", session_id=sqlite_harness.session_id
        )
    with sqlite_harness.engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == _IDENTITY).values(access_state="disabled"))
    assert not await allowed()
    with sqlite_harness.engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == _IDENTITY).values(access_state="active"))
    assert await allowed()
    with sqlite_harness.engine.begin() as conn:
        conn.execute(
            update(auth_session_families_table)
            .where(auth_session_families_table.c.family_id == family_id)
            .values(revoked_at=datetime.now(UTC))
        )
    assert not await allowed()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["logout", "password-reset"])
async def test_local_stream_authorization_refuses_logout_and_password_reset(tmp_path, revoke) -> None:
    """The local provider's ``auth.db`` credential check still gates every send."""
    from tests.helpers.composer_operations import build_composer_operation_app

    setup = await build_composer_operation_app(tmp_path, timeout_seconds=60)
    services = setup.app.state.composer_stream_auth
    principal = UserIdentity(user_id="transport-user", username="transport-user")
    assert await services.authorize(token=setup.token, principal=principal, session_id=setup.session_id)
    if revoke == "logout":
        services.provider._issuer.revoke(setup.token)
    else:
        services.provider.set_password("transport-user", "a different transport password")
    assert not await services.authorize(token=setup.token, principal=principal, session_id=setup.session_id)
