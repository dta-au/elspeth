"""Spawned processes prove ticket transfer, single-use and post-lock expiry."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Iterator
from datetime import timedelta
from hashlib import sha256
from multiprocessing.connection import Connection
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, text, update
from sqlalchemy.engine import make_url
from tests.unit.web.execution.test_durable_websocket_ticket import seed_ticket_run

from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.websocket_ticket_authority import RepositorySessionWebsocketTicketAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, websocket_tickets_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def ticket_postgres(external_deployment_postgres_url: str) -> Iterator[Engine]:
    # Ticket-only seeds omit execution fences. Keep those rows out of the
    # shared database scanned by the global recovery and membership proofs.
    database = f"ticket_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with control.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
        engine = create_session_engine(
            make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False)
        )
        try:
            initialize_session_schema(engine)
            yield engine
        finally:
            engine.dispose()
            with control.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    finally:
        control.dispose()


def _issue_process(url: str, run_id: str, user: UserIdentity, pipe: Connection) -> None:
    engine = create_session_engine(url)
    try:
        issued = RepositorySessionWebsocketTicketAuthority(engine).issue(run_id=run_id, user=user)
        pipe.send(issued.ticket)
    finally:
        engine.dispose()
        pipe.close()


def _consume_process(url: str, run_id: str, ticket: str, pipe: Connection) -> None:
    consumer_url = make_url(url).update_query_dict({"application_name": run_id}).render_as_string(hide_password=False)
    engine = create_session_engine(consumer_url)
    try:
        pipe.send("ready")
        assert pipe.recv() == "consume"
        result = RepositorySessionWebsocketTicketAuthority(engine).consume(ticket=ticket, run_id=run_id)
        pipe.send(None if result is None else result.user_id)
    finally:
        engine.dispose()
        pipe.close()


def _await_consumer_lock(engine: Engine, run_id: str) -> None:
    deadline = time.monotonic() + 10
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :name AND wait_event_type = 'Lock'"),
                {"name": run_id},
            ).scalar_one()
            if waiting:
                return
            time.sleep(0.01)
    pytest.fail("consumer did not block on the held database row lock")


def test_peer_consumes_after_issuer_exits_and_only_one_process_wins(ticket_postgres: Engine) -> None:
    _, run_id, user = seed_ticket_run(ticket_postgres)
    url = ticket_postgres.url.render_as_string(hide_password=False)
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    issuer = ctx.Process(target=_issue_process, args=(url, run_id, user, child))
    issuer.start()
    assert parent.poll(30)
    ticket = parent.recv()
    issuer.join(30)
    assert issuer.exitcode == 0
    parent.close()
    child.close()
    workers = []
    pipes = []
    try:
        for _ in range(2):
            parent, child = ctx.Pipe()
            process = ctx.Process(target=_consume_process, args=(url, run_id, ticket, child))
            process.start()
            child.close()
            workers.append(process)
            pipes.append(parent)
        for pipe in pipes:
            assert pipe.poll(30)
            assert pipe.recv() == "ready"
        for pipe in pipes:
            pipe.send("consume")
        outcomes = []
        for pipe in pipes:
            assert pipe.poll(30)
            outcomes.append(pipe.recv())
        assert outcomes.count(user.user_id) == 1
        assert outcomes.count(None) == 1
        for process in workers:
            process.join(30)
            assert process.exitcode == 0
    finally:
        for process in workers:
            if process.is_alive():
                process.terminate()
            process.join(30)
        for pipe in pipes:
            pipe.close()


def test_identity_revocation_committed_while_consumer_waits_refuses(ticket_postgres: Engine) -> None:
    _, run_id, user = seed_ticket_run(ticket_postgres)
    ticket = RepositorySessionWebsocketTicketAuthority(ticket_postgres).issue(run_id=run_id, user=user)
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(
        target=_consume_process, args=(ticket_postgres.url.render_as_string(hide_password=False), run_id, ticket.ticket, child)
    )
    try:
        with ticket_postgres.begin() as conn:
            conn.execute(select(identities_table).where(identities_table.c.identity_id == user.user_id).with_for_update()).one()
            process.start()
            child.close()
            assert parent.poll(30)
            assert parent.recv() == "ready"
            parent.send("consume")
            _await_consumer_lock(ticket_postgres, run_id)
            conn.execute(update(identities_table).where(identities_table.c.identity_id == user.user_id).values(access_state="disabled"))
        assert parent.poll(30)
        assert parent.recv() is None
        process.join(30)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
        process.join(30)
        parent.close()


def test_expiry_uses_database_time_after_ticket_lock(ticket_postgres: Engine) -> None:
    _, run_id, user = seed_ticket_run(ticket_postgres)
    issued = RepositorySessionWebsocketTicketAuthority(ticket_postgres).issue(run_id=run_id, user=user)
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(
        target=_consume_process, args=(ticket_postgres.url.render_as_string(hide_password=False), run_id, issued.ticket, child)
    )
    try:
        with ticket_postgres.begin() as conn:
            record = conn.execute(select(websocket_tickets_table).where(websocket_tickets_table.c.run_id == run_id).with_for_update()).one()
            process.start()
            child.close()
            assert parent.poll(30)
            assert parent.recv() == "ready"
            parent.send("consume")
            _await_consumer_lock(ticket_postgres, run_id)
            # Transaction's timestamp remains before expiry, while the fresh
            # database clock sampled after acquiring the ticket lock is later.
            expires = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one() + timedelta(seconds=1)
            conn.execute(
                update(websocket_tickets_table)
                .where(websocket_tickets_table.c.ticket_digest == record.ticket_digest)
                .values(expires_at=expires)
            )
            conn.exec_driver_sql("SELECT pg_sleep(1.2)")
        assert parent.poll(30)
        assert parent.recv() is None
        process.join(30)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
        process.join(30)
        parent.close()


def test_issuance_cleanup_skips_locked_expired_ticket_and_retries_later(ticket_postgres: Engine) -> None:
    _, old_run_id, old_user = seed_ticket_run(ticket_postgres)
    _, run_id, user = seed_ticket_run(ticket_postgres)
    authority = RepositorySessionWebsocketTicketAuthority(ticket_postgres)
    locked_ticket = authority.issue(run_id=old_run_id, user=old_user)
    removable_ticket = authority.issue(run_id=old_run_id, user=old_user)
    locked_digest = sha256(locked_ticket.ticket.encode()).hexdigest()
    removable_digest = sha256(removable_ticket.ticket.encode()).hexdigest()
    with ticket_postgres.begin() as conn:
        expired = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one() - timedelta(seconds=1)
        conn.execute(update(websocket_tickets_table).values(expires_at=expired))
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_issue_process, args=(ticket_postgres.url.render_as_string(hide_password=False), run_id, user, child))
    try:
        with ticket_postgres.begin() as conn:
            conn.execute(
                select(websocket_tickets_table).where(websocket_tickets_table.c.ticket_digest == locked_digest).with_for_update()
            ).one()
            process.start()
            child.close()
            # Issuance must finish while the expired credential remains locked.
            assert parent.poll(30)
            issued = parent.recv()
            process.join(30)
            assert process.exitcode == 0
            remaining = set(conn.execute(select(websocket_tickets_table.c.ticket_digest)).scalars())
            assert locked_digest in remaining
            assert removable_digest not in remaining
        authority.issue(run_id=run_id, user=user)
        with ticket_postgres.connect() as conn:
            assert locked_digest not in set(conn.execute(select(websocket_tickets_table.c.ticket_digest)).scalars())
        assert authority.consume(ticket=locked_ticket.ticket, run_id=old_run_id) is None
        assert authority.consume(ticket=issued, run_id=run_id) == UserIdentity(user.user_id, "current-name")
        assert authority.consume(ticket=issued, run_id=run_id) is None
    finally:
        if process.is_alive():
            process.terminate()
        process.join(30)
        parent.close()
