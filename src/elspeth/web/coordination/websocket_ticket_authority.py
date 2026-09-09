"""Database-backed, single-use run WebSocket credentials shared by replicas."""

from __future__ import annotations

import secrets
from datetime import timedelta
from hashlib import sha256
from typing import final
from uuid import UUID

from sqlalchemy import Engine, insert, select, update

from elspeth.web.auth.models import AuthenticationError, UserIdentity
from elspeth.web.coordination.membership_authority import _DATABASE_CLOCK_SQL, _database_clock_value, _ensure_utc
from elspeth.web.execution.websocket_ticket import WebSocketTicket
from elspeth.web.sessions.models import identities_table, runs_table, sessions_table, websocket_tickets_table


@final
class RepositorySessionWebsocketTicketAuthority:
    """Handle-free ticket authority; every call owns its complete transaction.

    ``websocket_tickets.user_id`` binds the canonical identity id, never the
    provider subject. Lock order is identity, session, ticket. The first ticket
    read only locates these locks; consumption re-reads under the ticket lock
    and uses an atomic conditional update. Wrong-run attempts burn the ticket,
    matching the local store. Database failures propagate to the caller.
    """

    __slots__ = ("_clock_sql", "_engine", "_ttl")

    def __init__(self, engine: Engine, ttl_seconds: int = 30) -> None:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be an integer from 1 through 3600")
        if engine.dialect.name not in _DATABASE_CLOCK_SQL:
            raise NotImplementedError(f"WebSocket ticket authority not implemented for {engine.dialect.name}")
        self._engine = engine
        self._clock_sql = _DATABASE_CLOCK_SQL[engine.dialect.name]
        self._ttl = timedelta(seconds=ttl_seconds)

    def issue(self, *, run_id: str | UUID, user: UserIdentity) -> WebSocketTicket:
        """Issue only after proving current active identity and session ownership."""
        with self._engine.begin() as conn:
            identity = conn.execute(
                select(identities_table).where(identities_table.c.identity_id == user.user_id).with_for_update()
            ).one_or_none()
            session = conn.execute(
                select(sessions_table)
                .join(runs_table, runs_table.c.session_id == sessions_table.c.id)
                .where(runs_table.c.id == str(run_id))
                .with_for_update(of=sessions_table)
            ).one_or_none()
            if (
                identity is None
                or identity.access_state != "active"
                or session is None
                or session.archived_at is not None
                or session.user_id != user.user_id
                or session.auth_provider_type != identity.provider
            ):
                raise AuthenticationError("Run WebSocket access refused")
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            raw_ticket = secrets.token_urlsafe(32)
            expires_at = now + self._ttl
            conn.execute(
                insert(websocket_tickets_table).values(
                    ticket_digest=sha256(raw_ticket.encode()).hexdigest(),
                    run_id=str(run_id),
                    user_id=identity.identity_id,
                    auth_provider_type=identity.provider,
                    issued_at=now,
                    expires_at=expires_at,
                )
            )
            return WebSocketTicket(raw_ticket, str(run_id), UserIdentity(identity.identity_id, identity.username), expires_at)

    def consume(self, *, ticket: str, run_id: str | UUID) -> UserIdentity | None:
        """Atomically burn one credential, returning only a currently authorized user."""
        if not ticket.strip():
            return None
        digest = sha256(ticket.encode()).hexdigest()
        with self._engine.begin() as conn:
            candidate = conn.execute(select(websocket_tickets_table).where(websocket_tickets_table.c.ticket_digest == digest)).one_or_none()
            if candidate is None:
                return None
            identity = conn.execute(
                select(identities_table).where(identities_table.c.identity_id == candidate.user_id).with_for_update()
            ).one_or_none()
            session = conn.execute(
                select(sessions_table)
                .join(runs_table, runs_table.c.session_id == sessions_table.c.id)
                .where(runs_table.c.id == candidate.run_id)
                .with_for_update(of=sessions_table)
            ).one_or_none()
            record = conn.execute(
                select(websocket_tickets_table).where(websocket_tickets_table.c.ticket_digest == digest).with_for_update()
            ).one_or_none()
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            if record is None or record.consumed_at is not None:
                return None
            consumed = conn.execute(
                update(websocket_tickets_table)
                .where(websocket_tickets_table.c.ticket_digest == digest, websocket_tickets_table.c.consumed_at.is_(None))
                .values(consumed_at=now)
                .returning(websocket_tickets_table.c.ticket_digest)
            ).one_or_none()
            if (
                consumed is None
                or record.run_id != str(run_id)
                or _ensure_utc(record.expires_at) <= now
                or identity is None
                or identity.identity_id != record.user_id
                or identity.access_state != "active"
                or identity.provider != record.auth_provider_type
                or session is None
                or session.user_id != record.user_id
                or session.auth_provider_type != identity.provider
                or session.archived_at is not None
            ):
                return None
            return UserIdentity(identity.identity_id, identity.username)
