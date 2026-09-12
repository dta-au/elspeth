"""Database ticket authority refuses stale ownership and stores only digests."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import OperationalError

from elspeth.web.auth.models import AuthenticationError, UserIdentity
from elspeth.web.coordination.websocket_ticket_authority import RepositorySessionWebsocketTicketAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    composition_states_table,
    identities_table,
    runs_table,
    sessions_table,
    websocket_tickets_table,
)
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity


def seed_ticket_run(engine: Engine) -> tuple[str, str, UserIdentity]:
    """Create a real identity, session, composition and run in the schema."""
    now = datetime.now(UTC)
    identity_id, session_id, state_id, run_id = (str(uuid4()) for _ in range(4))
    with engine.begin() as conn:
        conn.execute(
            insert(identities_table).values(
                identity_id=identity_id,
                provider="local",
                subject=identity_id,
                username="current-name",
                access_state="active",
                first_seen_at=now,
            )
        )
        conn.execute(
            insert(sessions_table).values(
                id=session_id, user_id=identity_id, title="ticket test", created_at=now, updated_at=now, auth_provider_type="local"
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id=state_id, session_id=session_id, version=1, is_valid=True, created_at=now, provenance="session_seed"
            )
        )
        conn.execute(insert(runs_table).values(id=run_id, session_id=session_id, state_id=state_id, status="running", started_at=now))
    return session_id, run_id, UserIdentity(user_id=identity_id, username="stale-name")


@pytest.fixture
def ticket_engine(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'tickets.db'}")
    initialize_session_schema(engine)
    yield engine
    engine.dispose()


def test_digest_only_single_use_and_current_identity(ticket_engine):
    _, run_id, user = seed_ticket_run(ticket_engine)
    first = RepositorySessionWebsocketTicketAuthority(ticket_engine)
    second = RepositorySessionWebsocketTicketAuthority(ticket_engine)
    issued = first.issue(run_id=run_id, user=user)
    with ticket_engine.connect() as conn:
        row = conn.execute(select(websocket_tickets_table)).one()
    assert row.ticket_digest == sha256(issued.ticket.encode()).hexdigest()
    assert issued.ticket not in str(row)
    assert second.consume(ticket=issued.ticket, run_id=run_id) == UserIdentity(user.user_id, "current-name")
    assert first.consume(ticket=issued.ticket, run_id=run_id) is None


@pytest.mark.parametrize("refusal", ["wrong-run", "expired", "disabled", "wrong-owner", "archived", "provider"])
def test_consumption_rechecks_live_authorization(ticket_engine, refusal):
    session_id, run_id, user = seed_ticket_run(ticket_engine)
    authority = RepositorySessionWebsocketTicketAuthority(ticket_engine)
    issued = authority.issue(run_id=run_id, user=user)
    with ticket_engine.begin() as conn:
        if refusal == "expired":
            conn.execute(update(websocket_tickets_table).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        elif refusal == "disabled":
            conn.execute(update(identities_table).values(access_state="disabled"))
        elif refusal == "wrong-owner":
            other_owner = str(uuid4())
            ensure_test_identity(conn, identity_id=other_owner)
            conn.execute(update(sessions_table).where(sessions_table.c.id == session_id).values(user_id=other_owner))
        elif refusal == "archived":
            conn.execute(update(sessions_table).values(archived_at=datetime.now(UTC)))
        elif refusal == "provider":
            conn.execute(update(websocket_tickets_table).values(auth_provider_type="oidc"))
    assert authority.consume(ticket=issued.ticket, run_id=str(uuid4()) if refusal == "wrong-run" else run_id) is None
    assert authority.consume(ticket=issued.ticket, run_id=run_id) is None


def test_issue_rejects_identity_that_does_not_own_run(ticket_engine):
    _, run_id, _ = seed_ticket_run(ticket_engine)
    _, _, other = seed_ticket_run(ticket_engine)
    authority = RepositorySessionWebsocketTicketAuthority(ticket_engine)
    with pytest.raises(AuthenticationError):
        authority.issue(run_id=run_id, user=other)
    with ticket_engine.connect() as conn:
        assert conn.execute(select(websocket_tickets_table)).all() == []


def test_database_failure_is_not_local_admission(ticket_engine):
    _, run_id, user = seed_ticket_run(ticket_engine)
    authority = RepositorySessionWebsocketTicketAuthority(ticket_engine)
    issued = authority.issue(run_id=run_id, user=user)
    websocket_tickets_table.drop(ticket_engine)
    with pytest.raises(OperationalError):
        authority.consume(ticket=issued.ticket, run_id=run_id)
    with pytest.raises(OperationalError):
        authority.issue(run_id=run_id, user=user)


def test_issue_reclaims_expired_tickets_in_bounded_batches_across_retained_runs(ticket_engine):
    _, old_run_id, old_user = seed_ticket_run(ticket_engine)
    _, run_id, user = seed_ticket_run(ticket_engine)
    authority = RepositorySessionWebsocketTicketAuthority(ticket_engine)
    consumed = authority.issue(run_id=old_run_id, user=old_user)
    assert authority.consume(ticket=consumed.ticket, run_id=old_run_id) is not None
    live = authority.issue(run_id=old_run_id, user=old_user)
    now = datetime.now(UTC)
    expired_digests = {sha256(f"expired-{number}".encode()).hexdigest() for number in range(105)}
    with ticket_engine.begin() as conn:
        conn.execute(update(runs_table).where(runs_table.c.id == old_run_id).values(status="completed"))
        conn.execute(
            insert(websocket_tickets_table),
            [
                {
                    "ticket_digest": digest,
                    "run_id": old_run_id,
                    "user_id": old_user.user_id,
                    "auth_provider_type": "local",
                    "issued_at": now - timedelta(minutes=2),
                    "expires_at": now - timedelta(minutes=1),
                    "consumed_at": now - timedelta(seconds=90) if number % 2 else None,
                }
                for number, digest in enumerate(sorted(expired_digests))
            ],
        )
    first = authority.issue(run_id=run_id, user=user)
    with ticket_engine.connect() as conn:
        remaining = set(conn.execute(select(websocket_tickets_table.c.ticket_digest)).scalars())
        assert conn.execute(select(runs_table.c.status).where(runs_table.c.id == old_run_id)).scalar_one() == "completed"
    assert len(remaining & expired_digests) == 5
    assert sha256(live.ticket.encode()).hexdigest() in remaining
    assert sha256(consumed.ticket.encode()).hexdigest() in remaining
    authority.issue(run_id=run_id, user=user)
    with ticket_engine.connect() as conn:
        assert not expired_digests.intersection(conn.execute(select(websocket_tickets_table.c.ticket_digest)).scalars())
    assert authority.consume(ticket=live.ticket, run_id=old_run_id) is not None
    assert authority.consume(ticket=consumed.ticket, run_id=old_run_id) is None
    assert authority.consume(ticket=first.ticket, run_id=run_id) is not None
