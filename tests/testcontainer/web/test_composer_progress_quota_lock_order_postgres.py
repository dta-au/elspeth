"""Real progress HTTP reads must coexist with session-owned token settlement."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, event, select, text, update
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.composer_progress_authority import DatabaseComposerProgressRegistry, SessionComposerProgressAuthority
from elspeth.web.coordination.quota_authority import TokenUsageEntry, record_token_usage_on_connection
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, sessions_table, token_usage_ledger_table
from elspeth.web.sessions.routes.composer.state import get_current_user, router
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def progress_database(external_deployment_postgres_url: str) -> Iterator[tuple[Engine, Engine, str]]:
    database = f"progress_quota_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False)
    writer = create_session_engine(url)
    reader = create_session_engine(url)
    try:
        initialize_session_schema(writer)
        with writer.begin() as conn:
            ensure_test_identity(conn, identity_id="owner")
            ensure_test_identity(conn, identity_id="other")
        session = PostgresSessionOperationRepository(writer).create_session_with_initial_fence(
            user_id="owner", title="Before settlement", auth_provider_type="local", owner_instance_id="seed", lease_seconds=120
        )
        yield writer, reader, str(session.id)
    finally:
        reader.dispose()
        writer.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def test_progress_http_and_token_settlement_do_not_deadlock(progress_database: tuple[Engine, Engine, str]) -> None:
    writer, reader, session_id = progress_database
    authority = SessionComposerProgressAuthority(reader, owner_instance_id="poller")
    app = FastAPI()
    app.state.composer_progress_registry = DatabaseComposerProgressRegistry(authority)
    app.state.session_service = SessionServiceImpl(
        writer, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.progress-quota"), owner_instance_id="web"
    )
    app.state.settings = WebSettings(
        auth_provider="local",
        composer_max_composition_turns=20,
        composer_max_discovery_turns=20,
        composer_timeout_seconds=300,
        composer_transport_idle_ceiling_seconds=360,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key="0" * 64,
    )
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(user_id="owner", username="owner")
    app.include_router(router, prefix="/api/sessions")
    session_locked = Event()
    progress_select_entered = Event()
    at = datetime.now(UTC)
    entry = TokenUsageEntry(
        model="provider/model",
        prompt_tokens=13,
        completion_tokens=7,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        call_id=str(uuid4()),
        recorded_at=at,
    )

    def observe_progress_select(
        _conn: Connection, _cursor: object, statement: str, _parameters: object, _context: object, _many: bool
    ) -> None:
        # Only the authority owns this engine: route ownership lookup cannot
        # release this barrier. On the old implementation identity FOR UPDATE
        # has already completed when the session SELECT is submitted.
        if statement.lstrip().upper().startswith("SELECT") and "sessions" in statement:
            progress_select_entered.set()

    def settle() -> tuple[str, ...]:
        with writer.begin() as conn:
            conn.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
            conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == session_id).with_for_update()).one()
            session_locked.set()
            assert progress_select_entered.wait(10), "progress never reached its session authorization query"
            ids = record_token_usage_on_connection(
                conn, session_id=session_id, source="composer", run_id=None, entries=(entry,), recorded_at=at
            )
            conn.execute(update(sessions_table).where(sessions_table.c.id == session_id).values(title="Settlement committed"))
            return ids

    event.listen(reader, "before_cursor_execute", observe_progress_select)
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as workers:
            settlement = workers.submit(settle)
            assert session_locked.wait(10)
            poll = workers.submit(client.get, f"/api/sessions/{session_id}/composer-progress")
            response = poll.result(timeout=20)
            entry_ids = settlement.result(timeout=20)
        assert response.status_code == 200
        assert response.json()["session_id"] == session_id
        assert progress_select_entered.is_set()
        assert len(entry_ids) == 1
    finally:
        event.remove(reader, "before_cursor_execute", observe_progress_select)

    # Replay the same provider call through the real ledger API: no duplicate
    # charge, and the caller's committed transaction marker remains unchanged.
    with writer.begin() as conn:
        assert (
            record_token_usage_on_connection(conn, session_id=session_id, source="composer", run_id=None, entries=(entry,), recorded_at=at)
            == ()
        )
        rows = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == session_id)).all()
        assert len(rows) == 1
        assert rows[0].entry_id == entry_ids[0]
        assert (rows[0].identity_id, rows[0].prompt_tokens, rows[0].completion_tokens) == ("owner", 13, 7)
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == session_id)).scalar_one() == "Settlement committed"


@pytest.mark.parametrize("refusal", ["inactive", "nonowner", "archived", "missing"])
def test_progress_authorization_remains_fail_closed(progress_database: tuple[Engine, Engine, str], refusal: str) -> None:
    writer, reader, session_id = progress_database
    authority = SessionComposerProgressAuthority(reader, owner_instance_id="poller")
    assert authority.get_latest(session_id, "owner").session_id == session_id
    with writer.begin() as conn:
        if refusal == "inactive":
            conn.execute(update(identities_table).where(identities_table.c.identity_id == "owner").values(access_state="disabled"))
        elif refusal == "archived":
            conn.execute(update(sessions_table).where(sessions_table.c.id == session_id).values(archived_at=datetime.now(UTC)))
    with pytest.raises(PermissionError):
        authority.get_latest(str(uuid4()) if refusal == "missing" else session_id, "other" if refusal == "nonowner" else "owner")
    if refusal == "inactive":
        with pytest.raises(PermissionError):
            authority.list_active("owner")
    else:
        assert authority.list_active("other" if refusal == "nonowner" else "owner") == ()


@pytest.mark.parametrize("operation", ["get_latest", "list_active"])
def test_progress_read_keeps_authorization_and_snapshot_in_one_database_view(
    progress_database: tuple[Engine, Engine, str], operation: str
) -> None:
    writer, reader, session_id = progress_database
    publisher = SessionComposerProgressAuthority(writer, owner_instance_id="publisher")
    authority = SessionComposerProgressAuthority(reader, owner_instance_id="reader")
    lease = publisher.begin_request(session_id, "owner")
    generation = publisher.bind_request(lease, "request")
    publisher.publish(lease, generation, "request", ComposerProgressEvent(phase="calling_model", headline="Original snapshot"))
    authorized = Event()
    resume = Event()

    def pause_after_authorization(
        _conn: Connection, _cursor: object, statement: str, _parameters: object, _context: object, _many: bool
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "identities" in statement:
            authorized.set()
            assert resume.wait(10), "snapshot read was not released"

    def read():
        if operation == "get_latest":
            return authority.get_latest(session_id, "owner")
        snapshots = authority.list_active("owner")
        assert len(snapshots) == 1
        return snapshots[0]

    event.listen(reader, "after_cursor_execute", pause_after_authorization)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            poll = workers.submit(read)
            try:
                assert authorized.wait(10)
                publisher.publish(lease, generation, "request", ComposerProgressEvent(phase="saving", headline="Later snapshot"))
                with writer.begin() as conn:
                    conn.execute(update(identities_table).where(identities_table.c.identity_id == "owner").values(access_state="disabled"))
            finally:
                resume.set()
            snapshot = poll.result(timeout=10)
        assert snapshot.headline == "Original snapshot"
        assert snapshot.inflight_requests == 1
    finally:
        event.remove(reader, "after_cursor_execute", pause_after_authorization)
    with pytest.raises(PermissionError):
        read()


def test_failed_settlement_rolls_back_token_usage_and_its_transaction(progress_database: tuple[Engine, Engine, str]) -> None:
    writer, _reader, session_id = progress_database
    with pytest.raises(RuntimeError, match="abort settlement"), writer.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == session_id).values(title="Uncommitted settlement"))
        record_token_usage_on_connection(
            conn,
            session_id=session_id,
            source="composer",
            run_id=None,
            entries=(
                TokenUsageEntry(model="model", prompt_tokens=13, completion_tokens=7, cached_prompt_tokens=None, reasoning_tokens=None),
            ),
            recorded_at=datetime.now(UTC),
        )
        raise RuntimeError("abort settlement")
    with writer.connect() as conn:
        assert conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == session_id)).all() == []
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == session_id)).scalar_one() == "Before settlement"


@pytest.mark.parametrize("operation", ["begin", "heartbeat", "bind", "publish", "replay", "clear"])
def test_progress_session_waiter_does_not_hold_identity_lock(progress_database: tuple[Engine, Engine, str], operation: str) -> None:
    writer, reader, session_id = progress_database
    authority = SessionComposerProgressAuthority(reader, owner_instance_id="writer")
    lease = authority.begin_request(session_id, "owner")
    generation = authority.bind_request(lease, "request")
    pending = Event()
    waiter_pids: list[int] = []

    def observe_session_lock(conn: Connection, _cursor: object, statement: str, _parameters: object, _context: object, _many: bool) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "sessions" in statement and "FOR UPDATE" in statement:
            waiter_pids.append(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            pending.set()

    def act() -> None:
        if operation == "begin":
            authority.begin_request(session_id, "owner")
        elif operation == "heartbeat":
            authority.heartbeat_request(lease)
        elif operation == "bind":
            authority.bind_request(lease, "next-request")
        elif operation == "publish":
            authority.publish(lease, generation, "request", ComposerProgressEvent(phase="saving", headline="Saving"))
        elif operation == "replay":
            authority.replay(session_id, "owner", "request", ComposerProgressEvent(phase="saving", headline="Saving"), lease=lease)
        else:
            assert operation == "clear"
            authority.clear(session_id, "owner")

    event.listen(reader, "before_cursor_execute", observe_session_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            with writer.begin() as holder:
                holder_pid = holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                holder.execute(select(sessions_table.c.id).where(sessions_table.c.id == session_id).with_for_update()).one()
                mutation = workers.submit(act)
                assert pending.wait(10)
                deadline = monotonic() + 10
                while True:
                    with writer.connect() as observer:
                        blockers = observer.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pids[-1]}).scalar_one()
                    if holder_pid in blockers:
                        break
                    assert monotonic() < deadline, "progress mutation did not block on the held session"
                # This is the foreign-key lock needed by ledger insertion. A
                # session waiter that already holds identity FOR UPDATE makes
                # this immediate check fail with LockNotAvailable.
                holder.execute(
                    select(identities_table.c.identity_id)
                    .where(identities_table.c.identity_id == "owner")
                    .with_for_update(read=True, key_share=True, nowait=True)
                ).one()
            mutation.result(timeout=10)
    finally:
        event.remove(reader, "before_cursor_execute", observe_session_lock)
