"""Single-process SQLite session-operation authority."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine

from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository
from elspeth.web.sessions.locking import locked_session_transaction

if TYPE_CHECKING:
    from collections.abc import Iterator


class SQLiteLocalSessionOperationAuthority(_SessionOperationAuthorityRepository):
    """Table-backed exact CAS under the existing process/file session lock.

    SQLite has no membership, peer takeover, or bypass path.  An unreleased
    row remains conflicting even after its lease expires; the supported local
    process must renew or release its own authority while it is live.
    """

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("SQLiteLocalSessionOperationAuthority requires SQLite")
        super().__init__(engine)

    @contextmanager
    def _locked_transaction(self, session_id: str) -> Iterator[Connection]:
        with locked_session_transaction(self._engine, session_id) as conn:
            yield conn
