"""Real PostgreSQL pinned-snapshot proof for add_message_with_transcript (F-1 / T1).

The freeform-500 defect required a reader whose snapshot predates the
committed insert (read/write-splitting proxy, pooled REPEATABLE READ
session). This module reproduces that condition faithfully on a real
PostgreSQL: a second connection holds a REPEATABLE READ transaction whose
snapshot is pinned before the write commits. The stale view stays stale
across the commit; the combined method's same-transaction read contains
its own insert regardless — the non-vacuous post-fix contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.postgres_target import postgres_test_target
from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance
from tests.unit.web.sessions.test_service import _admit_durable_ingress, _durable_ingress

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, message_ingress_receipts_table, sessions_table
from elspeth.web.sessions.protocol import MessageIngressFresh
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    with postgres_test_target(driver="psycopg", mem_limit="256m", nano_cpus=500_000_000) as postgres_url:
        engine = create_session_engine(postgres_url)
        initialize_session_schema(engine)
        _register_instance(engine, instance_id="ingress-pg-owner", lease_delta=timedelta(minutes=10))
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
        log=structlog.get_logger("test.add-message-transcript.postgres"),
        owner_instance_id="ingress-pg-owner",
    )


@pytest.mark.asyncio
async def test_postgres_combined_read_sees_its_own_write_despite_repeatable_read_snapshot(
    postgres_service: SessionServiceImpl,
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    session = await postgres_service.create_session("alice", "PG stale reader", "local")

    def _count(conn) -> int:
        return conn.execute(
            select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))
        ).scalar_one()

    # P4-D6 family A2b: the seed row is a fenced session write too, so the
    # turn's COMPOSE operation is acquired BEFORE it and both writes run under
    # the one operation, as they do on the live route. The stale reader's
    # snapshot is pinned by its first read below, not by connecting, so the
    # earlier acquire does not change what this test measures.
    context = postgres_service.session_operation_authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=postgres_service.session_operation_owner_instance_id,
        lease_seconds=postgres_service.session_operation_lease_seconds,
    )
    stale_reader = postgres_engine.connect().execution_options(isolation_level="REPEATABLE READ")
    try:
        await postgres_service.add_message(
            session.id,
            "user",
            "seed",
            writer_principal="route_user_message",
            session_operation_context=context,
        )

        # First read begins the transaction and pins the snapshot at one
        # seed row — the pooled-stale-reader condition from production.
        assert _count(stale_reader) == 1

        postgres_service.session_operation_authority.release(context)
        result = await _durable_ingress(
            postgres_service,
            session.id,
            "user",
            "hello",
            operation_id=uuid4(),
            requested_state_id=None,
            writer_principal="route_user_message",
        )
        assert isinstance(result, MessageIngressFresh)
        record, transcript = result.message, result.transcript

        # The REPEATABLE READ snapshot still cannot see the committed
        # insert — the stale-reader condition is real...
        assert _count(stale_reader) == 1
        # ...and irrelevant to the combined method, whose transcript was
        # read inside the insert's own transaction.
        assert [message.content for message in transcript] == ["seed", "hello"]
        assert transcript[-1].id == record.id
        sequence_numbers = [message.sequence_no for message in transcript]
        assert sequence_numbers == sorted(sequence_numbers)
    finally:
        stale_reader.rollback()
        stale_reader.close()

    # After the pinned transaction ends, a fresh read converges.
    assert [message.content for message in await postgres_service.get_messages(session.id, limit=None)] == ["seed", "hello"]


@pytest.mark.asyncio
async def test_postgres_two_service_instances_accept_one_receipt_for_same_key_and_session_cascade(
    postgres_service: SessionServiceImpl,
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    with postgres_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    session = await postgres_service.create_session("alice", "PG ingress race", "local")
    second = SessionServiceImpl(
        postgres_engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.add-message-transcript.second"),
        session_operation_authority=postgres_service.session_operation_authority,
        owner_instance_id=postgres_service.session_operation_owner_instance_id,
    )
    request_id = uuid4()

    def admit_one(service):
        return _admit_durable_ingress(service, session.id, operation_id=request_id, content="one admission", requested_state_id=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        admissions = list(pool.map(admit_one, (postgres_service, second)))
    assert sorted(item[2] for item in admissions) == [False, True]
    assert admissions[0][1].operation_id == admissions[1][1].operation_id
    authority = admissions[0][0]
    (claim,) = authority.claim_next(limit=1)
    context = postgres_service.session_operation_authority.start_composer_async_operation(
        claim, owner_instance_id=postgres_service.session_operation_owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning

    try:
        fresh = await postgres_service.add_message_with_transcript(
            session.id,
            "user",
            "one admission",
            operation_id=request_id,
            requested_state_id=None,
            writer_principal="route_user_message",
            session_operation_context=context,
            running=ComposerOperationRunning(claim=claim, session_operation_context=context),
        )
        assert isinstance(fresh, MessageIngressFresh)
        with postgres_engine.connect() as conn:
            assert (
                conn.scalar(
                    select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))
                )
                == 1
            )
            assert (
                conn.scalar(
                    select(func.count())
                    .select_from(message_ingress_receipts_table)
                    .where(message_ingress_receipts_table.c.session_id == str(session.id))
                )
                == 1
            )
        with postgres_engine.begin() as conn:
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(
                    update(message_ingress_receipts_table)
                    .where(message_ingress_receipts_table.c.session_id == str(session.id))
                    .values(requested_state_id=None)
                )
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(delete(message_ingress_receipts_table).where(message_ingress_receipts_table.c.session_id == str(session.id)))
    finally:
        postgres_service.session_operation_authority.release(context)

    with postgres_engine.begin() as conn:
        conn.execute(delete(sessions_table).where(sessions_table.c.id == str(session.id)))
    with postgres_engine.connect() as conn:
        assert (
            conn.scalar(
                select(func.count())
                .select_from(message_ingress_receipts_table)
                .where(message_ingress_receipts_table.c.session_id == str(session.id))
            )
            == 0
        )
