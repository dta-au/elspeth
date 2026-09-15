"""Handle-free repository authority for run-diagnostics audit rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, final
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import llm_call_usage_entries, record_token_usage_on_connection
from elspeth.web.sessions.audit_checkpoint import uncheckpointed_envelopes
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table, runs_table, sessions_table
from elspeth.web.sessions.protocol import (
    ChatMessageRecord,
    RunDiagnosticsAuditAuthority,
    RunDiagnosticsAuditDraft,
    RunDiagnosticsAuthorityLostError,
)


@final
class RepositoryRunDiagnosticsAuditAuthority:
    """Append diagnostics audit rows only while exact run custody is live."""

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
        if type(content) is not str:
            raise TypeError("content must be an exact string")
        (record,) = self.append_audit_messages(
            authority=authority,
            rows=(RunDiagnosticsAuditDraft(content=content, tool_calls=tuple(tool_calls) if tool_calls else None),),
        )
        return record

    def append_audit_messages(
        self,
        *,
        authority: RunDiagnosticsAuditAuthority,
        rows: Sequence[RunDiagnosticsAuditDraft],
    ) -> tuple[ChatMessageRecord, ...]:
        """Prove custody once and append the whole cohort in one locked commit.

        The cohort is one logical unit of audit evidence
        (elspeth-90231248dc): every row commits in the same locked
        transaction, under one durable custody proof and one contiguous
        sequence block, or none of it does. The per-row loop this
        replaces — one ``append_audit_message`` transaction per record —
        could fail after any prefix, leaving a partial sidecar set that
        reads as a complete diagnostics record.

        An empty ``rows`` sequence is a no-op: no custody proof runs and
        ``sessions.updated_at`` is not bumped.
        """
        if type(authority) is not RunDiagnosticsAuditAuthority:
            raise TypeError("authority must be an exact RunDiagnosticsAuditAuthority")
        for row in rows:
            if type(row) is not RunDiagnosticsAuditDraft:
                raise TypeError("rows must contain exact RunDiagnosticsAuditDraft instances")
        if not rows:
            return ()
        sid = str(authority.session_id)
        rid = str(authority.run_id)
        stid = str(authority.state_id)
        message_ids: tuple[UUID, ...] = tuple(uuid4() for _ in rows)

        # The canonical same-session lock is acquired before either proof.
        # On PostgreSQL this is the transaction advisory lock shared by
        # archive, run, state, and transcript writers; on SQLite it is the
        # process/file mutex. A waiter therefore rechecks after the winning
        # writer commits rather than acting on a stale pre-lock snapshot.
        with locked_session_transaction(self._engine, sid) as conn:
            # Stamped under the lock: an append that waited on the session
            # lock must not record a time from before its own transaction.
            #
            # The Python wall clock is the session store's timestamp source
            # for every chat/session row (SessionServiceImpl._now). Reading
            # the DB clock here instead would make this row's ``created_at``
            # and the session's ``updated_at`` incomparable with their
            # siblings, and on SQLite CURRENT_TIMESTAMP truncates to whole
            # seconds. Lock ordering, not the clock source, is what closes
            # the stale-authority race.
            now = datetime.now(UTC)
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

            replay_records: list[ChatMessageRecord] = []
            active_rows: list[RunDiagnosticsAuditDraft] = []
            for row in rows:
                envelopes = uncheckpointed_envelopes(conn, session_id=sid, envelopes=row.tool_calls or ())
                if row.tool_calls and not envelopes:
                    call_ids = {entry.call_id for entry in llm_call_usage_entries(row.tool_calls)}
                    recorded_ids: set[str | None] = set()
                    for prior in conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid)):
                        prior_ids = {entry.call_id for entry in llm_call_usage_entries(prior.tool_calls or ())}
                        if not call_ids.intersection(prior_ids):
                            continue
                        recorded_ids.update(prior_ids)
                        replay_records.append(
                            ChatMessageRecord(
                                id=UUID(prior.id),
                                session_id=UUID(sid),
                                role=prior.role,
                                content=prior.content,
                                raw_content=prior.raw_content,
                                tool_calls=prior.tool_calls,
                                created_at=prior.created_at if prior.created_at.tzinfo else prior.created_at.replace(tzinfo=UTC),
                                sequence_no=prior.sequence_no,
                                writer_principal=prior.writer_principal,
                                composition_state_id=UUID(prior.composition_state_id) if prior.composition_state_id else None,
                                tool_call_id=prior.tool_call_id,
                                parent_assistant_id=prior.parent_assistant_id,
                            )
                        )
                    if not call_ids.issubset(recorded_ids):
                        raise AuditIntegrityError("Settled diagnostics provider attempt has no audit checkpoint")
                    continue
                active_rows.append(replace(row, tool_calls=envelopes) if row.tool_calls else row)
            rows = tuple(active_rows)
            if not rows:
                return tuple(replay_records)
            message_ids = tuple(uuid4() for _ in rows)

            base_sequence = int(
                conn.execute(
                    select(func.coalesce(func.max(chat_messages_table.c.sequence_no), 0) + 1).where(chat_messages_table.c.session_id == sid)
                ).scalar_one()
            )
            for offset, row in enumerate(rows):
                conn.execute(
                    insert(chat_messages_table).values(
                        id=str(message_ids[offset]),
                        session_id=sid,
                        role="audit",
                        content=row.content,
                        raw_content=None,
                        tool_calls=deep_thaw(row.tool_calls) if row.tool_calls else None,
                        sequence_no=base_sequence + offset,
                        writer_principal="run_diagnostics",
                        composition_state_id=stid,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        created_at=now,
                    )
                )
            entries = llm_call_usage_entries(tuple(envelope for row in rows if row.tool_calls is not None for envelope in row.tool_calls))
            if entries:
                # Task I1 Composer adapter (run diagnostics): the explanation's
                # provider calls are charged with their audit rows.
                record_token_usage_on_connection(
                    conn, session_id=sid, source="composer", run_id=None, entries=entries, recorded_at=database_now(conn)
                )
            updated = conn.execute(
                update(sessions_table).where(sessions_table.c.id == sid, sessions_table.c.archived_at.is_(None)).values(updated_at=now)
            )
            if updated.rowcount != 1:
                raise RunDiagnosticsAuthorityLostError(authority, reason="session_archived")

        return tuple(replay_records) + tuple(
            ChatMessageRecord(
                id=message_ids[offset],
                session_id=authority.session_id,
                role="audit",
                content=row.content,
                raw_content=None,
                tool_calls=row.tool_calls,
                created_at=now,
                sequence_no=base_sequence + offset,
                composition_state_id=authority.state_id,
                writer_principal="run_diagnostics",
                tool_call_id=None,
                parent_assistant_id=None,
            )
            for offset, row in enumerate(rows)
        )
