"""PostgreSQL durability proof for mode-neutral fork/revert receipts."""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import Engine, create_engine, delete, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.sessions import operation_receipts as receipt_module
from elspeth.web.sessions.models import session_operation_receipt_events_table, session_operation_receipts_table, sessions_table
from elspeth.web.sessions.operation_receipts import read_operation_receipt, reserve_operation_receipt, settle_operation_receipt
from elspeth.web.sessions.protocol import OperationReceiptActive, OperationReceiptClaimed, OperationReceiptFailed
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        identifier = f"elspeth_receipts_{uuid4().hex}"
        assert re.fullmatch(r"[a-z0-9_]+", identifier)
        admin = create_engine(postgres_url)
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{identifier}"')
        engine = create_engine(make_url(postgres_url).set(database=identifier))
        try:
            yield engine
        finally:
            engine.dispose()
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{identifier}" WITH (FORCE)')
            admin.dispose()


def test_postgres_receipt_terminal_event_is_retained_and_immutable(postgres_engine: Engine) -> None:
    initialize_session_schema(postgres_engine)
    assert {"session_operation_receipts", "session_operation_receipt_events"} <= set(inspect(postgres_engine).get_table_names())
    with postgres_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        now = datetime.now(UTC)
        parent_id = uuid4()
        conn.execute(
            sessions_table.insert().values(
                id=str(parent_id), user_id="alice", auth_provider_type="local", title="parent", created_at=now, updated_at=now
            )
        )
        claim = reserve_operation_receipt(
            conn,
            session_id=parent_id,
            operation_id="revert-pg",
            kind="state_revert",
            request_hash="a" * 64,
            actor="test",
            lease_seconds=60,
            now=now,
        )
        assert isinstance(claim, OperationReceiptClaimed)
        settled = settle_operation_receipt(conn, claim.fence, now=now, actor="test", failure_code="operation_failed")
        assert isinstance(settled, OperationReceiptFailed)
    with postgres_engine.begin() as conn:
        row = read_operation_receipt(conn, session_id=parent_id, operation_id="revert-pg")
        assert row is not None and row["status"] == "failed"
        assert conn.execute(select(session_operation_receipt_events_table.c.event_kind)).scalars().all() == ["claimed", "failed"]
    with pytest.raises(DBAPIError), postgres_engine.begin() as conn:
        conn.execute(delete(session_operation_receipt_events_table))
    with pytest.raises(DBAPIError), postgres_engine.begin() as conn:
        conn.execute(session_operation_receipts_table.update().values(failure_code="custody_error"))


@pytest.mark.asyncio
async def test_postgres_receipt_reader_uses_one_snapshot_across_row_and_event_selects(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent settlement cannot fabricate corruption from two snapshots."""
    initialize_session_schema(postgres_engine)
    with postgres_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    service = SessionServiceImpl(postgres_engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.receipts.pg"))
    session = await service.create_session("alice", "receipt race", "local")
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    selected_row = threading.Event()
    release_event_read = threading.Event()
    operation_id = "revert-read-split"
    request_hash = "b" * 64
    try:
        claim = await service.reserve_operation_receipt(
            session_id=session.id,
            operation_id=operation_id,
            kind="state_revert",
            request_hash=request_hash,
            actor="test",
            lease_seconds=60,
            session_operation_context=context,
        )
        assert isinstance(claim, OperationReceiptClaimed)
        original_validate_events = receipt_module._validate_events

        def pause_after_row_select(conn, row):
            if row["operation_id"] == operation_id and not selected_row.is_set():
                selected_row.set()
                if not release_event_read.wait(timeout=10):
                    raise AssertionError("receipt event read barrier timed out")
            return original_validate_events(conn, row)

        with monkeypatch.context() as patcher:
            patcher.setattr(receipt_module, "_validate_events", pause_after_row_select)
            reader = asyncio.create_task(
                service.get_operation_receipt(
                    session_id=session.id,
                    operation_id=operation_id,
                    kind="state_revert",
                    request_hash=request_hash,
                )
            )
            try:
                assert await asyncio.to_thread(selected_row.wait, 5), "reader did not reach the row/event barrier"
                settled = await service.fail_operation_receipt(
                    claim.fence,
                    failure_code="operation_failed",
                    actor="test",
                    session_operation_context=context,
                )
                assert isinstance(settled, OperationReceiptFailed)
            finally:
                release_event_read.set()
            old_snapshot = await reader
        assert isinstance(old_snapshot, OperationReceiptActive)
        terminal = await service.get_operation_receipt(
            session_id=session.id,
            operation_id=operation_id,
            kind="state_revert",
            request_hash=request_hash,
        )
        assert isinstance(terminal, OperationReceiptFailed)
        assert terminal.failure_code == "operation_failed"

        # A consistent snapshot must not turn into a bypass of real corruption.
        with postgres_engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER trg_session_operation_receipt_events_no_delete ON session_operation_receipt_events")
            conn.execute(
                delete(session_operation_receipt_events_table).where(
                    session_operation_receipt_events_table.c.session_id == str(session.id),
                    session_operation_receipt_events_table.c.operation_id == operation_id,
                    session_operation_receipt_events_table.c.event_kind == "failed",
                )
            )
        with pytest.raises(AuditIntegrityError, match="terminal event"):
            await service.get_operation_receipt(
                session_id=session.id,
                operation_id=operation_id,
                kind="state_revert",
                request_hash=request_hash,
            )
    finally:
        release_event_read.set()
        await service._run_sync(service.session_operation_authority.release, context)
