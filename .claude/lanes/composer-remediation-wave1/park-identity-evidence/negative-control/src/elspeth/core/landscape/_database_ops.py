"""Read snapshots and statements on caller-owned Landscape connections."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol

from sqlalchemy import Executable
from sqlalchemy.engine import Connection, Row
from sqlalchemy.exc import SQLAlchemyError

from elspeth.core.landscape.errors import LandscapeRecordError


def _safe_database_error_message(*, operation: str, action: str, exc: SQLAlchemyError, context: str = "") -> str:
    detail = f" ({context})" if context else ""
    return f"{operation} failed{detail} — database rejected audit {action}: {type(exc).__name__}"


class DatabaseOpsConnectionProvider(Protocol):
    """Read connection surface required by database operation helpers."""

    @property
    def is_read_only(self) -> bool:
        raise NotImplementedError

    def read_only_connection(self) -> AbstractContextManager[Connection]:
        raise NotImplementedError


class ReadOnlyDatabaseOps:
    """Queries execute through the database's enforced read-only connection."""

    def __init__(self, db: DatabaseOpsConnectionProvider) -> None:
        self._db = db

    def execute_fetchone(self, query: Executable) -> Row[Any] | None:
        """Return one row or None, refusing an ambiguous multi-row result."""
        try:
            with self._db.read_only_connection() as conn:
                result = conn.execute(query)
                rows = result.fetchmany(2)
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(_safe_database_error_message(operation="execute_fetchone", action="query", exc=exc)) from exc
        if len(rows) > 1:
            raise LandscapeRecordError("execute_fetchone matched multiple rows — single-row audit query is ambiguous")
        if not rows:
            return None
        return rows[0]

    def execute_fetchall(self, query: Executable) -> list[Row[Any]]:
        """Execute a read-only query and return all rows."""
        try:
            with self._db.read_only_connection() as conn:
                result = conn.execute(query)
                return list(result.fetchall())
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(_safe_database_error_message(operation="execute_fetchall", action="query", exc=exc)) from exc

    def execute_fetchall_many(self, queries: Sequence[Executable]) -> list[list[Row[Any]]]:
        """Execute read-only queries through one read snapshot."""
        try:
            with self._db.read_only_connection() as conn:
                if conn.dialect.name == "sqlite" and self._db.is_read_only:
                    # Read-only engines use pysqlite autocommit for ordinary
                    # inspectors; this multi-query boundary needs one snapshot.
                    conn.exec_driver_sql("BEGIN")
                return [list(conn.execute(query).fetchall()) for query in queries]
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(_safe_database_error_message(operation="execute_fetchall_many", action="query", exc=exc)) from exc


class DatabaseOps(ReadOnlyDatabaseOps):
    """Statements require the transaction supplied by their authority owner.

    This helper cannot open a write transaction. Run repositories supply a
    fenced connection; non-run repositories own their closed table writes.
    """

    @staticmethod
    def execute_insert_on(conn: Connection, stmt: Executable, *, context: str = "") -> None:
        """Execute on the caller's connection without owning a transaction."""
        try:
            result = conn.execute(stmt)
        except SQLAlchemyError as exc:
            raise LandscapeRecordError(
                _safe_database_error_message(operation="execute_insert_on", action="write", exc=exc, context=context)
            ) from exc
        if result.rowcount == 0:
            detail = f" ({context})" if context else ""
            raise LandscapeRecordError(f"execute_insert_on: zero rows affected{detail} — audit write failed")
