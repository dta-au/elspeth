"""Single-process SQLite session-operation authority."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine

from elspeth.web.coordination.approval_authority import ApprovalSupersession, refuse_unrecorded_approval_supersession
from elspeth.web.coordination.quota_authority import QuotaExceeded, refuse_unrecorded_quota_exceeded
from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository
from elspeth.web.sessions.locking import locked_session_transaction

if TYPE_CHECKING:
    from collections.abc import Iterator


class SQLiteLocalSessionOperationAuthority(_SessionOperationAuthorityRepository):
    """Table-backed exact CAS under the existing process/file session lock.

    SQLite has no membership or distributed peer-takeover path.  A live lease
    always conflicts; an expired lease is locally recoverable under the same
    process/file lock, including after a process restart changes the diagnostic
    owner identity.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
        approval_supersession_recorder: Callable[[ApprovalSupersession], None] = refuse_unrecorded_approval_supersession,
    ) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("SQLiteLocalSessionOperationAuthority requires SQLite")
        super().__init__(
            engine,
            quota_exceeded_recorder=quota_exceeded_recorder,
            approval_supersession_recorder=approval_supersession_recorder,
        )

    @contextmanager
    def _locked_transaction(self, session_id: str) -> Iterator[Connection]:
        with locked_session_transaction(self._engine, session_id) as conn:
            yield conn

    def _expired_owner_allows_takeover(
        self,
        conn: Connection,
        *,
        owner_instance_id: str,
        database_now: datetime,
    ) -> bool:
        """Permit local expiry recovery without a membership dependency."""
        del conn, owner_instance_id, database_now
        return True
