"""Single-process SQLite session-operation authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine

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

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("SQLiteLocalSessionOperationAuthority requires SQLite")
        super().__init__(engine)

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
