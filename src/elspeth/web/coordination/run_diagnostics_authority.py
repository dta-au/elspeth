"""Handle-free repository authority for run-diagnostics audit rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, final
from uuid import uuid4

from sqlalchemy import Engine, func, insert, select, update

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table, runs_table, sessions_table
from elspeth.web.sessions.protocol import (
    ChatMessageRecord,
    RunDiagnosticsAuditAuthority,
    RunDiagnosticsAuthorityLostError,
)


@final
class RepositoryRunDiagnosticsAuditAuthority:
    """Append a diagnostics audit row only while exact run custody is live."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise NotImplementedError(f"run diagnostics audit authority not implemented for {engine.dialect.name}")
        self._engine = engine

    def append_audit_message(
        self,
        *,
        authority: RunDiagnosticsAuditAuthority,
        content: str,
        tool_calls: Sequence[Mapping[str, Any]] | None,
    ) -> ChatMessageRecord:
        """Prove custody and append one attributed row in one locked commit."""
        if type(authority) is not RunDiagnosticsAuditAuthority:
            raise TypeError("authority must be an exact RunDiagnosticsAuditAuthority")
        if type(content) is not str:
            raise TypeError("content must be an exact string")
        sid = str(authority.session_id)
        rid = str(authority.run_id)
        stid = str(authority.state_id)
        message_id = uuid4()
        # The Python wall clock is the session store's timestamp source for
        # every chat/session row (SessionServiceImpl._now). Reading the DB
        # clock here instead would make this one row's ``created_at`` and the
        # session's ``updated_at`` incomparable with their siblings, and on
        # SQLite CURRENT_TIMESTAMP truncates to whole seconds. Lock ordering,
        # not the clock source, is what closes the stale-authority race.
        now = datetime.now(UTC)

        # The canonical same-session lock is acquired before either proof.
        # On PostgreSQL this is the transaction advisory lock shared by
        # archive, run, state, and transcript writers; on SQLite it is the
        # process/file mutex. A waiter therefore rechecks after the winning
        # writer commits rather than acting on a stale pre-lock snapshot.
        with locked_session_transaction(self._engine, sid) as conn:
            session_row = conn.execute(
                select(sessions_table.c.id, sessions_table.c.archived_at).where(sessions_table.c.id == sid).with_for_update()
            ).one_or_none()
            if session_row is None:
                raise RunDiagnosticsAuthorityLostError(authority, reason="session_missing")
            if session_row.archived_at is not None:
                raise RunDiagnosticsAuthorityLostError(authority, reason="session_archived")

            run_row = conn.execute(
                select(runs_table.c.session_id, runs_table.c.state_id).where(runs_table.c.id == rid).with_for_update()
            ).one_or_none()
            if run_row is None:
                raise RunDiagnosticsAuthorityLostError(authority, reason="run_missing")
            if run_row.session_id != sid or run_row.state_id != stid:
                raise RunDiagnosticsAuthorityLostError(authority, reason="run_rebound")

            next_sequence = int(
                conn.execute(
                    select(func.coalesce(func.max(chat_messages_table.c.sequence_no), 0) + 1).where(chat_messages_table.c.session_id == sid)
                ).scalar_one()
            )
            conn.execute(
                insert(chat_messages_table).values(
                    id=str(message_id),
                    session_id=sid,
                    role="audit",
                    content=content,
                    raw_content=None,
                    tool_calls=deep_thaw(tool_calls) if tool_calls else None,
                    sequence_no=next_sequence,
                    writer_principal="run_diagnostics",
                    composition_state_id=stid,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=now,
                )
            )
            updated = conn.execute(
                update(sessions_table).where(sessions_table.c.id == sid, sessions_table.c.archived_at.is_(None)).values(updated_at=now)
            )
            if updated.rowcount != 1:
                raise RunDiagnosticsAuthorityLostError(authority, reason="session_archived")

        return ChatMessageRecord(
            id=message_id,
            session_id=authority.session_id,
            role="audit",
            content=content,
            raw_content=None,
            tool_calls=tool_calls,
            created_at=now,
            sequence_no=next_sequence,
            composition_state_id=authority.state_id,
            writer_principal="run_diagnostics",
            tool_call_id=None,
            parent_assistant_id=None,
        )
